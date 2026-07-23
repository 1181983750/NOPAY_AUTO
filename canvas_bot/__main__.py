from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]


def now_ms() -> int:
    return int(time.time() * 1000)


def resolve_path(value: str | Path, base: Path = ROOT) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    path = Path(expanded)
    if path.is_absolute():
        return path
    return (base / path).resolve()


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_config_path"] = str(path.resolve())
    return cfg


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def split_terms(value: Iterable[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = re.split(r"[,，|;；\n]+", value)
    else:
        raw = list(value)
    return [normalize_text(item) for item in raw if normalize_text(item)]


def text_has_any(text: str, terms: Iterable[str] | str | None) -> bool:
    normalized = normalize_text(text)
    return any(term in normalized for term in split_terms(terms))


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def is_chrome_running() -> bool:
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return False
    return "chrome.exe" in result.stdout.lower()


class PushPlusNotifier:
    """Best-effort low-frequency notifications through the PushPlus WeChat channel."""

    endpoint = "https://www.pushplus.plus/send/"

    def __init__(self) -> None:
        self.token = str(os.getenv("QJD67_PUSHPLUS_TOKEN") or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def send(self, title: str, content: str) -> str:
        if not self.enabled:
            return "disabled"
        payload = json.dumps(
            {
                "token": self.token,
                "title": title,
                "content": content,
                "template": "txt",
                "channel": "wechat",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=8) as response:  # noqa: S310 - configured public HTTPS endpoint
            return response.read().decode("utf-8", errors="replace")


@dataclass
class Rect:
    left: float
    top: float
    width: float
    height: float
    source: str = "unknown"

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def bottom(self) -> float:
        return self.top + self.height

    def point(self, rx: float, ry: float) -> tuple[float, float]:
        return self.left + self.width * rx, self.top + self.height * ry


@dataclass
class TextBox:
    text: str
    confidence: float
    left: float
    top: float
    right: float
    bottom: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.left + self.right) / 2, (self.top + self.bottom) / 2

    @property
    def normalized(self) -> str:
        return normalize_text(self.text)


@dataclass
class TemplateMatch:
    name: str
    confidence: float
    left: float
    top: float
    width: float
    height: float

    @property
    def center(self) -> tuple[float, float]:
        return self.left + self.width / 2, self.top + self.height / 2


class VisionEngine:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.templates_dir = resolve_path(cfg.get("templates_dir", "templates"))
        self.ocr = None
        self.ocr_name = "disabled"
        self.cv2 = None
        self._init_cv2()
        self._init_ocr()

    def _init_cv2(self) -> None:
        try:
            import cv2  # type: ignore

            self.cv2 = cv2
        except Exception:
            self.cv2 = None

    def _init_ocr(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore

            self.ocr = RapidOCR()
            self.ocr_name = "rapidocr-onnxruntime"
        except Exception:
            self.ocr = None
            self.ocr_name = "disabled"

    def read_text(self, image: Image.Image) -> list[TextBox]:
        if not self.ocr:
            return []

        arr = np.array(image.convert("RGB"))
        try:
            result, _ = self.ocr(arr)
        except Exception as exc:
            print(f"[vision] OCR failed: {exc}")
            return []

        boxes: list[TextBox] = []
        for item in result or []:
            try:
                points, text, score = item
                xs = [float(point[0]) for point in points]
                ys = [float(point[1]) for point in points]
                boxes.append(
                    TextBox(
                        text=str(text),
                        confidence=float(score),
                        left=min(xs),
                        top=min(ys),
                        right=max(xs),
                        bottom=max(ys),
                    )
                )
            except Exception:
                continue
        return boxes

    def find_text(
        self,
        boxes: list[TextBox],
        terms: Iterable[str] | str,
        *,
        min_confidence: float = 0.45,
        max_text_length: int | None = None,
        exclude: Iterable[str] | str | None = None,
        prefer_bottom: bool = False,
        prefer_right: bool = False,
        prefer_center: bool = False,
        bounds: tuple[float, float, float, float] | None = None,
    ) -> TextBox | None:
        needles = split_terms(terms)
        excludes = split_terms(exclude)
        best: tuple[float, TextBox] | None = None

        for box in boxes:
            if box.confidence < min_confidence:
                continue
            normalized = box.normalized
            if not normalized:
                continue
            if excludes and any(term in normalized for term in excludes):
                continue
            if max_text_length and len(normalized) > max_text_length:
                continue
            if not any(term and term in normalized for term in needles):
                continue

            cx, cy = box.center
            if bounds:
                left, top, right, bottom = bounds
                if cx < left or cx > right or cy < top or cy > bottom:
                    continue

            score = 100.0 * box.confidence
            if any(normalized == term for term in needles):
                score += 80
            elif any(normalized.startswith(term) or normalized.endswith(term) for term in needles):
                score += 40
            if prefer_bottom:
                score += cy / 12
            if prefer_right:
                score += cx / 18
            if prefer_center:
                score -= abs(cx - 0.5) / 25

            if best is None or score > best[0]:
                best = (score, box)

        return best[1] if best else None

    def find_template(
        self,
        image: Image.Image,
        name: str,
        *,
        threshold: float = 0.86,
    ) -> TemplateMatch | None:
        if not self.cv2:
            return None

        template_paths = []
        direct = self.templates_dir / f"{name}.png"
        if direct.exists():
            template_paths.append(direct)
        folder = self.templates_dir / name
        if folder.exists():
            template_paths.extend(sorted(folder.glob("*.png")))

        if not template_paths:
            return None

        cv2 = self.cv2
        haystack = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
        best: TemplateMatch | None = None

        for path in template_paths:
            template = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if template is None:
                continue
            th, tw = template.shape[:2]
            if th > haystack.shape[0] or tw > haystack.shape[1]:
                continue
            result = cv2.matchTemplate(haystack, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            if max_val < threshold:
                continue
            match = TemplateMatch(
                name=name,
                confidence=float(max_val),
                left=float(max_loc[0]),
                top=float(max_loc[1]),
                width=float(tw),
                height=float(th),
            )
            if best is None or match.confidence > best.confidence:
                best = match

        return best

    def save_debug(
        self,
        image: Image.Image,
        boxes: list[TextBox],
        path: Path,
        *,
        note: str = "",
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        out = image.convert("RGB")
        draw = ImageDraw.Draw(out)
        for box in boxes:
            draw.rectangle((box.left, box.top, box.right, box.bottom), outline=(255, 80, 80), width=2)
            label = f"{box.text} {box.confidence:.2f}"
            draw.text((box.left, max(0, box.top - 14)), label, fill=(255, 80, 80))
        if note:
            draw.text((8, 8), note, fill=(20, 180, 20))
        out.save(path)


class BrowserSession:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.playwright = None
        self.context = None
        self.page = None

    async def __aenter__(self) -> "BrowserSession":
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except Exception as exc:
            raise RuntimeError("Playwright is not installed. Run: uv sync") from exc

        self.playwright = await async_playwright().start()
        profile_cfg = self.cfg.get("browser_profile", {})
        profile_mode = str(profile_cfg.get("mode", "managed")).lower()
        use_system_profile = profile_mode in {"system", "user", "user_profile", "system_profile"}

        if use_system_profile:
            user_data_dir = resolve_path(
                profile_cfg.get("system_user_data_dir")
                or profile_cfg.get("user_data_dir")
                or "%LOCALAPPDATA%\\Google\\Chrome\\User Data"
            )
        else:
            user_data_dir = resolve_path(self.cfg.get("user_data_dir", ".browser-profile/qjd67"))
        user_data_dir.mkdir(parents=True, exist_ok=True)

        # ``null`` opts into Chrome's native window viewport. Keep a fixed
        # viewport available for reproducible OCR debugging when needed.
        viewport_cfg = self.cfg.get("viewport", {"width": 1280, "height": 900})
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=Translate",
        ]
        profile_directory = str(profile_cfg.get("profile_directory") or "").strip()
        if use_system_profile and profile_directory:
            args.append(f"--profile-directory={profile_directory}")

        launch_args: dict[str, Any] = {
            "headless": bool(self.cfg.get("headless", False)),
            "args": args,
            "chromium_sandbox": bool(profile_cfg.get("chromium_sandbox", True)),
        }
        if viewport_cfg is None:
            launch_args["no_viewport"] = True
        else:
            launch_args["viewport"] = viewport_cfg
        if profile_cfg.get("hide_automation_banner", True):
            launch_args["ignore_default_args"] = ["--enable-automation"]

        chrome_path = self.cfg.get("chrome_path")
        if chrome_path and Path(chrome_path).exists():
            launch_args["executable_path"] = chrome_path
        else:
            launch_args["channel"] = "chrome"

        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                **launch_args,
            )
        except Exception as exc:
            if use_system_profile:
                raise RuntimeError(
                    "无法使用系统 Chrome profile。请先关闭所有 Chrome 窗口后重试；"
                    "如果你不想关闭 Chrome，把 configs/qjd67.json 里的 "
                    'browser_profile.mode 改成 "managed"。'
                ) from exc
            raise
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        # Some versions of the target page keep requests open indefinitely, so
        # waiting for DOMContentLoaded can leave a blank browser after the
        # default Playwright navigation timeout. The canvas selector below is
        # the meaningful readiness check for this bot.
        await self.page.goto(
            self.cfg["url"],
            wait_until="commit",
            timeout=int(float(self.cfg.get("navigation_timeout_sec", 30)) * 1000),
        )
        await self.page.wait_for_timeout(int(float(self.cfg.get("startup_wait_sec", 3)) * 1000))
        try:
            await self.page.wait_for_selector(
                "flutter-view, flt-glass-pane, canvas",
                timeout=int(float(self.cfg.get("canvas_wait_sec", 15)) * 1000),
            )
            await self.page.wait_for_timeout(800)
        except Exception:
            pass
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()

    def detect_visual_app_rect(self, image: Image.Image, parent: Rect) -> Rect | None:
        visual_cfg = self.cfg.get("visual_app_rect", {})
        if not visual_cfg.get("enabled", True):
            return None

        arr = np.array(image.convert("RGB")).astype(np.int16)
        height, width = arr.shape[:2]
        if width <= int(visual_cfg.get("max_dom_width", 700)):
            return None

        patch = max(8, min(32, width // 20, height // 20))
        corners = np.concatenate(
            [
                arr[:patch, :patch].reshape(-1, 3),
                arr[:patch, -patch:].reshape(-1, 3),
                arr[-patch:, :patch].reshape(-1, 3),
                arr[-patch:, -patch:].reshape(-1, 3),
            ],
            axis=0,
        )
        background = np.median(corners, axis=0)
        diff = np.sqrt(((arr - background) ** 2).sum(axis=2))
        mask = diff > float(visual_cfg.get("background_threshold", 18))

        col_ratio = mask.mean(axis=0)
        candidates = np.where(col_ratio > 0.04)[0]
        if candidates.size == 0:
            return None

        segments: list[tuple[int, int]] = []
        start = int(candidates[0])
        last = int(candidates[0])
        for col in candidates[1:]:
            col = int(col)
            if col <= last + 2:
                last = col
            else:
                segments.append((start, last))
                start = last = col
        segments.append((start, last))

        min_width = int(visual_cfg.get("min_width", 240))
        segments = [(left, right) for left, right in segments if right - left + 1 >= min_width]
        if not segments:
            return None

        left, right = max(segments, key=lambda item: item[1] - item[0])
        inner = mask[:, left : right + 1]
        row_ratio = inner.mean(axis=1)
        rows = np.where(row_ratio > 0.04)[0]
        if rows.size:
            top = int(rows[0])
            bottom = int(rows[-1])
        else:
            top = 0
            bottom = height - 1

        pad = int(visual_cfg.get("padding", 0))
        left = max(0, left - pad)
        right = min(width - 1, right + pad)
        top = max(0, top - pad)
        bottom = min(height - 1, bottom + pad)

        return Rect(
            left=parent.left + left,
            top=parent.top + top,
            width=right - left + 1,
            height=bottom - top + 1,
            source=f"{parent.source}:visual",
        )

    async def dom_app_rect(self) -> Rect:
        assert self.page is not None
        data = await self.page.evaluate(
            """() => {
              const candidates = [];
              const add = (el, weight) => {
                if (!el) return;
                const r = el.getBoundingClientRect();
                if (!r || r.width < 180 || r.height < 300) return;
                const ratio = r.width / Math.max(1, r.height);
                const phoneScore = ratio >= 0.38 && ratio <= 0.80 ? 45 : 0;
                const widthScore = r.width <= 620 ? 35 : -Math.min(60, (r.width - 620) / 8);
                const score = weight + phoneScore + widthScore + Math.min(20, r.height / 70);
                candidates.push({ left: r.left, top: r.top, width: r.width, height: r.height, source: el.tagName, score });
              };
              for (const selector of ['flutter-view', 'flt-glass-pane', 'canvas']) {
                document.querySelectorAll(selector).forEach((el) => add(el, selector === 'canvas' ? 8 : 20));
              }
              candidates.sort((a, b) => b.score - a.score);
              if (candidates[0]) return candidates[0];
              return { left: 0, top: 0, width: Math.min(480, window.innerWidth), height: window.innerHeight, source: 'fallback' };
            }"""
        )
        return Rect(
            left=float(data["left"]),
            top=float(data["top"]),
            width=float(data["width"]),
            height=float(data["height"]),
            source=str(data.get("source", "unknown")),
        )

    async def app_rect(self) -> Rect:
        rect = await self.dom_app_rect()
        visual_cfg = self.cfg.get("visual_app_rect", {})
        if not visual_cfg.get("enabled", True):
            return rect
        if rect.width <= float(visual_cfg.get("max_dom_width", 700)):
            return rect
        image = await self.screenshot_app(rect)
        visual_rect = self.detect_visual_app_rect(image, rect)
        return visual_rect or rect

    async def diagnostics(self) -> dict[str, Any]:
        assert self.page is not None
        return await self.page.evaluate(
            """() => ({
              url: location.href,
              title: document.title,
              readyState: document.readyState,
              bodyText: (document.body && document.body.innerText || '').slice(0, 300),
              canvasCount: document.querySelectorAll('canvas').length,
              flutterViewCount: document.querySelectorAll('flutter-view').length,
              glassPaneCount: document.querySelectorAll('flt-glass-pane').length,
              htmlLength: document.documentElement ? document.documentElement.outerHTML.length : 0
            })"""
        )

    async def screenshot_app(self, rect: Rect) -> Image.Image:
        assert self.page is not None
        raw = await self.page.screenshot(
            full_page=False,
            scale="css",
            clip={
                "x": max(0, rect.left),
                "y": max(0, rect.top),
                "width": max(1, rect.width),
                "height": max(1, rect.height),
            },
        )
        return Image.open(io.BytesIO(raw)).convert("RGB")

    async def reload(self) -> None:
        """Reload the current Flutter page without following or clicking any modal action."""
        assert self.page is not None
        await self.page.reload(wait_until="domcontentloaded", timeout=15000)
        await self.page.wait_for_timeout(800)

    async def click_abs(self, x: float, y: float) -> None:
        assert self.page is not None
        await self.page.mouse.move(x, y)
        await self.page.mouse.down()
        await self.page.mouse.up()

    async def click_relative(self, rect: Rect, rx: float, ry: float) -> None:
        x, y = rect.point(rx, ry)
        await self.click_abs(x, y)

    async def wheel_relative(self, rect: Rect, rx: float, ry: float, delta_y: float) -> None:
        assert self.page is not None
        x, y = rect.point(rx, ry)
        await self.page.mouse.move(x, y)
        await self.page.mouse.wheel(0, delta_y)

    async def drag_relative(
        self,
        rect: Rect,
        start_rx: float,
        start_ry: float,
        end_rx: float,
        end_ry: float,
    ) -> None:
        assert self.page is not None
        start_x, start_y = rect.point(start_rx, start_ry)
        end_x, end_y = rect.point(end_rx, end_ry)
        await self.page.mouse.move(start_x, start_y)
        await self.page.mouse.down()
        await self.page.mouse.move(end_x, end_y, steps=12)
        await self.page.mouse.up()

    async def replace_text(self, text: str) -> None:
        assert self.page is not None
        await self.page.keyboard.press("Control+A")
        await self.page.keyboard.press("Backspace")
        await self.page.keyboard.insert_text(str(text))

    async def replace_numeric(self, text: str) -> None:
        """Enter numeric Flutter canvas fields with key events, not text injection."""
        assert self.page is not None
        await self.page.keyboard.press("Control+A")
        await self.page.keyboard.press("Backspace")
        for char in str(text):
            if char.isdigit() or char == ".":
                await self.page.keyboard.press(char)
                await asyncio.sleep(0.04)

    async def press(self, key: str) -> None:
        assert self.page is not None
        await self.page.keyboard.press(key)


class CanvasBot:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.vision = VisionEngine(cfg)
        self.pushplus = PushPlusNotifier()
        self.notified_events: set[str] = set()
        self.pending_since = 0.0
        self.form_expected_after = 0.0
        self.form_expected_until = 0.0
        self.next_post_after = 0.0
        self.last_action_at = 0.0
        self.last_start_at = 0.0
        self.last_form_submit_at = 0.0
        self.last_accept_at = 0.0
        self.last_waiting_transfer_back_at = 0.0
        self.last_post_accept_detail_back_at = 0.0
        self.last_exception_ignore_at = 0.0
        self.post_accept_order_check = ""
        self.post_accept_order_check_until = 0.0
        self.last_cancel_at = 0.0
        self.cancel_clicked_at = 0.0
        self.last_cancel_confirm_at = 0.0
        self.cancel_confirm_clicks = 0
        self.move_page_opened = False
        self.payment_selected_for_form = False
        self.form_submission_failed = False
        self.form_interrupted_by_trade_notification = False
        self.last_form_interruption_back_at = 0.0
        self.accept_confirm_until = 0.0
        self.cancel_confirm_until = 0.0
        self.multi_pending_since: list[float] = []
        self.multi_batch_complete = False
        self.multi_payment_index = 0
        self.form_values: dict[str, str] = {}
        self.cancelling_multi_order = False
        self.multi_cancel_scroll_steps = 0
        self.multi_cancel_recovering = False
        self.last_visual_page = ""
        self.last_orders_refresh_at = 0.0
        self.unknown_page_streak = 0
        self.last_unknown_page_reload_at = 0.0
        self.last_password_input_at = 0.0

    async def notify_once(self, event: str, title: str, content: str) -> None:
        """Send one notification per event per run; notification failures never stop the bot."""
        if event in self.notified_events or not self.pushplus.enabled:
            return
        self.notified_events.add(event)
        try:
            result = await asyncio.to_thread(self.pushplus.send, title, content)
            print(f"[notify] PushPlus 已提交：{event} {result[:120]}")
        except Exception as exc:
            print(f"[notify] PushPlus 发送失败：{event} {exc}")

    @property
    def texts(self) -> dict[str, list[str]]:
        return self.cfg.get("texts", {})

    @property
    def points(self) -> dict[str, Any]:
        return self.cfg.get("points", {})

    @property
    def debug_dir(self) -> Path:
        return resolve_path(self.cfg.get("debug", {}).get("dir", "debug-screenshots"))

    def point(self, key: str) -> tuple[float, float]:
        value = self.points[key]
        return float(value[0]), float(value[1])

    def password(self) -> str:
        password_cfg = self.cfg.get("password", {})
        env_name = password_cfg.get("env") or "QJD67_PAY_PASSWORD"
        return str(os.getenv(env_name) or password_cfg.get("value") or "")

    def dashboard_password(self) -> str:
        """Read the current in-memory password from the local dashboard on demand."""
        base_url = str(os.getenv("QJD67_DASHBOARD_URL") or "").rstrip("/")
        token = str(os.getenv("QJD67_DASHBOARD_TOKEN") or "")
        if not base_url or not token:
            return ""
        request = urllib.request.Request(
            f"{base_url}/api/internal/password",
            headers={"X-Dashboard-Token": token},
        )
        try:
            with urllib.request.urlopen(request, timeout=1.2) as response:  # noqa: S310 - loopback token endpoint
                value = json.loads(response.read().decode("utf-8")).get("password", "")
        except (OSError, ValueError, urllib.error.URLError):
            return ""
        password = str(value).strip()
        return password if len(password) == 6 and password.isdigit() else ""

    async def resolve_payment_password(self) -> str:
        password = self.password()
        if password:
            return password
        return await asyncio.to_thread(self.dashboard_password)

    def password_cfg(self) -> dict[str, Any]:
        return self.cfg.get("password", {})

    def multi_order_cfg(self) -> dict[str, Any]:
        return self.cfg.get("multi_order", {})

    def multi_order_enabled(self) -> bool:
        return bool(self.multi_order_cfg().get("enabled", False))

    def multi_cancel_search_max_steps(self) -> int:
        """Bound a visual tail scan without ever treating a visible row as the tail."""
        try:
            configured = int(self.multi_order_cfg().get("cancel_search_max_steps", 40))
        except (TypeError, ValueError):
            configured = 40
        return max(1, min(configured, 120))

    def unknown_page_refresh_cfg(self) -> tuple[int, float]:
        cfg = self.cfg.get("unknown_page_refresh", {})
        try:
            scans = int(cfg.get("consecutive_scans", 6))
        except (TypeError, ValueError):
            scans = 6
        try:
            cooldown = float(cfg.get("cooldown_sec", 20))
        except (TypeError, ValueError):
            cooldown = 20.0
        return max(2, min(scans, 30)), max(5.0, cooldown)

    def payment_keywords(self) -> list[str]:
        multi_keywords = self.multi_order_cfg().get("payment_keywords")
        payment = self.cfg.get("payment", {})
        raw = multi_keywords or payment.get("keywords") or payment.get("keyword") or ""
        if isinstance(raw, str):
            values = re.split(r"[,，;；\n]+", raw)
        else:
            values = list(raw)
        return [str(value).strip() for value in values if str(value).strip()]

    def current_payment_keyword(self) -> str:
        keywords = self.payment_keywords()
        if not keywords:
            return ""
        return keywords[self.multi_payment_index % len(keywords)]

    @staticmethod
    def parse_money(value: str) -> Decimal | None:
        match = re.search(r"\d[\d,]*(?:\.\d{1,2})?", str(value or ""))
        if not match:
            return None
        try:
            money = Decimal(match.group(0).replace(",", ""))
        except InvalidOperation:
            return None
        return money if money.is_finite() and money >= 0 else None

    @staticmethod
    def decimal_text(value: Decimal) -> str:
        return format(value.quantize(Decimal("0.01")).normalize(), "f")

    def find_available_balance(self, image: Image.Image, boxes: list[TextBox]) -> Decimal | None:
        """Read the top-of-page 搬砖余额, never an amount from a historical order row."""
        width, height = image.size
        label = self.vision.find_text(
            boxes,
            self.texts.get("balance", ["搬砖余额"]),
            max_text_length=24,
            bounds=(0, 0, width * 0.75, height * 0.42),
        )
        if not isinstance(label, TextBox):
            return None
        candidates: list[tuple[float, Decimal]] = []
        for box in boxes:
            money = self.parse_money(box.text)
            if money is None:
                continue
            cx, cy = box.center
            # Keep the search tightly under the top balance label. Lower dashboard counters
            # such as “我的下级 4” are otherwise valid numbers but not available balance.
            if cy < label.top - height * 0.03 or cy > label.bottom + height * 0.13:
                continue
            if cx < label.left - width * 0.08 or cx > width * 0.78:
                continue
            distance = abs(cx - label.left) / width + abs(cy - label.bottom) / height
            candidates.append((distance, money))
        # The balance is the largest monetary value in this small, label-bound region.
        return max(candidates, default=(0.0, None), key=lambda item: item[1])[1]

    def prepare_multi_order_values(self, balance: Decimal) -> bool:
        """Plan one order from the currently visible balance without exceeding it."""
        try:
            configured_amount = Decimal(str(self.cfg.get("amount") or ""))
        except InvalidOperation:
            configured_amount = Decimal(0)
        if configured_amount <= 0:
            print("[warn] amount 配置无效，停止多挂单。")
            self.multi_batch_complete = True
            return False
        limit_min = Decimal(str(self.cfg.get("limit_min") or "0"))
        if balance < limit_min:
            self.multi_batch_complete = True
            self.form_values = {}
            print(f"[state] 搬砖余额 {self.decimal_text(balance)} 小于最小限额 {self.decimal_text(limit_min)}，停止新增挂单。")
            return False
        amount = min(configured_amount, balance)
        limit_max = Decimal(str(self.cfg.get("limit_max") or amount))
        if amount < limit_min:
            self.multi_batch_complete = True
            self.form_values = {}
            print(f"[state] 本次可挂金额 {self.decimal_text(amount)} 小于最小限额，停止新增挂单。")
            return False
        # The app accepts cents for the total amount, but its per-order limit
        # inputs reject decimal values. Round down only the maximum limit so it
        # remains valid and never exceeds the total order amount.
        limit_max = min(limit_max, amount).to_integral_value(rounding=ROUND_DOWN)
        if limit_max < limit_min:
            self.multi_batch_complete = True
            self.form_values = {}
            print("[state] 当前余额不足以满足最小限额，停止新增挂单。")
            return False
        self.form_values = {
            "amount": self.decimal_text(amount),
            "limit_min": self.decimal_text(limit_min),
            "limit_max": self.decimal_text(limit_max),
            # When the remaining balance is less than the configured amount,
            # Flutter accepts the exact value more reliably through its 全部 button.
            "use_all_balance": "true" if balance < configured_amount else "false",
        }
        return True

    def can_act(self) -> bool:
        throttle = float(self.cfg.get("action_throttle_sec", 1.2))
        return time.time() - self.last_action_at >= max(0.7, throttle)

    def record_action(self, message: str) -> None:
        self.last_action_at = time.time()
        print(f"[action] {message}")

    def all_text(self, boxes: list[TextBox]) -> str:
        return "|".join(box.normalized for box in boxes)

    def has_dangerous_confirm(self, boxes: list[TextBox]) -> bool:
        return text_has_any(self.all_text(boxes), self.texts.get("dangerous_confirm", []))

    def is_waiting_transfer(self, image: Image.Image, boxes: list[TextBox]) -> bool:
        if self.vision.find_template(image, "wait_transfer"):
            return True
        return bool(
            self.vision.find_text(
                boxes,
                self.texts.get("wait_transfer", ["待付款", "请耐心等待买家转账", "等待转账"]),
                max_text_length=42,
            )
        )

    def is_waiting_transfer_detail(self, image: Image.Image, boxes: list[TextBox]) -> bool:
        if not self.is_waiting_transfer(image, boxes):
            return False
        width, height = image.size
        return bool(
            self.vision.find_text(
                boxes,
                self.texts.get("order_detail", ["出售详情"]),
                max_text_length=20,
                bounds=(width * 0.20, 0, width * 0.80, height * 0.16),
            )
        )

    def is_order_detail_interrupt_page(self, image: Image.Image, boxes: list[TextBox]) -> bool:
        """Recognize a buyer/order detail page that interrupts a posting form mid-flow."""
        width, height = image.size
        detail_terms = list(self.texts.get("order_detail", ["出售详情"]))
        detail_terms.extend(self.texts.get("buyer_detail", ["买家详情", "订单详情"]))
        return bool(
            self.vision.find_text(
                boxes,
                detail_terms,
                max_text_length=20,
                bounds=(width * 0.20, 0, width * 0.80, height * 0.16),
            )
        )

    def find_trade_notification(self, image: Image.Image, boxes: list[TextBox]) -> TextBox | TemplateMatch | None:
        """Find the incoming-order modal that can appear above any other page."""
        template = self.vision.find_template(image, "trade_notification")
        if template:
            return template
        width, height = image.size
        return self.vision.find_text(
            boxes,
            self.texts.get("trade_notification", ["交易通知"]),
            prefer_center=True,
            max_text_length=20,
            bounds=(width * 0.12, height * 0.10, width * 0.88, height * 0.48),
        )

    def find_order_exception_dialog(self, image: Image.Image, boxes: list[TextBox]) -> TextBox | TemplateMatch | None:
        """Recognize the timeout/order-exception reminder modal."""
        template = self.vision.find_template(image, "order_exception")
        if template:
            return template
        width, height = image.size
        return self.vision.find_text(
            boxes,
            self.texts.get("order_exception", ["订单异常提醒"]),
            prefer_center=True,
            max_text_length=24,
            bounds=(width * 0.12, height * 0.16, width * 0.88, height * 0.68),
        )

    def find_order_exception_ignore(self, image: Image.Image, boxes: list[TextBox]) -> TextBox | TemplateMatch | None:
        template = self.vision.find_template(image, "order_exception_ignore")
        if template:
            return template
        width, height = image.size
        return self.vision.find_text(
            boxes,
            self.texts.get("order_exception_ignore", ["忽略"]),
            prefer_bottom=True,
            max_text_length=12,
            bounds=(width * 0.08, height * 0.45, width * 0.54, height * 0.94),
        )

    def find_accept_confirm_target(self, image: Image.Image, boxes: list[TextBox]) -> TextBox | TemplateMatch | None:
        """Only accept a fresh, explicit 接单 confirmation; never reuse a form button underneath it."""
        template = self.vision.find_template(image, "accept_confirm")
        if template:
            return template
        width, height = image.size
        return self.vision.find_text(
            boxes,
            self.texts.get("accept_confirm", ["确认同意", "确认接单", "确定接单"]),
            prefer_center=True,
            prefer_bottom=True,
            max_text_length=16,
            exclude=self.texts.get("dangerous_confirm", []),
            bounds=(width * 0.12, height * 0.25, width * 0.88, height * 0.92),
        )

    def cancel_flow_cfg(self) -> dict[str, float | int]:
        cfg = self.cfg.get("cancel_flow", {})
        return {
            "confirm_delay_sec": float(cfg.get("confirm_delay_sec", 1.2)),
            "retry_interval_sec": float(cfg.get("retry_interval_sec", 2.0)),
            "confirm_timeout_sec": float(cfg.get("confirm_timeout_sec", 8.0)),
            "max_confirm_clicks": int(cfg.get("max_confirm_clicks", 1)),
        }

    def submit_flow_cfg(self) -> dict[str, float]:
        cfg = self.cfg.get("submit_flow", {})
        return {
            "secondary_confirm_delay_sec": float(cfg.get("secondary_confirm_delay_sec", 1.0)),
            "secondary_confirm_timeout_sec": float(cfg.get("secondary_confirm_timeout_sec", 7.0)),
            "password_delay_sec": float(cfg.get("password_delay_sec", 0.7)),
            "password_timeout_sec": float(cfg.get("password_timeout_sec", 8.0)),
            "return_to_start_timeout_sec": float(cfg.get("return_to_start_timeout_sec", 8.0)),
            "state_poll_sec": float(cfg.get("state_poll_sec", 0.35)),
        }

    def keyword_groups(self, value: str) -> list[list[str]]:
        groups: list[list[str]] = []
        for group in re.split(r"[,，|;；\n]+", str(value or "")):
            terms = [normalize_text(item) for item in re.split(r"[\s+&＆]+", group) if normalize_text(item)]
            if terms:
                groups.append(terms)
        return groups

    def text_matches_keyword_groups(self, text: str, groups: list[list[str]]) -> bool:
        normalized = normalize_text(text)
        return any(all(term in normalized for term in group) for group in groups)

    def is_order_form_visible(self, image: Image.Image, boxes: list[TextBox]) -> bool:
        if self.vision.find_template(image, "order_form"):
            return True
        amount = self.vision.find_text(boxes, self.texts.get("amount", []), max_text_length=42)
        submit = self.vision.find_text(boxes, self.texts.get("submit", []), max_text_length=20, prefer_bottom=True)
        limit = self.vision.find_text(boxes, self.texts.get("limit", []), max_text_length=36)
        return bool(amount or (submit and limit))

    def find_start_order_target(self, image: Image.Image, boxes: list[TextBox]) -> TextBox | TemplateMatch | None:
        template = self.vision.find_template(image, "start_order")
        if template:
            return template
        width, height = image.size
        return self.vision.find_text(
            boxes,
            self.texts.get("start", []),
            prefer_right=True,
            prefer_bottom=True,
            max_text_length=24,
            bounds=(width * 0.50, height * 0.05, width * 0.98, height * 0.55),
        )

    def find_move_page_tab(self, image: Image.Image, boxes: list[TextBox]) -> TextBox | TemplateMatch | None:
        template = self.vision.find_template(image, "move_page_tab")
        if template:
            return template
        width, height = image.size
        return self.vision.find_text(
            boxes,
            self.texts.get("move_page_tab", ["搬砖"]),
            prefer_center=True,
            max_text_length=12,
            bounds=(width * 0.12, 0, width * 0.62, height * 0.15),
        )

    def find_order_page_tab(self, image: Image.Image, boxes: list[TextBox]) -> TextBox | TemplateMatch | None:
        template = self.vision.find_template(image, "order_page_tab")
        if template:
            return template
        width, height = image.size
        return self.vision.find_text(
            boxes,
            self.texts.get("order_page_tab", ["订单"]),
            prefer_center=True,
            max_text_length=12,
            bounds=(width * 0.42, 0, width * 0.88, height * 0.15),
        )

    def is_empty_orders_page(self, image: Image.Image, boxes: list[TextBox]) -> bool:
        """Recognize the 订单 > 进行中 empty state before attempting to read 搬砖余额."""
        width, height = image.size
        empty = self.vision.find_text(
            boxes,
            self.texts.get("order_empty", ["暂无订单", "暂无数据", "没有订单"]),
            max_text_length=24,
            bounds=(width * 0.16, height * 0.18, width * 0.84, height * 0.84),
        )
        progress_tab = self.vision.find_text(
            boxes,
            ["进行中"],
            max_text_length=12,
            bounds=(0, height * 0.06, width * 0.34, height * 0.22),
        )
        return bool(empty and progress_tab)

    def is_orders_page(self, image: Image.Image, boxes: list[TextBox]) -> bool:
        """Recognize the orders list by its top navigation and order-state tabs."""
        width, height = image.size
        order_tab = self.find_order_page_tab(image, boxes)
        state_tab = self.vision.find_text(
            boxes,
            ["进行中", "成功", "失败/取消", "全部"],
            max_text_length=12,
            bounds=(0, height * 0.06, width * 0.95, height * 0.23),
        )
        return bool(order_tab and state_tab)

    def is_auth_page(self, image: Image.Image, boxes: list[TextBox]) -> bool:
        """Recognize the site's login/register screen and wait for manual login."""
        width, height = image.size
        auth_terms = self.vision.find_text(
            boxes,
            ["注册", "登录", "请输入手机号", "请输入验证码", "手机号"],
            max_text_length=24,
            bounds=(width * 0.20, height * 0.10, width * 0.90, height * 0.90),
        )
        return bool(auth_terms)

    def classify_visual_page(self, image: Image.Image, boxes: list[TextBox]) -> str:
        """Classify the visible Flutter page before deciding which actions are allowed."""
        if self.find_trade_notification(image, boxes):
            return "trade_notification"
        if self.find_order_exception_dialog(image, boxes):
            return "order_exception_dialog"
        if self.find_password_dialog(image, boxes):
            return "password_dialog"
        if getattr(self, "cancel_confirm_until", 0) and self.find_cancel_confirm_target(image, boxes):
            return "cancel_confirm_dialog"
        # A 订单 > 进行中 list can contain a row labelled 待付款. It is still a
        # list page (and must receive its periodic pull-to-refresh), not the
        # full-screen 出售详情 waiting page.
        if self.is_empty_orders_page(image, boxes):
            return "orders_empty"
        if self.is_auth_page(image, boxes):
            return "auth_page"
        if self.is_orders_page(image, boxes):
            return "orders_list"
        if self.is_waiting_transfer_detail(image, boxes):
            return "waiting_transfer_detail"
        if self.is_waiting_transfer(image, boxes):
            return "waiting_transfer"
        if self.is_order_form_visible(image, boxes):
            return "withdraw_form"
        if self.is_order_detail_interrupt_page(image, boxes):
            return "order_detail"
        if self.find_available_balance(image, boxes) is not None or self.find_start_order_target(image, boxes):
            return "move_page"
        if self.find_move_page_tab(image, boxes):
            return "navigation"
        return "unknown"

    def note_visual_page(self, page: str) -> None:
        if page == getattr(self, "last_visual_page", ""):
            return
        self.last_visual_page = page
        labels = {
            "password_dialog": "支付密码弹窗",
            "trade_notification": "交易通知",
            "order_exception_dialog": "订单异常提醒",
            "cancel_confirm_dialog": "取消确认弹窗",
            "waiting_transfer_detail": "待付款订单详情",
            "waiting_transfer": "待付款订单",
            "withdraw_form": "变现表单",
            "orders_empty": "空订单页",
            "order_detail": "订单详情",
            "orders_list": "订单列表",
            "move_page": "搬砖页",
            "navigation": "导航页",
            "auth_page": "登录/注册页",
            "unknown": "未知/加载页",
        }
        print(f"[page] 当前页面：{labels.get(page, page)}")

    def find_active_order_anchor(self, image: Image.Image, boxes: list[TextBox]) -> TextBox | TemplateMatch | None:
        template = self.vision.find_template(image, "active_order")
        if template:
            return template
        return self.vision.find_text(
            boxes,
            self.texts.get(
                "active_order",
                ["待付款", "待接单", "接单中", "待放行", "变现中", "进行中", "待成交", "已上架"],
            ),
            max_text_length=36,
            exclude=self.texts.get("done", []),
        )

    def find_password_dialog(self, image: Image.Image, boxes: list[TextBox]) -> TextBox | TemplateMatch | None:
        template = self.vision.find_template(image, "password_dialog")
        if template:
            return template
        return self.vision.find_text(
            boxes,
            self.texts.get("password_dialog", []),
            prefer_center=True,
            prefer_bottom=True,
            max_text_length=36,
        )

    def resolve_password_field_target(
        self,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> tuple[float, float] | None:
        """Locate the first six-digit password box from the dialog heading."""
        heading = self.find_password_dialog(image, boxes)
        if not heading:
            return None
        width, height = image.size
        heading_x, _ = heading.center
        x = heading_x - width * 0.31
        y = heading.bottom + clamp(height * 0.062, 46, 64)
        if x <= 12 or y >= height - 16:
            return None
        return x, y

    async def wait_for_visual_state(
        self,
        browser: BrowserSession,
        state: str,
        timeout_sec: float,
    ) -> tuple[Rect, Image.Image, list[TextBox]] | None:
        """Wait for a canvas-only state using fresh Playwright screenshots."""
        cfg = self.submit_flow_cfg()
        deadline = time.time() + max(0.1, timeout_sec)
        last_scan: tuple[Rect, Image.Image, list[TextBox]] | None = None

        while time.time() <= deadline:
            scan = await self.scan_app(browser)
            rect, image, boxes = scan
            last_scan = scan
            if self.find_trade_notification(image, boxes):
                print("[state] 提交等待期间检测到交易通知，优先处理来单。")
                await self.handle_incoming_order(browser, rect, image, boxes)
                return None
            if self.find_order_exception_dialog(image, boxes):
                print("[state] 提交等待期间检测到订单异常提醒，按忽略关闭。")
                await self.handle_order_exception_dialog(browser, rect, image, boxes)
                await asyncio.sleep(max(0.15, cfg["state_poll_sec"]))
                continue
            if state == "password" and self.find_password_dialog(image, boxes):
                print("[state] Playwright 检测到支付密码弹窗")
                return scan
            if state == "start" and self.find_start_order_target(image, boxes):
                print("[state] Playwright 检测到已返回开始搬砖页面")
                return scan
            if state == "form" and self.is_order_form_visible(image, boxes):
                print("[state] Playwright 检测到变现表单页面")
                return scan
            await asyncio.sleep(max(0.15, cfg["state_poll_sec"]))

        if last_scan:
            _, image, boxes = last_scan
            await self.save_action_debug(image, boxes, f"state_timeout_{state}")
        print(f"[warn] Playwright 未在 {timeout_sec:.1f}s 内检测到状态：{state}")
        return None

    def find_pending_anchor(self, image: Image.Image, boxes: list[TextBox]) -> TextBox | TemplateMatch | None:
        template = self.vision.find_template(image, "pending")
        if template:
            return template
        pending = self.vision.find_text(
            boxes,
            self.texts.get("pending", []),
            prefer_right=True,
            prefer_bottom=True,
            max_text_length=36,
            exclude=self.texts.get("done", []),
        )
        if pending:
            return pending
        return self.vision.find_text(
            boxes,
            self.texts.get("cancel", []),
            prefer_right=True,
            prefer_bottom=True,
            max_text_length=30,
            exclude=self.texts.get("done", []),
        )

    def find_bottom_cancel_target(self, image: Image.Image, boxes: list[TextBox]) -> TextBox | None:
        """Pick the lowest visible pending-order cancel button (oldest order in this UI)."""
        return self.vision.find_text(
            boxes,
            self.texts.get("cancel", []),
            prefer_right=True,
            prefer_bottom=True,
            max_text_length=16,
            exclude=self.texts.get("done", []),
        )

    def has_terminal_history_below(self, target: TextBox | TemplateMatch, boxes: list[TextBox]) -> bool:
        """A terminal row below Cancel marks the end of the active-order section."""
        if not isinstance(target, TextBox):
            return False
        terminal_terms = [
            term
            for term in self.texts.get("done", [])
            if normalize_text(term) not in {"已接单"}
        ]
        target_y = target.center[1]
        return any(
            text_has_any(box.text, terminal_terms) and box.center[1] > target_y + 8
            for box in boxes
        )

    def count_pending_orders(self, boxes: list[TextBox]) -> int:
        """Count visual pending-state labels so manual cancellations can clear local timers."""
        pending_terms = self.texts.get("pending", [])
        done_terms = self.texts.get("done", [])
        return sum(
            1
            for box in boxes
            if text_has_any(box.text, pending_terms) and not text_has_any(box.text, done_terms)
        )

    def clear_stale_pending_if_missing(self, anchor: TextBox | TemplateMatch | None, boxes: list[TextBox]) -> bool:
        """Forget an order only when the page explicitly shows a terminal state."""
        if not self.pending_since or anchor:
            return False
        done = self.vision.find_text(
            boxes,
            self.texts.get("done", []),
            max_text_length=24,
        )
        if not done:
            return False
        self.pending_since = 0
        return True

    def find_cancel_confirm_target(self, image: Image.Image, boxes: list[TextBox]) -> TextBox | TemplateMatch | None:
        template = self.vision.find_template(image, "cancel_confirm")
        if template:
            return template

        width, height = image.size
        return self.vision.find_text(
            boxes,
            self.texts.get("cancel_confirm", self.texts.get("confirm", [])),
            prefer_center=True,
            prefer_bottom=True,
            max_text_length=12,
            exclude=self.texts.get("dangerous_confirm", []),
            bounds=(width * 0.18, height * 0.32, width * 0.90, height * 0.74),
        )

    def find_payment_card_target(
        self,
        image: Image.Image,
        boxes: list[TextBox],
        keyword: str,
    ) -> TextBox | None:
        groups = self.keyword_groups(keyword)
        if not groups:
            return None

        width, height = image.size
        bounds = (width * 0.06, height * 0.10, width * 0.94, height * 0.97)
        excluded = ["收款方式", "请选择收款方式", "支付方式", "添加", "备注", "客服", "防骗", "骗子"]
        candidates: list[tuple[float, TextBox]] = []

        for box in boxes:
            normalized = box.normalized
            if not normalized or len(normalized) > 90:
                continue
            if any(term in normalized for term in map(normalize_text, excluded)):
                continue
            if not self.text_matches_keyword_groups(normalized, groups):
                continue
            cx, cy = box.center
            if cx < bounds[0] or cx > bounds[2] or cy < bounds[1] or cy > bounds[3]:
                continue
            score = 100 * box.confidence - max(0, len(normalized) - 12) + cy / max(1, height) * 12
            if any(normalized == "".join(group) for group in groups):
                score += 35
            candidates.append((score, box))

        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1] if candidates else None

    def find_first_payment_card_target(self, image: Image.Image, boxes: list[TextBox]) -> TextBox | None:
        keywords = "支付宝,微信,银行卡,收款码,转账,qq钱包,数字人民币,云闪付"
        groups = self.keyword_groups(keywords)
        width, height = image.size
        candidates: list[tuple[float, TextBox]] = []

        for box in boxes:
            normalized = box.normalized
            if not normalized or len(normalized) > 90:
                continue
            if "收款方式" in normalized or "添加" in normalized or "客服" in normalized:
                continue
            if not self.text_matches_keyword_groups(normalized, groups):
                continue
            cx, cy = box.center
            if cy < height * 0.10 or cy > height * 0.97 or cx < width * 0.06 or cx > width * 0.94:
                continue
            candidates.append((cy, box))

        candidates.sort(key=lambda item: item[0])
        return candidates[0][1] if candidates else None

    def find_bottom_action_target(
        self,
        image: Image.Image,
        boxes: list[TextBox],
        terms: Iterable[str] | str,
    ) -> TextBox | TemplateMatch | None:
        width, height = image.size
        return self.vision.find_text(
            boxes,
            terms,
            prefer_center=True,
            prefer_bottom=True,
            max_text_length=18,
            exclude=self.texts.get("dangerous_confirm", []),
            bounds=(width * 0.10, height * 0.54, width * 0.90, height * 0.99),
        )

    def resolve_canvas_field_target(
        self,
        image: Image.Image,
        boxes: list[TextBox],
        label_terms: Iterable[str] | str,
        x_ratio: float,
    ) -> tuple[float, float] | None:
        """Return the input point immediately below an OCR-visible form label."""
        label = self.vision.find_text(
            boxes,
            label_terms,
            max_text_length=24,
        )
        if not label:
            return None
        width, height = image.size
        input_y = label.bottom + clamp(height * 0.06, 36, 68)
        if input_y >= height - 18:
            return None
        return width * clamp(x_ratio, 0.08, 0.92), input_y

    def is_field_value_visible(
        self,
        boxes: list[TextBox],
        expected: str,
        target: tuple[float, float],
        image: Image.Image,
    ) -> bool:
        expected_digits = re.sub(r"\D", "", str(expected))
        if not expected_digits:
            return True
        target_x, target_y = target
        width, height = image.size
        for box in boxes:
            digits = re.sub(r"\D", "", box.text)
            if digits != expected_digits:
                continue
            x, y = box.center
            # Flutter inputs left-align their value, while the click target is centered.
            # Keep the vertical tolerance tight but accept the full input-row width.
            if abs(x - target_x) <= width * 0.48 and abs(y - target_y) <= height * 0.10:
                return True
        return False

    def find_payment_picker_entry(self, image: Image.Image, boxes: list[TextBox]) -> TextBox | TemplateMatch | None:
        template = self.vision.find_template(image, "payment_picker_entry")
        if template:
            return template
        width, height = image.size
        return self.vision.find_text(
            boxes,
            self.texts.get("payment_picker_entry", ["请选择收款方式"]),
            prefer_center=True,
            max_text_length=24,
            bounds=(width * 0.06, height * 0.10, width * 0.94, height * 0.55),
        )

    def is_payment_picker_visible(self, image: Image.Image, boxes: list[TextBox]) -> bool:
        if self.vision.find_template(image, "payment_picker"):
            return True
        width, height = image.size
        return bool(
            self.vision.find_text(
                boxes,
                self.texts.get("payment_picker", ["收款方式"]),
                min_confidence=0.55,
                max_text_length=24,
                bounds=(0, 0, width * 0.80, height * 0.18),
            )
        )

    async def click_visual_target(
        self,
        browser: BrowserSession,
        rect: Rect,
        target: TextBox | TemplateMatch,
    ) -> None:
        cx, cy = target.center
        await browser.click_abs(rect.left + cx, rect.top + cy)

    async def maybe_save_debug(
        self,
        image: Image.Image,
        boxes: list[TextBox],
        note: str = "",
        *,
        force: bool = False,
    ) -> None:
        debug = self.cfg.get("debug", {})
        if not force and not debug.get("save_each_tick") and not debug.get("save_on_action"):
            return
        if not force and not debug.get("save_each_tick"):
            return
        path = self.debug_dir / f"{int(time.time())}_{note or 'tick'}.png"
        if debug.get("draw_ocr", True):
            self.vision.save_debug(image, boxes, path, note=note)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            image.save(path)

    async def save_action_debug(self, image: Image.Image, boxes: list[TextBox], note: str) -> None:
        if not self.cfg.get("debug", {}).get("save_on_action", True):
            return
        path = self.debug_dir / f"{int(time.time())}_{note}.png"
        if self.cfg.get("debug", {}).get("draw_ocr", True):
            self.vision.save_debug(image, boxes, path, note=note)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            image.save(path)

    async def scan_app(self, browser: BrowserSession) -> tuple[Rect, Image.Image, list[TextBox]]:
        rect = await browser.app_rect()
        image = await browser.screenshot_app(rect)
        boxes = self.vision.read_text(image)
        return rect, image, boxes

    async def handle_incoming_order(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        now = time.time()
        notification = self.find_trade_notification(image, boxes)
        if notification:
            if not self.cfg.get("auto_accept", True):
                print("[state] 检测到交易通知，自动接单已关闭。")
                return True
            target = self.vision.find_text(
                boxes,
                self.texts.get("accept", []),
                prefer_right=True,
                prefer_bottom=True,
                max_text_length=20,
                exclude=(self.texts.get("done", []) + self.texts.get("dangerous_confirm", [])),
            )
            if not target:
                await self.save_action_debug(image, boxes, "trade_notification_accept_not_found")
                print("[state] 检测到交易通知，等待识别同意交易按钮。")
                return True
            if now - self.last_accept_at < 0.5:
                return True
            await self.save_action_debug(image, boxes, "trade_notification_accept")
            self.record_action("交易通知，按文字点击同意交易")
            self.last_accept_at = now
            self.form_expected_after = 0
            self.form_expected_until = 0
            self.form_submission_failed = False
            self.form_interrupted_by_trade_notification = True
            self.form_values = {}
            await self.click_visual_target(browser, rect, target)
            self.accept_confirm_until = time.time() + 6
            return True
        if self.is_order_detail_interrupt_page(image, boxes) and (
            self.form_expected_until or self.form_submission_failed
        ):
            # A new order can navigate away while a multi-order form is being filled.
            # Drop only the in-progress form state, then return to a page where the
            # normal order-status checks can decide what to do next.
            self.form_expected_after = 0
            self.form_expected_until = 0
            self.form_submission_failed = False
            self.payment_selected_for_form = False
            self.form_values = {}
            if now - self.last_form_interruption_back_at >= 1.5:
                await self.save_action_debug(image, boxes, "order_interrupt_back")
                self.record_action("挂单被订单详情打断，返回后检查订单状态")
                back = self.points.get("order_detail_back", [0.08, 0.04])
                await browser.click_relative(rect, float(back[0]), float(back[1]))
                self.last_form_interruption_back_at = now
            else:
                print("[state] 挂单被订单详情打断，等待返回后检查订单状态。")
            return True
        if self.is_waiting_transfer(image, boxes) and not self.is_orders_page(image, boxes):
            if self.is_waiting_transfer_detail(image, boxes):
                if now - self.last_waiting_transfer_back_at >= 1.5:
                    await self.save_action_debug(image, boxes, "waiting_transfer_back")
                    self.record_action("订单待付款，点击左上角返回搬砖列表")
                    back = self.points.get("order_detail_back", [0.08, 0.04])
                    await browser.click_relative(rect, float(back[0]), float(back[1]))
                    self.last_waiting_transfer_back_at = now
                    self.accept_confirm_until = 0
                    self.post_accept_order_check = "open"
                    self.post_accept_order_check_until = now + 10.0
                return True
            print("[state] 订单待付款，等待买家转账。")
            self.accept_confirm_until = 0
            return True
        active_order = self.find_active_order_anchor(image, boxes)
        if active_order and not self.multi_order_enabled():
            label = active_order.text if isinstance(active_order, TextBox) else active_order.name
            print(f"[state] 订单页存在进行中订单：{label}")
            return True
        if self.accept_confirm_until > now:
            target = self.find_accept_confirm_target(image, boxes)
            if target:
                self.record_action("识别到接单二次确认，点击确认")
                await self.click_visual_target(browser, rect, target)
                self.accept_confirm_until = 0
            else:
                print("[state] 等待接单二次确认或订单状态更新。")
            return True

        if not self.cfg.get("auto_accept", True):
            return False
        if now - self.last_accept_at < 1.2:
            return False
        if self.has_dangerous_confirm(boxes):
            print("[guard] Dangerous confirmation text detected; skip auto accept.")
            return False

        target = self.vision.find_template(image, "accept") or self.vision.find_text(
            boxes,
            self.texts.get("accept", []),
            prefer_right=True,
            prefer_bottom=True,
            max_text_length=20,
            exclude=(self.texts.get("done", []) + self.texts.get("dangerous_confirm", [])),
        )
        if not target:
            return False
        if not self.can_act():
            return True

        await self.save_action_debug(image, boxes, "accept")
        self.record_action("来单，点击同意接单")
        self.last_accept_at = now
        self.pending_since = 0
        await self.click_visual_target(browser, rect, target)
        self.accept_confirm_until = time.time() + 6
        return True

    async def handle_order_exception_dialog(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        """Dismiss the timeout reminder without opening its detail page."""
        target = self.find_order_exception_ignore(image, boxes)
        if not target:
            await self.save_action_debug(image, boxes, "order_exception_ignore_not_found")
            print("[warn] 识别到订单异常提醒，但未找到忽略按钮，不点击立即查看。")
            return True
        now = time.time()
        if now - self.last_exception_ignore_at < 0.8 or not self.can_act():
            return True
        await self.save_action_debug(image, boxes, "order_exception_ignore")
        self.record_action("订单异常提醒，按文字点击忽略")
        self.last_exception_ignore_at = now
        await self.click_visual_target(browser, rect, target)
        return True

    async def handle_post_accept_order_check(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        if not self.post_accept_order_check:
            return False

        now = time.time()
        if now > self.post_accept_order_check_until:
            self.post_accept_order_check = "blocked"

        if self.post_accept_order_check == "blocked":
            print("[state] 未能确认订单页是否存在订单，暂停自动重挂。")
            return True

        if self.post_accept_order_check == "open":
            # After accepting, Flutter may navigate straight to 出售详情 rather than
            # back to the tabbed main page. Leave that detail page first; the order
            # tab does not exist there, so waiting for it would otherwise time out.
            if self.is_order_detail_interrupt_page(image, boxes):
                if now - self.last_post_accept_detail_back_at >= 1.5:
                    await self.save_action_debug(image, boxes, "post_accept_detail_back")
                    self.record_action("接单后在订单详情，点击左上角返回订单列表")
                    back = self.points.get("order_detail_back", [0.08, 0.04])
                    await browser.click_relative(rect, float(back[0]), float(back[1]))
                    self.last_post_accept_detail_back_at = now
                else:
                    print("[state] 接单后订单详情正在返回，等待订单页标签出现。")
                return True
            target = self.find_order_page_tab(image, boxes)
            if not target:
                print("[state] 等待订单页标签出现。")
                return True
            await self.save_action_debug(image, boxes, "order_page_tab")
            self.record_action("返回后点击订单页检查是否有进行中订单")
            await self.click_visual_target(browser, rect, target)
            self.post_accept_order_check = "inspect"
            return True

        active_order = self.find_active_order_anchor(image, boxes)
        if active_order:
            self.post_accept_order_check = ""
            print("[state] 订单页存在进行中订单，保持在订单页等待。")
            return True

        terminal = self.vision.find_text(
            boxes,
            self.texts.get("done", []),
            max_text_length=24,
        ) or self.vision.find_text(
            boxes,
            self.texts.get("order_empty", ["暂无订单", "暂无数据", "没有订单"]),
            max_text_length=24,
        )
        if not terminal:
            print("[state] 订单页尚未明确显示订单或空状态，继续等待检查。")
            return True

        target = self.find_move_page_tab(image, boxes)
        if not target:
            print("[state] 已确认没有进行中订单，等待搬砖标签出现。")
            return True
        await self.save_action_debug(image, boxes, "return_to_move_page")
        self.record_action("订单页无进行中订单，返回搬砖页重新挂单")
        await self.click_visual_target(browser, rect, target)
        self.post_accept_order_check = ""
        self.move_page_opened = True
        self.pending_since = 0
        return True

    async def click_safe_confirm(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        if self.has_dangerous_confirm(boxes):
            print("[guard] Dangerous confirmation text detected; confirm skipped.")
            return False

        target = self.vision.find_template(image, "confirm") or self.vision.find_text(
            boxes,
            self.texts.get("confirm", []),
            prefer_center=True,
            prefer_bottom=True,
            max_text_length=16,
            exclude=self.texts.get("dangerous_confirm", []),
        )
        if not target:
            return False
        self.record_action("点击安全确认")
        await self.click_visual_target(browser, rect, target)
        return True

    async def fill_canvas_text_field(
        self,
        browser: BrowserSession,
        label: str,
        label_terms: Iterable[str] | str,
        x_ratio: float,
        value: str,
    ) -> bool:
        if value is None or str(value).strip() == "":
            return True
        rect, image, boxes = await self.scan_app(browser)
        target = self.resolve_canvas_field_target(image, boxes, label_terms, x_ratio)
        if not target:
            await self.save_action_debug(image, boxes, f"field_{label}_not_found")
            print(f"[warn] 未识别{label}标签，不填写该字段。")
            return False

        x, y = target
        self.record_action(f"按文字定位填写{label}")
        await browser.click_abs(rect.left + x, rect.top + y)
        await asyncio.sleep(0.16)
        await browser.replace_numeric(str(value))
        await asyncio.sleep(0.28)

        _, verify_image, verify_boxes = await self.scan_app(browser)
        if self.is_field_value_visible(verify_boxes, str(value), target, verify_image):
            print(f"[state] 已确认{label}显示为 {value}")
            return True

        await self.save_action_debug(verify_image, verify_boxes, f"field_{label}_not_verified")
        print(f"[warn] 未在{label}输入框确认数值 {value}，停止本次挂单。")
        return False

    async def fill_canvas_all_balance(self, browser: BrowserSession, expected_amount: str) -> bool:
        """Use the form's 全部 control so the app supplies a fractional remaining balance."""
        rect, image, boxes = await self.scan_app(browser)
        field = self.resolve_canvas_field_target(
            image,
            boxes,
            self.texts.get("amount_label", ["变现金额", "出售金额"]),
            0.50,
        )
        if not field:
            await self.save_action_debug(image, boxes, "amount_all_target_not_found")
            print("[warn] 未识别变现金额输入框，不点击全部。")
            return False
        width, height = image.size
        target = self.vision.find_text(
            boxes,
            ["全部"],
            prefer_right=True,
            max_text_length=8,
            bounds=(width * 0.72, field[1] - height * 0.09, width * 0.98, field[1] + height * 0.09),
        )
        if not target:
            await self.save_action_debug(image, boxes, "amount_all_not_found")
            print("[warn] 未识别变现金额的全部按钮，不执行坐标点击。")
            return False
        self.record_action("余额不足配置金额，点击变现金额全部")
        await self.click_visual_target(browser, rect, target)
        await asyncio.sleep(0.28)
        _, verify_image, verify_boxes = await self.scan_app(browser)
        if self.is_field_value_visible(verify_boxes, expected_amount, field, verify_image):
            print(f"[state] 已确认变现金额全部显示为 {expected_amount}")
            return True
        try:
            rounded_amount = self.decimal_text(Decimal(str(expected_amount)).to_integral_value(rounding=ROUND_DOWN))
        except InvalidOperation:
            rounded_amount = ""
        if rounded_amount and self.is_field_value_visible(verify_boxes, rounded_amount, field, verify_image):
            print(f"[state] 页面将全部金额取整为 {rounded_amount}，继续填写限额")
            return True
        await self.save_action_debug(verify_image, verify_boxes, "amount_all_not_verified")
        print(f"[warn] 点击全部后未确认变现金额 {expected_amount}，停止本次挂单。")
        return False

    async def type_password_with_keypad(self, browser: BrowserSession, rect: Rect, password: str) -> None:
        keypad = self.points.get("keypad", {})
        for digit in str(password):
            if not digit.isdigit() or digit not in keypad:
                continue
            rx, ry = keypad[digit]
            await browser.click_relative(rect, float(rx), float(ry))
            await asyncio.sleep(0.09)

    async def type_password_with_keyboard(
        self,
        browser: BrowserSession,
        rect: Rect,
        password: str,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        """Use key events because Flutter's six-slot field ignores text insertion."""
        target = self.resolve_password_field_target(image, boxes)
        if not target:
            await self.save_action_debug(image, boxes, "password_field_not_found")
            print("[warn] 未识别支付密码输入格，不输入密码。")
            return False
        x, y = target
        await browser.click_abs(rect.left + x, rect.top + y)
        await asyncio.sleep(0.12)
        for digit in str(password):
            if not digit.isdigit():
                continue
            await browser.press(digit)
            await asyncio.sleep(0.08)
        return True

    async def handle_runtime_password_dialog(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        """Allow a password entered in the dashboard after the dialog appeared."""
        now = time.time()
        if now - self.last_password_input_at < 3.0:
            return False
        password = await self.resolve_payment_password()
        if not password:
            print("[state] 支付密码弹窗可见，等待控制台输入支付密码。")
            return False

        self.last_password_input_at = now
        password_cfg = self.password_cfg()
        mode = password_cfg.get("input_mode", "keyboard")
        self.record_action("检测到运行中支付密码，输入支付密码")
        if mode == "keypad":
            await self.type_password_with_keypad(browser, rect, password)
            entered = True
        else:
            entered = await self.type_password_with_keyboard(browser, rect, password, image, boxes)
        if not entered:
            return False
        await asyncio.sleep(float(password_cfg.get("post_input_wait_sec", 0.8)))
        if password_cfg.get("confirm_after_input", False):
            await self.confirm_password_if_still_visible(browser)
        else:
            print("[state] 运行中支付密码输入完成，等待页面自动返回。")
        return True

    async def select_canvas_payment_card(self, browser: BrowserSession, rect: Rect) -> bool:
        if self.payment_selected_for_form:
            return True

        rect, image, boxes = await self.scan_app(browser)
        payment = self.cfg.get("payment", {})
        keyword = self.current_payment_keyword() or str(payment.get("keyword") or "").strip()
        attempts = int(clamp(float(payment.get("ocr_attempts", 8)), 1, 16))
        if not keyword:
            print("[warn] 未配置收款卡片关键词，停止本次挂单。")
            return False

        # 当前页面的收款卡直接位于变现表单内。先取消默认选中的第一张卡，
        # 再用目标文字定位卡片；只有没有表单卡时才兼容独立选择页版本。
        first_card = self.find_first_payment_card_target(image, boxes)
        initial_target = self.find_payment_card_target(image, boxes, keyword)
        if first_card and first_card is not initial_target:
            await self.save_action_debug(image, boxes, "payment_deselect")
            self.record_action(f"取消默认收款方式：{first_card.text}")
            await self.click_payment_card_row(browser, rect, first_card)
            await asyncio.sleep(0.30)

        for attempt in range(attempts):
            rect, image, boxes = await self.scan_app(browser)
            target = self.find_payment_card_target(image, boxes, keyword)
            if target:
                await self.save_action_debug(image, boxes, "payment_select")
                self.record_action(f"按文字选择收款卡片：{target.text}")
                await self.click_payment_card_row(browser, rect, target)
                self.payment_selected_for_form = True
                return True

            entry = self.find_payment_picker_entry(image, boxes)
            if entry:
                await self.save_action_debug(image, boxes, "payment_entry")
                self.record_action("按文字打开收款方式选择页")
                await self.click_visual_target(browser, rect, entry)
                await asyncio.sleep(0.35)
                continue

            if attempt < attempts - 1:
                self.record_action(f"未找到收款卡片：{keyword}，在卡片区域向下滚动 {attempt + 1}/{attempts}")
                await browser.wheel_relative(rect, 0.5, 0.76, 520)
                await asyncio.sleep(0.42)

        await self.save_action_debug(image, boxes, "payment_not_found")
        print(f"[warn] 未按文字找到收款卡片：{keyword}，不使用卡片序号坐标兜底。")
        return False

    async def click_payment_card_row(self, browser: BrowserSession, rect: Rect, target: TextBox) -> None:
        _, y = target.center
        x = rect.width * float(self.points.get("payment_card_x", 0.5))
        y = clamp(y, 20, rect.height - 70)
        await browser.click_abs(rect.left + x, rect.top + y)

    async def submit_canvas_order_form(self, browser: BrowserSession, rect: Rect) -> bool:
        self.form_interrupted_by_trade_notification = False
        rect, image, boxes = await self.scan_app(browser)
        submit = self.find_bottom_action_target(image, boxes, self.texts.get("submit", []))
        if not submit:
            await self.save_action_debug(image, boxes, "submit_not_found")
            print("[warn] 未识别底部确认变现，不执行坐标点击。")
            return False

        await self.save_action_debug(image, boxes, "submit")
        self.record_action("按文字点击底部确认变现")
        await self.click_visual_target(browser, rect, submit)

        if not await self.click_secondary_confirm(browser):
            return False
        cfg = self.submit_flow_cfg()
        await asyncio.sleep(cfg["password_delay_sec"])
        password_scan = await self.wait_for_visual_state(browser, "password", cfg["password_timeout_sec"])
        if not password_scan:
            if self.form_interrupted_by_trade_notification:
                print("[state] 挂单等待被交易通知打断，转入来单处理。")
                return True
            return False

        password = await self.resolve_payment_password()
        if password:
            password_cfg = self.password_cfg()
            mode = password_cfg.get("input_mode", "keyboard")
            rect = password_scan[0]
            self.record_action("二次确认后输入支付密码")
            if mode == "keypad":
                await self.type_password_with_keypad(browser, rect, password)
            else:
                if not await self.type_password_with_keyboard(
                    browser,
                    rect,
                    password,
                    password_scan[1],
                    password_scan[2],
                ):
                    return False
            await asyncio.sleep(float(password_cfg.get("post_input_wait_sec", 0.8)))
            if password_cfg.get("confirm_after_input", False):
                await self.confirm_password_if_still_visible(browser)
            else:
                print("[state] 支付密码输入完成，等待页面自动返回")
        else:
            print("[warn] 未提供支付密码；如果页面弹密码框，需要手动输入。")
            return False

        if not await self.wait_for_visual_state(browser, "start", cfg["return_to_start_timeout_sec"]):
            if self.form_interrupted_by_trade_notification:
                print("[state] 挂单返回等待被交易通知打断，转入来单处理。")
                return True
            return False

        submitted_at = time.time()
        if self.multi_order_enabled():
            self.multi_pending_since.append(submitted_at)
            self.multi_payment_index += 1
            self.multi_batch_complete = False
            self.multi_cancel_scroll_steps = 0
            self.multi_cancel_recovering = False
            self.form_values = {}
            self.pending_since = 0
            self.record_action("多挂单已提交，返回搬砖页继续检查余额")
        else:
            self.pending_since = submitted_at
        self.form_expected_after = 0
        self.form_expected_until = 0
        if not self.multi_order_enabled():
            self.record_action("页面已返回开始搬砖，挂单完成，等待来单")
        return True

    async def confirm_password_if_still_visible(self, browser: BrowserSession) -> bool:
        rect, image, boxes = await self.scan_app(browser)
        dialog = self.find_password_dialog(image, boxes)
        if not dialog:
            print("[state] 支付密码弹窗已关闭，不点击密码确认")
            return False

        target = self.vision.find_text(
            boxes,
            self.texts.get("password_confirm", []),
            prefer_center=True,
            prefer_bottom=True,
            max_text_length=12,
        )
        if target:
            await self.save_action_debug(image, boxes, "password_confirm")
            self.record_action("检测到支付密码确认按钮，点击确认")
            await self.click_visual_target(browser, rect, target)
            return True

        await self.save_action_debug(image, boxes, "password_confirm_not_found")
        print("[warn] 支付密码弹窗仍在但未识别确认文字，不执行坐标点击。")
        return False

    async def click_secondary_confirm(self, browser: BrowserSession) -> bool:
        cfg = self.submit_flow_cfg()
        started = time.time()

        while time.time() - started <= cfg["secondary_confirm_timeout_sec"]:
            elapsed = time.time() - started
            await asyncio.sleep(max(0.15, cfg["state_poll_sec"]))
            rect, image, boxes = await self.scan_app(browser)

            if self.has_dangerous_confirm(boxes):
                print("[guard] 检测到账/放行类确认，跳过二次确认。")
                return False

            password_dialog = self.find_password_dialog(image, boxes)
            if password_dialog:
                print("[state] 已进入支付密码弹窗，跳过二次确认点击")
                return True

            if elapsed < cfg["secondary_confirm_delay_sec"]:
                print(
                    "[state] 等待底部二次确认 "
                    f"{elapsed:.1f}/{cfg['secondary_confirm_delay_sec']:.1f}s"
                )
                continue

            target = self.vision.find_template(image, "secondary_confirm") or self.find_bottom_action_target(
                image,
                boxes,
                self.texts.get("secondary_confirm", self.texts.get("confirm", [])),
            )

            if target:
                await self.save_action_debug(image, boxes, "secondary_confirm")
                self.record_action("按文字点击底部二次确认")
                await self.click_visual_target(browser, rect, target)
                return True

            print(
                "[state] 等待识别底部二次确认 "
                f"{elapsed:.1f}/{cfg['secondary_confirm_timeout_sec']:.1f}s"
            )

        await self.save_action_debug(image, boxes, "secondary_confirm_not_found")
        print("[warn] 未检测到底部二次确认，不执行坐标点击。")
        return False

    async def fill_and_submit_canvas_order_form(self, browser: BrowserSession, rect: Rect) -> bool:
        now = time.time()
        if now - self.last_form_submit_at < 5:
            return True
        self.last_form_submit_at = now

        values = self.form_values if self.multi_order_enabled() else {}
        amount = values.get("amount") or str(self.cfg.get("amount") or "").strip()
        if not amount:
            print("[warn] 请先在 configs/qjd67.json 填写 amount。")
            return True

        fill_amount = (
            self.fill_canvas_all_balance(browser, amount)
            if values.get("use_all_balance") == "true"
            else self.fill_canvas_text_field(
                browser,
                "变现金额",
                self.texts.get("amount_label", ["变现金额", "出售金额"]),
                0.50,
                amount,
            )
        )
        if not await fill_amount:
            self.form_submission_failed = True
            return True
        if not await self.fill_canvas_text_field(
            browser,
            "最小限额",
            self.texts.get("limit_label", ["单笔限额"]),
            0.26,
            values.get("limit_min") or str(self.cfg.get("limit_min") or ""),
        ):
            self.form_submission_failed = True
            return True
        if not await self.fill_canvas_text_field(
            browser,
            "最大限额",
            self.texts.get("limit_label", ["单笔限额"]),
            0.74,
            values.get("limit_max") or str(self.cfg.get("limit_max") or ""),
        ):
            self.form_submission_failed = True
            return True
        if not await self.select_canvas_payment_card(browser, rect):
            self.form_submission_failed = True
            return True
        submitted = await self.submit_canvas_order_form(browser, rect)
        self.form_submission_failed = not submitted
        return True

    async def cancel_pending_order(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        now = time.time()
        if now - self.last_cancel_at < 4:
            return True
        if not self.can_act():
            return True

        is_multi = self.multi_order_enabled()
        target: TextBox | TemplateMatch | None = self.find_bottom_cancel_target(image, boxes) if is_multi else self.vision.find_template(image, "cancel")
        if is_multi:
            if not target and self.multi_cancel_scroll_steps:
                self.record_action("未找到进行中订单，向上回滚取消列表")
                await browser.wheel_relative(rect, 0.50, 0.66, -360)
                self.multi_cancel_scroll_steps -= 1
                self.multi_cancel_recovering = True
                return True
            if target:
                # Only a completed/cancelled row below the button proves this is
                # the oldest active order. A low visible button alone is not enough
                # when the active section overflows the viewport.
                history_below = self.has_terminal_history_below(target, boxes)
                if not history_below:
                    max_steps = self.multi_cancel_search_max_steps()
                    if self.multi_cancel_scroll_steps >= max_steps:
                        await self.save_action_debug(image, boxes, "cancel_tail_not_confirmed")
                        print(f"[warn] 已向下扫描 {max_steps} 次仍未确认活动订单尾部，不点击取消。")
                        return True
                    self.record_action(
                        f"按先进先出继续向下查找最尾部挂单 "
                        f"{self.multi_cancel_scroll_steps + 1}/{max_steps}"
                    )
                    await browser.wheel_relative(rect, 0.50, 0.66, 360)
                    self.multi_cancel_scroll_steps += 1
                    self.multi_cancel_recovering = False
                    return True
                self.multi_cancel_recovering = False
        anchor = self.find_pending_anchor(image, boxes)
        if not target and isinstance(anchor, TextBox) and not text_has_any(anchor.text, self.texts.get("cancel", [])):
            _, height = image.size
            target = self.vision.find_text(
                boxes,
                self.texts.get("cancel", []),
                prefer_right=True,
                max_text_length=16,
                exclude=self.texts.get("done", []),
                bounds=(0, max(0, anchor.top - height * 0.09), image.width, min(height, anchor.bottom + height * 0.09)),
            )
        if not target:
            target = self.find_bottom_cancel_target(image, boxes) if is_multi else self.vision.find_text(
                boxes,
                self.texts.get("cancel", []),
                prefer_right=True,
                prefer_bottom=True,
                max_text_length=16,
                exclude=self.texts.get("done", []),
            )
        if not target:
            await self.save_action_debug(image, boxes, "cancel_not_found")
            print("[warn] 未识别取消按钮，不执行坐标点击。")
            return True

        self.last_cancel_at = now
        self.cancel_clicked_at = now
        self.last_cancel_confirm_at = 0
        self.cancel_confirm_clicks = 0
        self.record_action("按先进先出点击最底部取消挂单" if is_multi else "按文字点击取消挂单")
        await self.click_visual_target(browser, rect, target)
        self.cancel_confirm_until = time.time() + self.cancel_flow_cfg()["confirm_timeout_sec"]
        self.cancelling_multi_order = is_multi and bool(self.multi_pending_since)
        if is_multi:
            self.multi_cancel_scroll_steps = 0
        self.pending_since = 0
        return True

    async def handle_cancel_confirm_dialog(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        now = time.time()
        cfg = self.cancel_flow_cfg()
        elapsed = now - self.cancel_clicked_at

        if elapsed < cfg["confirm_delay_sec"]:
            print(f"[state] 等待取消确认弹窗 {elapsed:.1f}/{cfg['confirm_delay_sec']:.1f}s")
            return True

        if self.cancel_confirm_clicks >= cfg["max_confirm_clicks"]:
            self.cancel_confirm_until = 0
            self.next_post_after = now + 1.4
            print("[state] 已点击取消确认，等待页面稳定")
            return True

        if self.last_cancel_confirm_at and now - self.last_cancel_confirm_at < cfg["retry_interval_sec"]:
            return True

        target = self.find_cancel_confirm_target(image, boxes)
        if not target:
            print(f"[state] 等待识别取消确认弹窗 {elapsed:.1f}/{cfg['confirm_timeout_sec']:.1f}s")
            return True

        await self.save_action_debug(image, boxes, "cancel_confirm")
        self.record_action("识别到取消确认弹窗，点击确认")
        await self.click_visual_target(browser, rect, target)

        self.cancel_confirm_clicks += 1
        self.last_cancel_confirm_at = now
        self.cancel_confirm_until = 0
        self.next_post_after = now + 1.4
        if self.cancelling_multi_order:
            if self.multi_pending_since:
                self.multi_pending_since.pop(0)
            self.multi_batch_complete = False
            self.cancelling_multi_order = False
            self.multi_cancel_scroll_steps = 0
            self.multi_cancel_recovering = False
            print("[state] 已取消一笔超时挂单，等待余额恢复后补挂。")
        return True

    async def start_order_flow(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        now = time.time()
        if now < self.next_post_after or now - self.last_start_at < 1.6:
            return True
        if not self.can_act():
            return True

        if self.multi_order_enabled():
            balance = self.find_available_balance(image, boxes)
            if balance is None:
                await self.save_action_debug(image, boxes, "balance_not_found")
                print("[warn] 未识别顶部搬砖余额，不新增挂单。")
                return True
            if not self.prepare_multi_order_values(balance):
                return True

        target = self.find_start_order_target(image, boxes)
        if not target:
            await self.save_action_debug(image, boxes, "start_not_found")
            print("[state] 未检测到开始搬砖按钮，本轮不点击。")
            return True

        self.last_start_at = now
        self.last_form_submit_at = 0
        self.payment_selected_for_form = False
        self.form_submission_failed = False
        self.form_expected_after = now + 0.8
        self.form_expected_until = now + 12
        self.record_action("按文字点击开始搬砖")
        await self.click_visual_target(browser, rect, target)
        return True

    async def handle_order_workflow(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        if not self.cfg.get("auto_make_order", True):
            return False

        now = time.time()
        if self.is_waiting_transfer(image, boxes):
            print("[state] 订单待付款，等待买家转账。")
            return True
        if self.cancel_confirm_until:
            if self.cancel_confirm_until > now:
                return await self.handle_cancel_confirm_dialog(browser, rect, image, boxes)
            print("[state] 取消确认等待超时，跳过本轮重挂")
            self.cancel_confirm_until = 0
            self.multi_cancel_scroll_steps = 0
            self.multi_cancel_recovering = False
            self.next_post_after = now + 1.4
            return True

        if self.form_expected_until:
            if now < self.form_expected_after:
                print("[state] 等待变现页面打开")
                return True
            if now <= self.form_expected_until:
                if self.form_submission_failed:
                    print("[state] 本次挂单未完成视觉校验，暂停重试，等待你检查调试截图。")
                    return True
                if self.is_order_form_visible(image, boxes):
                    return await self.fill_and_submit_canvas_order_form(browser, rect)
                print("[state] Playwright 尚未检测到变现页面")
                return True
            self.form_expected_after = 0
            self.form_expected_until = 0

        if self.is_order_form_visible(image, boxes):
            if self.form_submission_failed:
                print("[state] 本次挂单未完成视觉校验，暂停重试，等待你检查调试截图。")
                return True
            return await self.fill_and_submit_canvas_order_form(browser, rect)

        if self.multi_order_enabled():
            return await self.handle_multi_order_workflow(browser, rect, image, boxes)

        anchor = self.find_pending_anchor(image, boxes)
        if self.clear_stale_pending_if_missing(anchor, boxes):
            print("[state] 页面确认订单已结束，清除旧挂单计时并重新开始。")
            return await self.start_order_flow(browser, rect, image, boxes)
        if anchor:
            if not self.pending_since:
                self.pending_since = now
            age = int(now - self.pending_since)
            repost_delay = int(self.cfg.get("repost_delay_sec", 30))
            print(f"[state] 已挂单等待来单 {age}/{repost_delay}s")
            if self.cfg.get("auto_repost", True) and age >= repost_delay:
                return await self.cancel_pending_order(browser, rect, image, boxes)
            return True

        if self.pending_since:
            age = int(now - self.pending_since)
            repost_delay = int(self.cfg.get("repost_delay_sec", 30))
            print(f"[state] 已提交挂单，等待来单 {age}/{repost_delay}s")
            if self.cfg.get("auto_repost", True) and age >= repost_delay:
                return await self.cancel_pending_order(browser, rect, image, boxes)
            return True

        self.pending_since = 0
        return await self.start_order_flow(browser, rect, image, boxes)

    async def handle_multi_order_workflow(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        """Post until balance is depleted, then use the existing visual cancel chain for timeouts."""
        if self.is_empty_orders_page(image, boxes):
            return await self.return_from_empty_orders_page(browser, rect, image, boxes)

        pending = self.find_pending_anchor(image, boxes)
        pending_count = self.count_pending_orders(boxes)

        if pending_count and not self.multi_pending_since and self.multi_order_cfg().get("adopt_existing_pending_orders", False):
            self.multi_pending_since = [time.time()] * pending_count
            print(f"[state] 已接管启动前已有的 {pending_count} 笔挂单，从本次启动开始重新计时。")

        balance = self.find_available_balance(image, boxes)
        if balance is None:
            await self.save_action_debug(image, boxes, "balance_not_found")
            print("[warn] 未识别顶部搬砖余额，不新增挂单。")
            return True
        minimum_remaining = Decimal(str(self.cfg.get("limit_min") or "0"))
        if pending_count == 0 and self.multi_pending_since:
            if balance >= minimum_remaining:
                self.multi_pending_since.clear()
                print("[state] 余额已恢复且未检测到待接单订单，清除手动取消后的本地计时。")
            else:
                print("[state] 当前视图未识别待接单订单，但余额未恢复，保留本地计时。")
        if balance >= minimum_remaining:
            if self.multi_batch_complete:
                print(f"[state] 搬砖余额恢复为 {self.decimal_text(balance)}，继续新增挂单。")
            self.multi_batch_complete = False
            return await self.start_order_flow(browser, rect, image, boxes)

        if not self.multi_batch_complete:
            return await self.start_order_flow(browser, rect, image, boxes)

        if not pending:
            # We may have just moved below the active cards into completed/cancelled
            # history. Recover one small step instead of treating that history as
            # proof that the timed order disappeared.
            if self.multi_pending_since and getattr(self, "multi_cancel_scroll_steps", 0):
                age = int(time.time() - self.multi_pending_since[0])
                repost_delay = int(self.cfg.get("repost_delay_sec", 30))
                if self.cfg.get("auto_repost", True) and age >= repost_delay:
                    print("[state] 已滚入历史订单区，向上回找超时挂单。")
                    return await self.cancel_pending_order(browser, rect, image, boxes)
            print("[state] 多挂单批次已完成，余额不足阈值，停止新增挂单。")
            return True

        if not self.multi_pending_since:
            print("[state] 检测到已有挂单，但本次启动未创建它们，不按本地计时自动取消。")
            return True

        age = int(time.time() - self.multi_pending_since[0])
        repost_delay = int(self.cfg.get("repost_delay_sec", 30))
        print(f"[state] 多挂单已停止新增，最早一笔等待 {age}/{repost_delay}s，共跟踪 {len(self.multi_pending_since)} 笔。")
        if self.cfg.get("auto_repost", True) and age >= repost_delay:
            return await self.cancel_pending_order(browser, rect, image, boxes)
        return True

    async def return_from_empty_orders_page(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        """An explicitly empty order page is safe to leave and resume from 搬砖."""
        target = self.find_move_page_tab(image, boxes)
        if not target:
            print("[state] 订单页已空，等待搬砖标签出现。")
            return True
        await self.save_action_debug(image, boxes, "empty_order_return_move")
        self.record_action("订单页无进行中订单，返回搬砖页继续挂单")
        await self.click_visual_target(browser, rect, target)
        self.multi_pending_since.clear()
        self.multi_batch_complete = False
        self.move_page_opened = True
        return True

    async def handle_orders_list_page(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        """Observe an orders list; posting is intentionally never started from this page."""
        if self.is_empty_orders_page(image, boxes):
            return await self.return_from_empty_orders_page(browser, rect, image, boxes)
        if await self.maybe_pull_refresh_orders(browser, rect, image, boxes):
            return True
        active = self.find_active_order_anchor(image, boxes)
        if active:
            label = active.text if isinstance(active, TextBox) else active.name
            print(f"[state] 订单列表存在进行中订单：{label}")
            return True
        print("[state] 订单列表仍在加载，等待明确的订单或空状态。")
        return True

    async def maybe_pull_refresh_orders(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        """Refresh the orders list once per minute without interacting with an order row."""
        now = time.time()
        if now - self.last_orders_refresh_at < 60:
            return False
        self.last_orders_refresh_at = now
        await self.save_action_debug(image, boxes, "orders_pull_refresh")
        self.record_action("订单页每分钟下拉刷新")
        # Start below the status tabs and above the bottom navigation. The gesture
        # lands in empty list content even when a single order card is visible.
        await browser.drag_relative(rect, 0.50, 0.42, 0.50, 0.70)
        await asyncio.sleep(0.45)
        return True

    async def handle_withdraw_form_page(self, browser: BrowserSession, rect: Rect) -> bool:
        """Only the withdraw form handler may type form values or submit a new order."""
        if self.form_submission_failed:
            print("[state] 变现表单上次视觉校验失败，等待你手动修改或离开页面。")
            return True
        return await self.fill_and_submit_canvas_order_form(browser, rect)

    async def open_move_page(self, browser: BrowserSession, rect: Rect, image: Image.Image, boxes: list[TextBox]) -> bool:
        target = self.find_move_page_tab(image, boxes)
        if not target:
            print("[state] 未检测到搬砖标签，等待页面加载。")
            return True
        if not self.can_act():
            return True
        await self.save_action_debug(image, boxes, "move_page_tab")
        self.record_action("按文字点击顶部搬砖页面")
        await self.click_visual_target(browser, rect, target)
        self.move_page_opened = True
        return True

    async def handle_unknown_page(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        """Recover from a stuck canvas/loading state without touching risky confirmations."""
        self.unknown_page_streak = getattr(self, "unknown_page_streak", 0) + 1
        consecutive_scans, cooldown_sec = self.unknown_page_refresh_cfg()
        if self.unknown_page_streak < consecutive_scans:
            print(f"[state] 未识别当前页面，等待刷新阈值 {self.unknown_page_streak}/{consecutive_scans}。")
            return True

        now = time.time()
        last_reload = getattr(self, "last_unknown_page_reload_at", 0.0)
        if now - last_reload < cooldown_sec:
            print("[state] 未识别页面仍在刷新冷却期，不执行任何点击。")
            return True

        await self.save_action_debug(image, boxes, "unknown_page_reload")
        self.record_action("连续未识别页面，刷新当前网页重试")
        self.last_unknown_page_reload_at = now
        self.unknown_page_streak = 0
        await browser.reload()
        return True

    async def tick(self, browser: BrowserSession) -> None:
        rect = await browser.app_rect()
        image = await browser.screenshot_app(rect)
        boxes = self.vision.read_text(image)

        if self.cfg.get("debug", {}).get("save_each_tick"):
            await self.maybe_save_debug(image, boxes, "tick")

        print(f"[scan] app={rect.source} {rect.width:.0f}x{rect.height:.0f} ocr={len(boxes)}")

        page = self.classify_visual_page(image, boxes)
        # OCR may be unavailable during first-run setup. The auth screen still
        # exposes a stable URL/DOM hint, so do not treat it as an unknown page.
        if page == "unknown" and not boxes:
            try:
                diag = await browser.diagnostics()
                auth_hint = f"{diag.get('url', '')} {diag.get('bodyText', '')}".lower()
                if "loginpage" in auth_hint or any(term in auth_hint for term in ("登录", "注册", "手机号")):
                    page = "auth_page"
            except Exception:
                pass
        self.note_visual_page(page)

        if page == "trade_notification":
            await self.notify_once("trade_notification", "NO钱包：收到交易通知", "检测到新的交易通知，请及时处理。")
        elif page == "order_exception_dialog":
            await self.notify_once("order_exception", "NO钱包：订单异常", "检测到订单异常提醒，请及时检查。")
        elif page in {"waiting_transfer", "waiting_transfer_detail"}:
            await self.notify_once("waiting_transfer", "NO钱包：待付款", "订单已进入待付款状态，请留意买家转账。")

        if page == "unknown":
            await self.handle_unknown_page(browser, rect, image, boxes)
            return
        self.unknown_page_streak = 0

        # A manual navigation away from a failed form is an explicit recovery.
        # Forget the stale failure, then let the handler for the new page decide.
        if page != "withdraw_form" and self.form_submission_failed:
            self.form_submission_failed = False
            self.form_expected_after = 0
            self.form_expected_until = 0
            self.form_values = {}
            print("[state] 已离开失败的变现表单，恢复按当前页面识别。")

        if page == "trade_notification":
            await self.handle_incoming_order(browser, rect, image, boxes)
            return

        if page == "order_exception_dialog":
            await self.handle_order_exception_dialog(browser, rect, image, boxes)
            return

        if await self.handle_post_accept_order_check(browser, rect, image, boxes):
            return

        if await self.handle_incoming_order(browser, rect, image, boxes):
            return

        if page == "cancel_confirm_dialog":
            return await self.handle_cancel_confirm_dialog(browser, rect, image, boxes)
        if page == "password_dialog":
            await self.handle_runtime_password_dialog(browser, rect, image, boxes)
            return
        if page == "auth_page":
            print("[state] 当前是登录/注册页，请先手动登录；不刷新页面。")
            return
        if page == "withdraw_form":
            self.move_page_opened = True
            return await self.handle_withdraw_form_page(browser, rect)
        if page in {"orders_empty", "orders_list"}:
            return await self.handle_orders_list_page(browser, rect, image, boxes)
        if page == "move_page":
            self.move_page_opened = True
            return await self.handle_order_workflow(browser, rect, image, boxes)
        if page in {"waiting_transfer", "waiting_transfer_detail", "order_detail"}:
            print("[state] 订单详情正在加载，等待订单状态文字出现。")
            return
        if page == "navigation":
            return await self.open_move_page(browser, rect, image, boxes)

        if self.form_expected_until and time.time() <= self.form_expected_until:
            print("[state] 等待变现页面加载。")
            return

    async def run(self) -> None:
        async with BrowserSession(self.cfg) as browser:
            print(f"[boot] OCR: {self.vision.ocr_name}")
            print("[boot] Browser ready. Press Ctrl+C to stop.")
            await self.notify_once("started", "NO钱包：脚本已启动", f"OCR：{self.vision.ocr_name}")
            while True:
                try:
                    await self.tick(browser)
                except Exception as exc:
                    if "Target page, context or browser has been closed" in str(exc):
                        print("[error] 浏览器页面已关闭，脚本停止。")
                        break
                    print(f"[error] tick failed: {exc}")
                await asyncio.sleep(float(self.cfg.get("poll_interval_sec", 0.8)))


async def snapshot(cfg: dict[str, Any]) -> None:
    bot = CanvasBot(cfg)
    async with BrowserSession(cfg) as browser:
        diag = await browser.diagnostics()
        rect = await browser.app_rect()
        image = await browser.screenshot_app(rect)
        boxes = bot.vision.read_text(image)
        output = bot.debug_dir / f"snapshot_{int(time.time())}.png"
        bot.vision.save_debug(image, boxes, output, note=f"{rect.source} {rect.width:.0f}x{rect.height:.0f}")
        print(f"[snapshot] saved: {output}")
        print(f"[snapshot] page: {diag.get('title')!r} {diag.get('url')}")
        print(
            "[snapshot] dom: "
            f"ready={diag.get('readyState')} "
            f"canvas={diag.get('canvasCount')} "
            f"flutter={diag.get('flutterViewCount')} "
            f"glass={diag.get('glassPaneCount')} "
            f"html={diag.get('htmlLength')}"
        )
        if diag.get("bodyText"):
            print(f"[snapshot] body: {diag.get('bodyText')}")
        print(f"[snapshot] app rect: {rect}")
        print(f"[snapshot] OCR engine: {bot.vision.ocr_name}, boxes: {len(boxes)}")
        for box in boxes[:40]:
            print(f"  {box.confidence:.2f} {box.text}")


async def manual_login(cfg: dict[str, Any]) -> None:
    async with BrowserSession(cfg) as browser:
        diag = await browser.diagnostics()
        print(f"[login] opened: {diag.get('title')!r} {diag.get('url')}")
        print("[login] 请在打开的浏览器窗口里手动登录。登录完成并确认进入首页后，回到这里按 Enter 保存并关闭窗口。")
        await asyncio.to_thread(input, "[login] 登录完成后按 Enter ...")


def doctor(cfg: dict[str, Any]) -> int:
    ok = True

    def check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        status = "ok" if passed else "missing"
        print(f"[{status}] {name}{(': ' + detail) if detail else ''}")
        ok = ok and passed

    check("uv", shutil.which("uv") is not None, shutil.which("uv") or "")

    try:
        import playwright  # type: ignore

        check("playwright", True, str(Path(playwright.__file__).parent))
    except Exception:
        check("playwright", False, "run: uv sync")

    try:
        import cv2  # type: ignore

        check("opencv", True, cv2.__version__)
    except Exception:
        check("opencv", False, "run: uv sync")

    try:
        from rapidocr_onnxruntime import RapidOCR  # noqa: F401

        check("rapidocr", True)
    except Exception:
        print("[optional] rapidocr missing; run: uv sync --extra ocr")

    chrome_path = cfg.get("chrome_path")
    check("chrome", bool(chrome_path and Path(chrome_path).exists()), str(chrome_path or ""))

    profile_cfg = cfg.get("browser_profile", {})
    profile_mode = str(profile_cfg.get("mode", "managed")).lower()
    if profile_mode in {"system", "user", "user_profile", "system_profile"}:
        profile_root = resolve_path(
            profile_cfg.get("system_user_data_dir")
            or profile_cfg.get("user_data_dir")
            or "%LOCALAPPDATA%\\Google\\Chrome\\User Data"
        )
        profile_dir = str(profile_cfg.get("profile_directory") or "Default")
        check("system chrome profile", profile_root.exists(), str(profile_root / profile_dir))
        if is_chrome_running():
            print("[warn] Chrome is running; close all Chrome windows before using browser_profile.mode=system.")
    else:
        managed_profile = resolve_path(cfg.get("user_data_dir", ".browser-profile/qjd67"))
        print(f"[ok] managed browser profile: {managed_profile}")

    if not str(cfg.get("amount") or "").strip():
        print("[warn] amount is empty in config; bot will not submit an order form.")
    if not os.getenv(cfg.get("password", {}).get("env", "QJD67_PAY_PASSWORD")) and not cfg.get("password", {}).get("value"):
        print("[warn] password is not set; password dialogs need manual input.")

    return 0 if ok else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Canvas screenshot driven browser bot.")
    parser.add_argument("--config", default=None, help="Path to config JSON.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in {
        "doctor": "Check local dependencies and config.",
        "login": "Open browser and wait for manual login before closing.",
        "snapshot": "Open browser, capture app screenshot, and print OCR text.",
        "run": "Run the automation loop.",
    }.items():
        subparser = subparsers.add_parser(name, help=help_text)
        subparser.add_argument("--config", default=None, help=argparse.SUPPRESS)
    dashboard_parser = subparsers.add_parser("dashboard", help="Open the local configuration dashboard.")
    dashboard_parser.add_argument("--config", default=None, help=argparse.SUPPRESS)
    dashboard_parser.add_argument("--host", default="127.0.0.1", help="Local bind host.")
    dashboard_parser.add_argument("--port", type=int, default=8765, help="Local bind port.")
    return parser.parse_args(argv)


async def async_main(argv: list[str]) -> int:
    args = parse_args(argv)
    cfg_path = resolve_path(args.config or "configs/qjd67.json")
    cfg = load_config(cfg_path)

    if args.command == "doctor":
        return doctor(cfg)
    if args.command == "login":
        await manual_login(cfg)
        return 0
    if args.command == "snapshot":
        await snapshot(cfg)
        return 0
    if args.command == "run":
        await CanvasBot(cfg).run()
        return 0
    if args.command == "dashboard":
        from .dashboard import run_dashboard

        run_dashboard(cfg_path, host=args.host, port=args.port)
        return 0
    raise AssertionError(f"Unknown command: {args.command}")


def main() -> None:
    try:
        raise SystemExit(asyncio.run(async_main(sys.argv[1:])))
    except KeyboardInterrupt:
        print("\n[stop] Stopped by user.")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
