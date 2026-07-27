"""Native WebView shell for the local dashboard in packaged Windows builds."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


def runtime_root() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def request_json(url: str, value: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = None if value is None else json.dumps(value).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"} if payload is not None else {},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_dashboard(url: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("本地控制台启动失败，请检查 desktop-dashboard.log")
        try:
            request_json(f"{url}/api/status")
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("本地控制台启动超时，请检查 desktop-dashboard.log")


def show_error(message: str) -> None:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "NO 钱包自动化", 0x10)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open the packaged local dashboard in a native WebView window.")
    parser.add_argument("--config", default="configs/qjd67.json", help="Path to the config JSON.")
    parser.add_argument("--no-auto-start", action="store_true", help="Open the dashboard without starting the bot.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        import webview  # type: ignore
    except Exception as exc:
        show_error(f"桌面 WebView 组件不可用：{exc}")
        return 1

    root = runtime_root()
    config = Path(args.config)
    config = config if config.is_absolute() else (root / config).resolve()
    if not config.is_file():
        show_error(f"找不到配置文件：{config}")
        return 1

    port = available_port()
    url = f"http://127.0.0.1:{port}"
    log_path = root / "desktop-dashboard.log"
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with log_path.open("a", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, "dashboard", "--config", str(config), "--host", "127.0.0.1", "--port", str(port)],
            cwd=root,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
        )
        try:
            wait_for_dashboard(url, process)
            password = os.getenv("QJD67_PAY_PASSWORD", "").strip()
            if password:
                request_json(f"{url}/api/password", {"password": password})
            if not args.no_auto_start:
                request_json(f"{url}/api/bot/start", {})
            window = webview.create_window("NO 钱包自动化", url, width=1180, height=860, min_size=(960, 680))
            webview.start()
            return 0
        except Exception as exc:
            show_error(str(exc))
            return 1
        finally:
            try:
                request_json(f"{url}/api/bot/stop", {})
            except Exception:
                pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
