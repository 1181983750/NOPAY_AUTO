"""Local-only configuration and runtime dashboard for the canvas bot."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


SPEED_PROFILES: dict[str, dict[str, float]] = {
    # The safe profile keeps the existing visual confirmation cadence.
    "safe": {
        "poll_interval_sec": 0.8,
        "action_throttle_sec": 0.7,
        "secondary_confirm_delay_sec": 0.5,
        "state_poll_sec": 0.35,
        "form_focus_wait_sec": 0.16,
        "form_verify_wait_sec": 0.40,
    },
    # Fast mode affects only reversible page/field timing. Password and cancel
    # confirmation timeouts remain separately configured and unchanged.
    "fast": {
        "poll_interval_sec": 0.5,
        "action_throttle_sec": 0.45,
        "secondary_confirm_delay_sec": 0.35,
        "state_poll_sec": 0.25,
        "form_focus_wait_sec": 0.12,
        "form_verify_wait_sec": 0.25,
    },
}


def _number(value: Any, label: str, *, minimum: Decimal, allow_empty: bool = False) -> str:
    text = str(value or "").strip()
    if not text and allow_empty:
        return ""
    if not text:
        raise ValueError(f"{label}不能为空")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{label}必须是数字") from exc
    if not number.is_finite() or number < minimum:
        raise ValueError(f"{label}必须大于等于 {minimum}")
    return text


def _integer(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是整数") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"{label}必须在 {minimum} 到 {maximum} 之间")
    return number


def _flag(value: Any) -> bool:
    return value is True or str(value).lower() in {"1", "true", "on", "yes"}


def public_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return only the values that the local UI is allowed to change."""
    speed_mode = str(cfg.get("speed_mode") or "safe").lower()
    speed = SPEED_PROFILES.get(speed_mode, SPEED_PROFILES["safe"])
    payment = cfg.get("payment", {})
    cancel_flow = cfg.get("cancel_flow", {})
    submit_flow = cfg.get("submit_flow", {})
    debug = cfg.get("debug", {})
    password = cfg.get("password", {})
    multi_order = cfg.get("multi_order", {})
    raw_keywords = multi_order.get("payment_keywords") or payment.get("keywords") or [payment.get("keyword", "")]
    if isinstance(raw_keywords, str):
        raw_keywords = raw_keywords.splitlines()
    return {
        "speed_mode": speed_mode if speed_mode in SPEED_PROFILES else "safe",
        "amount": str(cfg.get("amount", "")),
        "limit_min": str(cfg.get("limit_min", "")),
        "limit_max": str(cfg.get("limit_max", "")),
        "poll_interval_sec": speed["poll_interval_sec"],
        "action_throttle_sec": speed["action_throttle_sec"],
        "repost_delay_sec": cfg.get("repost_delay_sec", 30),
        "auto_make_order": bool(cfg.get("auto_make_order", True)),
        "auto_repost": bool(cfg.get("auto_repost", True)),
        "auto_accept": bool(cfg.get("auto_accept", True)),
        "payment_keyword": str(payment.get("keyword", "")),
        "payment_keywords": "\n".join(str(value).strip() for value in raw_keywords if str(value).strip()),
        "multi_order_enabled": bool(multi_order.get("enabled", False)),
        "adopt_existing_pending_orders": bool(multi_order.get("adopt_existing_pending_orders", False)),
        "payment_ocr_attempts": payment.get("ocr_attempts", 8),
        "cancel_confirm_delay_sec": cancel_flow.get("confirm_delay_sec", 1.2),
        "cancel_retry_interval_sec": cancel_flow.get("retry_interval_sec", 2.0),
        "cancel_confirm_timeout_sec": cancel_flow.get("confirm_timeout_sec", 8.0),
        "submit_secondary_confirm_delay_sec": speed["secondary_confirm_delay_sec"],
        "submit_secondary_confirm_timeout_sec": submit_flow.get("secondary_confirm_timeout_sec", 7.0),
        "submit_password_timeout_sec": submit_flow.get("password_timeout_sec", 8.0),
        "debug_save_each_tick": bool(debug.get("save_each_tick", False)),
        "password_env": str(password.get("env") or "QJD67_PAY_PASSWORD"),
        "password_available": bool(os.getenv(str(password.get("env") or "QJD67_PAY_PASSWORD"))),
    }


def validate_dashboard_settings(cfg: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Merge and validate editable dashboard values without touching secrets."""
    updated = json.loads(json.dumps(cfg))
    speed_mode = str(values.get("speed_mode") or "safe").strip().lower()
    if speed_mode not in SPEED_PROFILES:
        raise ValueError("运行速度档位无效")
    speed = SPEED_PROFILES[speed_mode]
    amount = _number(values.get("amount"), "变现金额", minimum=Decimal("10"))
    limit_min = _number(values.get("limit_min"), "最小限额", minimum=Decimal("0"), allow_empty=True)
    limit_max = _number(values.get("limit_max"), "最大限额", minimum=Decimal("0"), allow_empty=True)
    if limit_min and limit_max and Decimal(limit_min) > Decimal(limit_max):
        raise ValueError("最小限额不能大于最大限额")

    raw_keywords = re_split_keywords(values.get("payment_keywords"))
    if not raw_keywords:
        raise ValueError("收款卡片关键词列表至少需要一项")
    if any(len(item) > 80 for item in raw_keywords):
        raise ValueError("收款方式关键词不能超过 80 个字符")

    updated["amount"] = amount
    updated["limit_min"] = limit_min
    updated["limit_max"] = limit_max
    updated["speed_mode"] = speed_mode
    updated["poll_interval_sec"] = speed["poll_interval_sec"]
    updated["action_throttle_sec"] = speed["action_throttle_sec"]
    updated["repost_delay_sec"] = float(_number(values.get("repost_delay_sec"), "重挂等待时间", minimum=Decimal("1")))
    updated["auto_make_order"] = _flag(values.get("auto_make_order"))
    updated["auto_repost"] = _flag(values.get("auto_repost"))
    updated["auto_accept"] = _flag(values.get("auto_accept"))

    payment = updated.setdefault("payment", {})
    payment["keyword"] = raw_keywords[0]
    payment["keywords"] = raw_keywords
    payment["ocr_attempts"] = _integer(values.get("payment_ocr_attempts"), "收款卡片识别次数", minimum=1, maximum=30)

    multi_order = updated.setdefault("multi_order", {})
    multi_order["enabled"] = _flag(values.get("multi_order_enabled"))
    multi_order["adopt_existing_pending_orders"] = _flag(values.get("adopt_existing_pending_orders"))
    multi_order["payment_keywords"] = raw_keywords
    multi_order.pop("minimum_remaining_balance", None)

    cancel_flow = updated.setdefault("cancel_flow", {})
    cancel_flow["confirm_delay_sec"] = float(
        _number(values.get("cancel_confirm_delay_sec"), "取消二次确认等待", minimum=Decimal("0.1"))
    )
    cancel_flow["retry_interval_sec"] = float(
        _number(values.get("cancel_retry_interval_sec"), "取消重试间隔", minimum=Decimal("0.1"))
    )
    cancel_flow["confirm_timeout_sec"] = float(
        _number(values.get("cancel_confirm_timeout_sec"), "取消确认超时", minimum=Decimal("1"))
    )

    submit_flow = updated.setdefault("submit_flow", {})
    submit_flow["secondary_confirm_delay_sec"] = speed["secondary_confirm_delay_sec"]
    submit_flow["state_poll_sec"] = speed["state_poll_sec"]
    submit_flow["secondary_confirm_timeout_sec"] = float(
        _number(values.get("submit_secondary_confirm_timeout_sec"), "挂单二次确认超时", minimum=Decimal("1"))
    )
    submit_flow["password_timeout_sec"] = float(
        _number(values.get("submit_password_timeout_sec"), "支付密码弹窗超时", minimum=Decimal("1"))
    )
    form_input = updated.setdefault("form_input", {})
    form_input["focus_wait_sec"] = speed["form_focus_wait_sec"]
    form_input["verify_wait_sec"] = speed["form_verify_wait_sec"]
    updated.setdefault("debug", {})["save_each_tick"] = _flag(values.get("debug_save_each_tick"))
    return updated


def re_split_keywords(value: Any) -> list[str]:
    return [item.strip() for item in str(value or "").replace("，", ",").replace(";", ",").split(",") for item in item.splitlines() if item.strip()]


def write_config(path: Path, cfg: dict[str, Any]) -> None:
    """Atomically replace the JSON config so a running process never reads half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent, suffix=".json") as tmp:
        json.dump(cfg, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        temporary_path = Path(tmp.name)
    temporary_path.replace(path)


@dataclass
class DashboardState:
    config_path: Path
    project_root: Path
    process: subprocess.Popen[str] | None = None
    process_log: Any = None
    payment_password: str = ""
    server_url: str = ""
    dashboard_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def log_path(self) -> Path:
        return self.debug_dir / "dashboard-bot.log"

    @property
    def debug_dir(self) -> Path:
        with self.config_path.open("r", encoding="utf-8") as config_file:
            cfg = json.load(config_file)
        raw = str(cfg.get("debug", {}).get("dir", "debug-screenshots"))
        path = Path(raw)
        return path if path.is_absolute() else self.project_root / path

    def load_config(self) -> dict[str, Any]:
        with self.config_path.open("r", encoding="utf-8") as config_file:
            return json.load(config_file)

    def running(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def password_env(self) -> str:
        return str(self.load_config().get("password", {}).get("env") or "QJD67_PAY_PASSWORD")

    def password_available(self) -> bool:
        return bool(self.payment_password or os.getenv(self.password_env()))

    def internal_password(self) -> str:
        return self.payment_password

    def set_payment_password(self, value: Any) -> str:
        password = str(value or "").strip()
        if password and (len(password) != 6 or not password.isdigit()):
            raise ValueError("支付密码必须是 6 位数字")
        self.payment_password = password
        return "支付密码已清除。" if not password else "支付密码已保存到当前控制台内存。"

    def current_public_config(self) -> dict[str, Any]:
        values = public_config(self.load_config())
        values["password_available"] = self.password_available()
        values["password_source"] = "dashboard" if self.payment_password else ("environment" if values["password_available"] else "none")
        return values

    def clear_previous_run_artifacts(self) -> None:
        """Keep each dashboard-started run's screenshots and bot log self-contained."""
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        for path in self.debug_dir.iterdir():
            if not path.is_file():
                continue
            # Keep the log file in place. On Windows another dashboard/bot
            # process may still hold it open, making unlink fail with WinError 32.
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                path.unlink()

    def start(self) -> tuple[bool, str]:
        with self.lock:
            if self.running():
                return False, "脚本已经由本控制台启动。"
            # A crashed or naturally exited child can leave the redirected log
            # handle open. Windows then refuses to remove the old log on the
            # next start, so close the stale handle before cleanup.
            if self.process_log:
                self.process_log.close()
                self.process_log = None
            self.clear_previous_run_artifacts()
            self.process_log = self.log_path.open("w", encoding="utf-8", buffering=1)
            self.process_log.write(f"[dashboard] started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            self.process = subprocess.Popen(
                [sys.executable, "-u", "-m", "canvas_bot", "run", "--config", str(self.config_path)],
                cwd=self.project_root,
                stdout=self.process_log,
                stderr=subprocess.STDOUT,
                text=True,
                env=self.process_environment(),
            )
            return True, "已清除上一轮调试截图和运行日志，自动脚本已启动。"

    def process_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        # Windows uses the active console code page for redirected output unless forced.
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        environment.pop(self.password_env(), None)
        environment["QJD67_DASHBOARD_URL"] = self.server_url
        environment["QJD67_DASHBOARD_TOKEN"] = self.dashboard_token
        return environment

    def stop(self) -> tuple[bool, str]:
        with self.lock:
            if not self.running():
                return False, "没有可由本控制台停止的脚本。"
            assert self.process is not None
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=4)
            if self.process_log:
                self.process_log.write(f"[dashboard] stopped {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                self.process_log.close()
                self.process_log = None
            return True, "自动脚本已停止。"

    def status(self) -> dict[str, Any]:
        process = self.process
        return {
            "running": self.running(),
            "pid": process.pid if process and self.running() else None,
            "controlled_process_only": True,
            "config": self.current_public_config(),
            "log": tail_text(self.log_path, 120),
        }

    def screenshots(self) -> list[dict[str, Any]]:
        if not self.debug_dir.exists():
            return []
        images = sorted(
            (path for path in self.debug_dir.glob("*.png") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:12]
        return [
            {"name": path.name, "time": int(path.stat().st_mtime), "bytes": path.stat().st_size}
            for path in images
        ]


def tail_text(path: Path, limit: int) -> str:
    if not path.exists():
        return ""
    try:
        content = path.read_text(encoding="utf-8", errors="replace").replace("\x00", "")
        return "\n".join(content.splitlines()[-limit:])
    except OSError:
        return ""


class DashboardHandler(BaseHTTPRequestHandler):
    state: DashboardState

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def send_json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def send_html(self, value: str) -> None:
        payload = value.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 65_536:
            raise ValueError("配置请求无效")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("配置请求无效")
        return value

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/internal/password":
            supplied = self.headers.get("X-Dashboard-Token") or ""
            if not secrets.compare_digest(supplied, self.state.dashboard_token):
                self.send_json({"error": "未授权"}, HTTPStatus.FORBIDDEN)
                return
            self.send_json({"password": self.state.internal_password()})
            return
        if path == "/":
            self.send_html(DASHBOARD_HTML)
            return
        if path == "/api/status":
            self.send_json(self.state.status())
            return
        if path == "/api/config":
            self.send_json(self.state.current_public_config())
            return
        if path == "/api/screenshots":
            self.send_json(self.state.screenshots())
            return
        if path.startswith("/debug/"):
            self.serve_screenshot(path.removeprefix("/debug/"))
            return
        self.send_json({"error": "未找到该地址"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/config":
                updated = validate_dashboard_settings(self.state.load_config(), self.read_json())
                write_config(self.state.config_path, updated)
                self.send_json({"ok": True, "message": "配置已保存；运行中的脚本需停止后重新启动才会读取新参数。"})
                return
            if path == "/api/password":
                message = self.state.set_payment_password(self.read_json().get("password"))
                self.send_json({"ok": True, "message": message, "password_available": self.state.password_available()})
                return
            if path == "/api/bot/start":
                ok, message = self.state.start()
                self.send_json({"ok": ok, "message": message, "status": self.state.status()})
                return
            if path == "/api/bot/stop":
                ok, message = self.state.stop()
                self.send_json({"ok": ok, "message": message, "status": self.state.status()})
                return
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except OSError as exc:
            self.send_json({"ok": False, "error": f"保存失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json({"error": "未找到该地址"}, HTTPStatus.NOT_FOUND)

    def serve_screenshot(self, name: str) -> None:
        candidate = (self.state.debug_dir / Path(name).name).resolve()
        directory = self.state.debug_dir.resolve()
        if candidate.parent != directory or not candidate.is_file() or candidate.suffix.lower() != ".png":
            self.send_json({"error": "截图不存在"}, HTTPStatus.NOT_FOUND)
            return
        content = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)


DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NO钱包自动化控制台</title>
  <style>
    :root { color-scheme: light; --ink:#172033; --muted:#617083; --line:#d7dee8; --surface:#ffffff; --ground:#f4f7fb; --blue:#1668dc; --green:#177c45; --red:#c63f36; --amber:#a56800; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--ground); color:var(--ink); font:14px/1.45 "Microsoft YaHei UI", "Segoe UI", sans-serif; }
    button,input,textarea { font:inherit; }
    button { border:1px solid var(--line); border-radius:6px; min-height:34px; padding:6px 12px; background:var(--surface); color:var(--ink); cursor:pointer; }
    button:hover { border-color:var(--blue); color:var(--blue); }
    button.primary { color:#fff; background:var(--blue); border-color:var(--blue); }
    button.stop { color:#fff; background:var(--red); border-color:var(--red); }
    button:disabled { opacity:.45; cursor:default; }
    header { background:var(--surface); border-bottom:1px solid var(--line); }
    .header-inner { max-width:1440px; margin:auto; min-height:64px; padding:12px 24px; display:flex; align-items:center; justify-content:space-between; gap:16px; }
    h1 { margin:0; font-size:20px; font-weight:650; letter-spacing:0; }
    .actions { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    .state { border-radius:999px; padding:4px 9px; font-size:12px; font-weight:600; background:#e9edf2; color:var(--muted); }
    .state.on { background:#e5f5eb; color:var(--green); }
    main { max-width:1440px; margin:0 auto; padding:20px 24px 36px; display:grid; grid-template-columns:minmax(540px,1.05fr) minmax(420px,.95fr); gap:16px; }
    .panel { background:var(--surface); border:1px solid var(--line); border-radius:7px; }
    .panel > header { padding:13px 16px; background:transparent; border-bottom:1px solid var(--line); display:flex; align-items:center; justify-content:space-between; }
    h2 { margin:0; font-size:15px; font-weight:650; }
    form { padding:16px; }
    fieldset { margin:0 0 16px; padding:14px; border:1px solid var(--line); border-radius:6px; }
    legend { padding:0 6px; color:var(--muted); font-size:12px; }
    .form-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }
    label { display:grid; gap:5px; color:var(--muted); font-size:12px; }
    .field-label { display:flex; align-items:center; gap:6px; }
    .info { display:inline-grid; width:16px; height:16px; place-items:center; border:1px solid var(--muted); border-radius:50%; color:var(--muted); font-size:11px; font-weight:700; cursor:help; }
    input[type="text"], input[type="number"], select, textarea { width:100%; min-height:36px; border:1px solid var(--line); border-radius:5px; padding:6px 8px; color:var(--ink); background:#fff; }
    input[readonly] { color:#667085; background:#f3f5f8; cursor:not-allowed; }
    textarea { min-height:74px; resize:vertical; }
    input:focus { outline:2px solid #b6d3ff; border-color:var(--blue); }
    .checks { display:flex; gap:20px; align-items:center; flex-wrap:wrap; }
    .checks label { display:flex; align-items:center; gap:7px; color:var(--ink); font-size:14px; }
    .form-footer { display:flex; gap:10px; justify-content:flex-end; align-items:center; }
    .hint, .status-text { color:var(--muted); font-size:12px; }
    .status-text.error { color:var(--red); }
    .status-text.ok { color:var(--green); }
    .runtime { padding:16px; display:grid; gap:14px; }
    .runtime-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }
    .metric { padding:10px; border:1px solid var(--line); border-radius:6px; }
    .metric b { display:block; font-size:12px; font-weight:500; color:var(--muted); }
    .metric span { display:block; margin-top:4px; font-size:16px; font-weight:650; }
    pre { margin:0; height:330px; overflow:auto; padding:12px; color:#d7e2f3; background:#172033; border-radius:6px; font:12px/1.45 Consolas, monospace; white-space:pre-wrap; word-break:break-word; }
    .screens { grid-column:1 / -1; }
    .gallery { padding:16px; display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:12px; }
    .shot { min-width:0; }
    .shot button { padding:0; display:block; width:100%; height:180px; overflow:hidden; border-radius:6px; background:#eef2f7; }
    .shot img { display:block; width:100%; height:100%; object-fit:contain; }
    .shot small { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; padding-top:5px; color:var(--muted); }
    dialog { max-width:min(92vw,850px); border:1px solid var(--line); border-radius:7px; padding:0; box-shadow:0 12px 42px rgba(17,33,58,.2); }
    dialog::backdrop { background:rgba(23,32,51,.42); }
    .dialog-head { padding:10px; display:flex; justify-content:flex-end; }
    dialog img { display:block; max-width:86vw; max-height:82vh; }
    @media (max-width:980px) { main { grid-template-columns:1fr; padding:14px; } .screens { grid-column:auto; } .header-inner { padding:12px 14px; } }
    @media (max-width:600px) { .form-grid { grid-template-columns:1fr 1fr; } .runtime-grid { grid-template-columns:1fr 1fr; } }
  </style>
</head>
<body>
  <header><div class="header-inner"><h1>NO钱包自动化控制台</h1><div class="actions"><span id="state" class="state">未运行</span><button id="start" class="primary" type="button">启动脚本</button><button id="stop" class="stop" type="button">停止脚本</button></div></div></header>
  <main>
    <section class="panel"><header><h2>挂单参数</h2><span class="hint">保存后需重启脚本生效</span></header>
      <form id="settings">
        <fieldset><legend>金额与限额</legend><div class="form-grid">
          <label>变现金额<input name="amount" type="text" inputmode="decimal"></label>
          <label>最小限额<input name="limit_min" type="text" inputmode="decimal"></label>
          <label>最大限额<input name="limit_max" type="text" inputmode="decimal"></label>
        </div></fieldset>
        <fieldset><legend>等待与节奏（秒）</legend><div class="form-grid">
          <label>运行速度<select name="speed_mode"><option value="safe">安全：稳妥校验</option><option value="fast">极速：缩短页面与表单等待</option></select><span class="hint">极速不缩短支付密码、取消确认和重挂等待。</span></label>
          <label>挂单后重挂等待<input name="repost_delay_sec" type="number" min="1" step="1"></label>
          <label>页面轮询间隔 <span class="hint">由速度档位控制</span><input name="poll_interval_sec" type="number" min="0.1" step="0.1" readonly aria-readonly="true"></label>
          <label>操作最小间隔 <span class="hint">由速度档位控制</span><input name="action_throttle_sec" type="number" min="0.1" step="0.1" readonly aria-readonly="true"></label>
          <label>取消二次确认等待<input name="cancel_confirm_delay_sec" type="number" min="0.1" step="0.1"></label>
          <label>取消确认超时<input name="cancel_confirm_timeout_sec" type="number" min="1" step="1"></label>
          <label>挂单二次确认超时<input name="submit_secondary_confirm_timeout_sec" type="number" min="1" step="1"></label>
        </div></fieldset>
        <fieldset><legend>收款方式与自动操作</legend><div class="form-grid">
          <label><span class="field-label">收款卡片关键词列表 <span class="info" tabindex="0" aria-label="填写规则" title="推荐每行一张收款卡关键词。也支持英文逗号、中文逗号、英文分号分隔；不要用空格分隔。脚本按填写顺序循环选择。">i</span></span><textarea name="payment_keywords" placeholder="每行一张卡关键词" title="推荐每行一张收款卡关键词。也支持英文逗号、中文逗号、英文分号分隔；不要用空格分隔。脚本按填写顺序循环选择。"></textarea></label>
          <label>卡片 OCR 查找次数<input name="payment_ocr_attempts" type="number" min="1" max="30" step="1"></label>
          <label>取消重试间隔<input name="cancel_retry_interval_sec" type="number" min="0.1" step="0.1"></label>
          <label>挂单二次确认等待 <span class="hint">由速度档位控制</span><input name="submit_secondary_confirm_delay_sec" type="number" min="0.1" step="0.1" readonly aria-readonly="true"></label>
          <label>支付密码弹窗超时<input name="submit_password_timeout_sec" type="number" min="1" step="1"></label>
        </div><div class="checks" style="margin-top:14px">
          <label><input name="auto_make_order" type="checkbox"> 自动挂单</label>
          <label><input name="auto_repost" type="checkbox"> 超时自动重挂</label>
          <label><input name="auto_accept" type="checkbox"> 自动接单</label>
          <label><input name="multi_order_enabled" type="checkbox"> 多挂单直到低于最小限额</label>
          <label><input name="adopt_existing_pending_orders" type="checkbox"> <span class="field-label">启动后接管已有挂单 <span class="info" tabindex="0" aria-label="接管规则" title="从脚本启动时重新计时；超时后按最底部优先取消。">i</span></span></label>
          <label><input name="debug_save_each_tick" type="checkbox"> 每轮保存截图</label>
        </div></fieldset>
        <div class="form-footer"><span id="save-message" class="status-text"></span><button class="primary" type="submit">保存配置</button></div>
      </form>
    </section>
    <section class="panel"><header><h2>运行状态</h2><span id="password-state" class="hint"></span></header><div class="runtime">
      <div class="runtime-grid"><div class="metric"><b>脚本状态</b><span id="run-state">未运行</span></div><div class="metric"><b>进程 PID</b><span id="pid">-</span></div><div class="metric"><b>配置载入</b><span id="config-state">等待中</span></div></div>
      <div class="form-footer" style="justify-content:flex-start"><label style="width:min(250px,100%)">支付密码<input id="payment-password" type="password" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="6 位数字"></label><button id="save-password" type="button">设置支付密码</button></div>
      <pre id="log">等待日志...</pre>
    </div></section>
    <section class="panel screens"><header><h2>调试截图</h2><span class="hint">最新 12 张</span></header><div id="gallery" class="gallery"></div></section>
  </main>
  <dialog id="preview"><div class="dialog-head"><button type="button" id="close-preview">关闭</button></div><img alt="调试截图预览"></dialog>
  <script>
    const $ = (s) => document.querySelector(s);
    let config = null;
    const speedProfiles = {
      safe: { poll_interval_sec: 0.8, action_throttle_sec: 0.7, submit_secondary_confirm_delay_sec: 0.5 },
      fast: { poll_interval_sec: 0.5, action_throttle_sec: 0.45, submit_secondary_confirm_delay_sec: 0.35 },
    };
    const names = ['speed_mode','amount','limit_min','limit_max','repost_delay_sec','poll_interval_sec','action_throttle_sec','cancel_confirm_delay_sec','cancel_confirm_timeout_sec','submit_secondary_confirm_timeout_sec','payment_keywords','payment_ocr_attempts','cancel_retry_interval_sec','submit_secondary_confirm_delay_sec','submit_password_timeout_sec'];
    const checks = ['auto_make_order','auto_repost','auto_accept','multi_order_enabled','adopt_existing_pending_orders','debug_save_each_tick'];
    async function request(url, options) { const r = await fetch(url, options); const data = await r.json(); if (!r.ok) throw new Error(data.error || '请求失败'); return data; }
    function fill(c) { config = c; names.forEach(n => { const e = document.querySelector(`[name="${n}"]`); if (e) e.value = c[n] ?? ''; }); checks.forEach(n => document.querySelector(`[name="${n}"]`).checked = Boolean(c[n])); const source = c.password_source === 'dashboard' ? '控制台内存' : (c.password_source === 'environment' ? '环境变量' : '未配置'); $('#password-state').textContent = `支付密码：${source}`; }
    function applySpeedProfile(mode) { const profile = speedProfiles[mode]; if (!profile) return; Object.entries(profile).forEach(([name, value]) => { const input = document.querySelector(`[name="${name}"]`); if (input) input.value = value; }); }
    async function loadConfig() { fill(await request('/api/config')); }
    function collect() { const data = {}; names.forEach(n => data[n] = document.querySelector(`[name="${n}"]`).value); checks.forEach(n => data[n] = document.querySelector(`[name="${n}"]`).checked); return data; }
    async function save(e) { e.preventDefault(); const message = $('#save-message'); message.className = 'status-text'; message.textContent = '保存中...'; try { const data = await request('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(collect())}); message.className = 'status-text ok'; message.textContent = data.message; await loadConfig(); } catch (err) { message.className = 'status-text error'; message.textContent = err.message; } }
    async function savePassword() { const input = $('#payment-password'); const message = $('#save-message'); try { const data = await request('/api/password', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password:input.value})}); input.value = ''; message.className = 'status-text ok'; message.textContent = data.message; await loadConfig(); } catch (err) { message.className = 'status-text error'; message.textContent = err.message; } }
    async function control(action) { try { const data = await request(`/api/bot/${action}`, {method:'POST'}); $('#save-message').className = data.ok ? 'status-text ok' : 'status-text error'; $('#save-message').textContent = data.message; refresh(); } catch (err) { $('#save-message').className='status-text error'; $('#save-message').textContent=err.message; } }
    let logInitialized = false;
    function logNearBottom(log) { return log.scrollHeight - log.scrollTop - log.clientHeight < 18; }
    function renderLog(value) { const log = $('#log'); const follow = !logInitialized || logNearBottom(log); log.textContent = value || '暂无本控制台启动的日志。'; logInitialized = true; if (follow) requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; }); }
    async function refresh() { try { const s = await request('/api/status'); $('#state').textContent = s.running ? '运行中' : '未运行'; $('#state').className = `state ${s.running ? 'on' : ''}`; $('#run-state').textContent = s.running ? '运行中' : '未运行'; $('#pid').textContent = s.pid || '-'; $('#config-state').textContent = s.running ? '重启后更新' : '已就绪'; renderLog(s.log); $('#start').disabled = s.running; $('#stop').disabled = !s.running; } catch (_) {} }
    async function shots() { const gallery = $('#gallery'); const list = await request('/api/screenshots'); gallery.replaceChildren(); list.forEach(item => { const wrap=document.createElement('div'); wrap.className='shot'; const button=document.createElement('button'); const image=document.createElement('img'); image.loading='lazy'; image.src=`/debug/${encodeURIComponent(item.name)}?t=${item.time}`; image.alt=item.name; button.append(image); button.onclick=()=>{ $('#preview img').src=image.src; $('#preview').showModal(); }; const label=document.createElement('small'); label.textContent=item.name; wrap.append(button,label); gallery.append(wrap); }); }
    $('#settings').addEventListener('submit', save); $('[name="speed_mode"]').onchange=(event)=>applySpeedProfile(event.target.value); $('#save-password').onclick=savePassword; $('#start').onclick=()=>control('start'); $('#stop').onclick=()=>control('stop'); $('#close-preview').onclick=()=>$('#preview').close();
    Promise.all([loadConfig(), refresh(), shots()]); setInterval(refresh, 1500); setInterval(shots, 7000);
  </script>
</body></html>"""


def run_dashboard(config_path: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    project_root = config_path.parent.parent.resolve()
    state = DashboardState(config_path=config_path.resolve(), project_root=project_root)
    state.debug_dir.mkdir(parents=True, exist_ok=True)
    state.log_path.write_text(
        f"[dashboard] session started {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
        encoding="utf-8",
    )

    class BoundDashboardHandler(DashboardHandler):
        pass

    BoundDashboardHandler.state = state
    server = ThreadingHTTPServer((host, port), BoundDashboardHandler)
    loopback_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    state.server_url = f"http://{loopback_host}:{server.server_port}"
    print(f"[dashboard] Open http://{host}:{port}")
    print("[dashboard] Press Ctrl+C to stop the dashboard.")
    try:
        server.serve_forever()
    finally:
        state.stop()
        server.server_close()
