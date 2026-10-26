from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import random
import re
import site
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image

from .core.context import PageContext
from .core.models import Rect, TemplateMatch, TextBox
from .core.payment import enabled_payment_keywords, normalize_payment_keyword_options
from .core.text import clamp, normalize_text, resolve_path, split_terms, text_has_any
from .dialogs.handlers import classify_modal_page, dispatch_modal, is_global_interrupt_modal
from .pages.handlers import dispatch_page
from .services.pushplus import PushPlusNotifier
from .services.telegram import TelegramNotifier


# See ``core.text.ROOT``: packaged builds use the directory containing the EXE
# so editable config and browser state remain outside the temporary bundle.
ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
_CUDA_DLL_HANDLES: list[object] = []


def prepare_cuda_runtime() -> None:
    """Make pip-installed NVIDIA DLLs visible to Windows' DLL loader."""
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return

    nvidia_roots: set[Path] = {Path(sys.executable).resolve().parent / "nvidia"}
    for site_packages in site.getsitepackages():
        nvidia_roots.add(Path(site_packages) / "nvidia")

    dll_directories: set[Path] = set()
    for nvidia_root in nvidia_roots:
        if not nvidia_root.is_dir():
            continue
        dll_directories.update(path.parent for path in nvidia_root.rglob("*.dll"))

    known_directories = {str(path) for path in dll_directories}
    current_path = os.environ.get("PATH", "").split(os.pathsep)
    missing_path = [directory for directory in sorted(known_directories) if directory not in current_path]
    if missing_path:
        os.environ["PATH"] = os.pathsep.join([*missing_path, *current_path])

    for directory in missing_path:
        try:
            _CUDA_DLL_HANDLES.append(os.add_dll_directory(directory))
        except OSError:
            continue


class DashboardLogWriter:
    """UTF-8 writer that releases the log file between writes for Windows readers."""

    encoding = "utf-8"

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, value: str) -> int:
        if not value:
            return 0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", buffering=1) as log_file:
                log_file.write(value)
        except OSError:
            pass
        return len(value)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


def configure_log_output() -> None:
    """Keep redirected dashboard logs readable on Windows, including frozen builds."""
    dashboard_log = str(os.getenv("QJD67_DASHBOARD_LOG") or "").strip()
    if dashboard_log:
        try:
            log_writer = DashboardLogWriter(Path(dashboard_log))
            sys.stdout = log_writer
            sys.stderr = log_writer
            return
        except OSError:
            # Fall through to the regular streams if the dashboard log is not
            # available (for example, a standalone CLI launch).
            pass
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True, write_through=True)
        except (AttributeError, OSError, ValueError):
            # Some GUI launchers deliberately provide no standard streams.
            # The dashboard itself remains usable in that case.
            continue


configure_log_output()


def now_ms() -> int:
    return int(time.time() * 1000)


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_config_path"] = str(path.resolve())
    return cfg


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


class RuntimeMonitor:
    """Periodically report the cost and memory footprint of the running bot.

    The monitor is deliberately log-only: it does not retain screenshots or OCR
    data, and its Windows process query runs off the asyncio event loop.
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        monitor_cfg = cfg.get("performance_monitor", {})
        self.enabled = bool(monitor_cfg.get("enabled", True))
        try:
            self.interval_sec = max(10.0, float(monitor_cfg.get("interval_sec", 60)))
        except (TypeError, ValueError):
            self.interval_sec = 60.0
        self.last_report_at = 0.0
        self.last_screenshot_sec: float | None = None
        self.last_ocr_sec: float | None = None

        profile_cfg = cfg.get("browser_profile", {})
        profile_mode = str(profile_cfg.get("mode", "managed")).lower()
        self.managed_profile_dir: Path | None = None
        if profile_mode not in {"system", "user", "user_profile", "system_profile"}:
            self.managed_profile_dir = resolve_path(cfg.get("user_data_dir", ".browser-profile/qjd67"))

    def record_scan(self, screenshot_sec: float, ocr_sec: float) -> None:
        self.last_screenshot_sec = screenshot_sec
        self.last_ocr_sec = ocr_sec

    def memory_snapshot_mb(self) -> tuple[float | None, float | None]:
        """Return Python RSS and this managed Chrome profile's RSS on Windows."""
        if os.name != "nt":
            return None, None

        profile = str(self.managed_profile_dir) if self.managed_profile_dir else ""
        # Keep the command data-only and pass the profile via the environment so
        # unusual characters in a path cannot alter the PowerShell program.
        environment = os.environ.copy()
        environment["QJD67_MONITOR_PROFILE"] = profile
        environment["QJD67_MONITOR_PID"] = str(os.getpid())
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "$processId = [int]$env:QJD67_MONITOR_PID; "
            "$python = (Get-Process -Id $processId).WorkingSet64; "
            "$profile = $env:QJD67_MONITOR_PROFILE; "
            "$chrome = 0; "
            "if ($profile) { $chrome = (Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            "Where-Object { $_.CommandLine -like ('*--user-data-dir=' + $profile + '*') } | "
            "Measure-Object -Property WorkingSetSize -Sum).Sum }; "
            "@{ python_rss = $python; chrome_rss = $chrome } | ConvertTo-Json -Compress"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
                env=environment,
            )
            if result.returncode:
                return None, None
            values = json.loads(result.stdout)
            python_rss = float(values.get("python_rss", 0)) / (1024 * 1024)
            chrome_rss = float(values.get("chrome_rss", 0)) / (1024 * 1024)
            return python_rss, chrome_rss if self.managed_profile_dir else None
        except Exception:
            return None, None

    async def report_if_due(self, tick_sec: float) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if now - self.last_report_at < self.interval_sec:
            return
        self.last_report_at = now
        python_rss, chrome_rss = await asyncio.to_thread(self.memory_snapshot_mb)
        parts = [f"tick={tick_sec:.2f}s"]
        if self.last_screenshot_sec is not None:
            parts.append(f"screenshot={self.last_screenshot_sec:.2f}s")
        if self.last_ocr_sec is not None:
            parts.append(f"ocr={self.last_ocr_sec:.2f}s")
        if python_rss is not None:
            parts.append(f"python_rss={python_rss:.0f}MB")
        if chrome_rss is not None:
            parts.append(f"managed_chrome_rss={chrome_rss:.0f}MB")
        print("[perf] " + " ".join(parts))


class VisionEngine:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.templates_dir = resolve_path(cfg.get("templates_dir", "templates"))
        self.ocr = None
        self.ocr_name = "disabled"
        self.ocr_device = "cpu"
        self.cv2 = None
        vision_cfg = cfg.get("vision", {})
        self.reuse_identical_frames = bool(vision_cfg.get("reuse_identical_frames", True))
        self._last_frame_key: bytes | None = None
        self._last_frame_boxes: list[TextBox] = []
        self.last_read_used_cache = False
        self._init_cv2()
        self._init_ocr()

    def _init_cv2(self) -> None:
        try:
            import cv2  # type: ignore

            self.cv2 = cv2
        except Exception:
            self.cv2 = None

    def _init_ocr(self) -> None:
        vision_cfg = self.cfg.get("vision", {})
        requested_device = str(vision_cfg.get("ocr_device", "cuda")).strip().lower()
        use_cuda = requested_device in {"cuda", "gpu", "nvidia"}
        use_dml = requested_device in {"directml", "dml"}
        try:
            if use_cuda:
                prepare_cuda_runtime()
            from rapidocr_onnxruntime import RapidOCR  # type: ignore

            self.ocr = RapidOCR(
                det_use_cuda=use_cuda,
                cls_use_cuda=use_cuda,
                rec_use_cuda=use_cuda,
                det_use_dml=use_dml,
                cls_use_dml=use_dml,
                rec_use_dml=use_dml,
            )
            providers = self._ocr_providers()
            provider = providers[0] if providers else "unknown"
            if provider == "CUDAExecutionProvider":
                self.ocr_device = "cuda"
            elif provider == "DmlExecutionProvider":
                self.ocr_device = "directml"
            else:
                self.ocr_device = "cpu"
            self.ocr_name = f"rapidocr-onnxruntime/{provider}"
            if use_cuda and self.ocr_device != "cuda":
                print(
                    "[vision] CUDA OCR requested but CUDAExecutionProvider is unavailable; "
                    f"using {provider}. Install onnxruntime-gpu and verify CUDA/cuDNN."
                )
        except Exception as exc:
            self.ocr = None
            self.ocr_name = "disabled"
            print(f"[vision] OCR initialization failed: {exc}")

    def _ocr_providers(self) -> list[str]:
        """Return the execution providers used by RapidOCR's model sessions."""
        if self.ocr is None:
            return []

        providers: list[str] = []
        for component_name in ("text_det", "text_cls", "text_rec"):
            component = getattr(self.ocr, component_name, None)
            for wrapper_name in ("infer", "session"):
                wrapper = getattr(component, wrapper_name, None)
                session = getattr(wrapper, "session", wrapper)
                get_providers = getattr(session, "get_providers", None)
                if not callable(get_providers):
                    continue
                for provider in get_providers():
                    if provider not in providers:
                        providers.append(provider)
                break
        return providers

    def read_text(self, image: Image.Image) -> list[TextBox]:
        if not self.ocr:
            return []

        rgb_image = image.convert("RGB")
        frame_key: bytes | None = None
        if self.reuse_identical_frames:
            # This is an exact pixel fingerprint, not a fuzzy visual heuristic.
            # A changed canvas always runs OCR again, while idle order pages avoid
            # repeatedly spending seconds on the same frame.
            frame_key = hashlib.blake2b(rgb_image.tobytes(), digest_size=16).digest()
            if frame_key == self._last_frame_key:
                self.last_read_used_cache = True
                return list(self._last_frame_boxes)

        self.last_read_used_cache = False
        arr = np.array(rgb_image)
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
        if frame_key is not None:
            self._last_frame_key = frame_key
            self._last_frame_boxes = list(boxes)
        return boxes

    def read_text_region(
        self,
        image: Image.Image,
        bounds: tuple[float, float, float, float],
    ) -> list[TextBox]:
        """OCR a normalized canvas region and restore every result to canvas coordinates."""
        width, height = image.size
        left_ratio, top_ratio, right_ratio, bottom_ratio = bounds
        left = int(clamp(width * left_ratio, 0, width - 1))
        top = int(clamp(height * top_ratio, 0, height - 1))
        right = int(clamp(width * right_ratio, left + 1, width))
        bottom = int(clamp(height * bottom_ratio, top + 1, height))
        region_boxes = self.read_text(image.crop((left, top, right, bottom)))
        return [
            TextBox(
                text=box.text,
                confidence=box.confidence,
                left=box.left + left,
                top=box.top + top,
                right=box.right + left,
                bottom=box.bottom + top,
            )
            for box in region_boxes
        ]

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

    def find_password_slots(self, image: Image.Image) -> list[tuple[float, float, float, float]]:
        """Find the six outlined Flutter password slots when OCR misses the title."""
        cv2 = getattr(self, "cv2", None)
        if cv2 is None:
            return []

        width, height = image.size
        gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(gray, 40, 150)
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        candidates: list[tuple[float, float, float, float]] = []
        for contour in contours:
            x, y, box_width, box_height = cv2.boundingRect(contour)
            if not (width * 0.06 <= box_width <= width * 0.20):
                continue
            if not (height * 0.035 <= box_height <= height * 0.13):
                continue
            if not (0.55 <= box_width / max(box_height, 1) <= 2.4):
                continue
            if not (width * 0.08 <= x <= width * 0.92 and height * 0.20 <= y <= height * 0.82):
                continue
            candidates.append((float(x), float(y), float(box_width), float(box_height)))

        # Rounded Flutter borders produce both an inner and an outer contour.
        # Keep one representative for nearby contours before looking for a row.
        candidates.sort(key=lambda item: item[2] * item[3], reverse=True)
        unique: list[tuple[float, float, float, float]] = []
        for candidate in candidates:
            cx = candidate[0] + candidate[2] / 2
            cy = candidate[1] + candidate[3] / 2
            if any(
                abs(cx - (item[0] + item[2] / 2)) <= max(8.0, candidate[2] * 0.28)
                and abs(cy - (item[1] + item[3] / 2)) <= max(8.0, candidate[3] * 0.28)
                for item in unique
            ):
                continue
            unique.append(candidate)

        unique.sort(key=lambda item: (item[1] + item[3] / 2, item[0]))
        rows: list[list[tuple[float, float, float, float]]] = []
        for candidate in unique:
            center_y = candidate[1] + candidate[3] / 2
            row = next(
                (
                    item
                    for item in rows
                    if abs(center_y - (sum(box[1] + box[3] / 2 for box in item) / len(item)))
                    <= max(14.0, candidate[3] * 0.45)
                ),
                None,
            )
            if row is None:
                rows.append([candidate])
            else:
                row.append(candidate)

        for row in rows:
            row.sort(key=lambda item: item[0])
            for start in range(max(0, len(row) - 5)):
                group = row[start : start + 6]
                widths = [item[2] for item in group]
                heights = [item[3] for item in group]
                median_width = sorted(widths)[2:4]
                median_height = sorted(heights)[2:4]
                ref_width = sum(median_width) / 2
                ref_height = sum(median_height) / 2
                if any(abs(item[2] - ref_width) > ref_width * 0.45 for item in group):
                    continue
                if any(abs(item[3] - ref_height) > ref_height * 0.45 for item in group):
                    continue
                centers = [item[0] + item[2] / 2 for item in group]
                gaps = [right - left for left, right in zip(centers, centers[1:])]
                if any(gap < ref_width * 1.15 or gap > ref_width * 3.6 for gap in gaps):
                    continue
                if centers[-1] - centers[0] < width * 0.32:
                    continue
                return group
        return []

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
            # Desktop builds inject an external profile path. Keep the profile
            # out of installer resources so cookies survive upgrades.
            user_data_dir = resolve_path(
                os.getenv("QJD67_USER_DATA_DIR") or self.cfg.get("user_data_dir", ".browser-profile/qjd67")
            )
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
        startup_navigation = self.cfg.get("startup_navigation", {})
        if not isinstance(startup_navigation, dict):
            startup_navigation = {}
        attempts = max(1, int(startup_navigation.get("attempts", 3)))
        retry_interval = max(0.5, float(startup_navigation.get("retry_interval_sec", 2)))
        navigation_timeout = int(float(self.cfg.get("navigation_timeout_sec", 30)) * 1000)
        target_url = str(self.cfg["url"])
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                await self.page.goto(target_url, wait_until="commit", timeout=navigation_timeout)
                break
            except Exception as exc:
                last_error = exc
                if attempt < attempts:
                    print(
                        f"[boot] 打开目标网站失败（第 {attempt}/{attempts} 次）：{exc}；"
                        f"{retry_interval:g} 秒后重试。"
                    )
                    await asyncio.sleep(retry_interval)
        else:
            # __aenter__ raising skips the normal context-manager exit path.
            # Close the partially created browser before returning control to
            # the dashboard, otherwise repeated Start clicks leak Chrome jobs.
            try:
                if self.context:
                    await self.context.close()
                    self.context = None
            finally:
                if self.playwright:
                    await self.playwright.stop()
                    self.playwright = None
            raise RuntimeError(
                f"连续 {attempts} 次无法打开目标网站 {target_url}，请检查网络或稍后重试。"
            ) from last_error
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

    async def focus_flutter_text_input(self) -> bool:
        """Focus Flutter's invisible editing input after a canvas field click."""
        assert self.page is not None
        result = await self.page.evaluate(
            """() => {
                const isEditable = (element) => {
                    if (!(element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement)) return false;
                    const type = (element.getAttribute('type') || 'text').toLowerCase();
                    return !element.disabled && !element.readOnly && !['hidden', 'button', 'checkbox', 'radio', 'file', 'submit'].includes(type);
                };
                const active = document.activeElement;
                const inputs = [...document.querySelectorAll('input, textarea')].filter(isEditable);
                // A Flutter page can leave an unrelated HTML field active. Always
                // prefer Flutter's editing input after the canvas field itself was
                // clicked; otherwise Playwright types successfully into the wrong
                // DOM element while the visual form remains empty.
                const flutterInputs = inputs.filter((element) =>
                    element.classList.contains('flt-text-editing-input') ||
                    Boolean(element.closest('flt-glass-pane, flutter-view'))
                );
                const target = flutterInputs.at(-1) || (isEditable(active) ? active : inputs.at(-1));
                if (!target) return false;
                target.focus({ preventScroll: true });
                return document.activeElement === target;
            }"""
        )
        return bool(result)

    async def active_text_input_value(self) -> str | None:
        """Read the current Flutter editing value without relying on canvas OCR."""
        assert self.page is not None
        value = await self.page.evaluate(
            """() => {
                const active = document.activeElement;
                if (!(active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement)) return null;
                return active.value;
            }"""
        )
        return None if value is None else str(value)

    async def set_active_text_input_value(self, value: str) -> bool:
        """Dispatch standard input events when Flutter ignored keyboard key events."""
        assert self.page is not None
        result = await self.page.evaluate(
            """(nextValue) => {
                const active = document.activeElement;
                if (!(active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement)) return false;
                const prototype = active instanceof HTMLInputElement
                    ? HTMLInputElement.prototype
                    : HTMLTextAreaElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
                if (!setter) return false;
                setter.call(active, nextValue);
                active.dispatchEvent(new InputEvent('input', {
                    bubbles: true,
                    inputType: 'insertText',
                    data: nextValue,
                }));
                active.dispatchEvent(new Event('change', { bubbles: true }));
                return active.value === nextValue;
            }""",
            value,
        )
        return bool(result)

    async def replace_numeric(self, text: str, *, verify_and_repair: bool = True) -> bool:
        """Replace a numeric Flutter canvas field.

        Password fields pass ``verify_and_repair=False``. They are entered one
        digit at a time so Flutter cannot drop characters from one large
        ``insert_text`` event; a mismatch is reported as a failed attempt and
        is never repaired by sending the password again.
        """
        assert self.page is not None
        value = "".join(char for char in str(text) if char.isdigit() or char == ".")
        if not value:
            return False
        try:
            focused = await self.focus_flutter_text_input()
        except Exception:
            return False
        if not focused:
            return False
        await self.page.keyboard.press("ControlOrMeta+A")
        await self.page.keyboard.press("Backspace")
        # Some Flutter web inputs do not honor Control+A consistently. Select
        # the current line once more, then clear a bounded number of digits.
        await self.page.keyboard.press("Home")
        await self.page.keyboard.press("Shift+End")
        await self.page.keyboard.press("Backspace")
        for _ in range(24):
            await self.page.keyboard.press("Backspace")
        if not verify_and_repair:
            for digit in value:
                await self.page.keyboard.insert_text(digit)
                await self.page.wait_for_timeout(70)
            # Flutter's editing input is an event bridge, not a reliable
            # password value store. The engine commonly clears its DOM value
            # after forwarding each digit while the canvas keeps the masked
            # value internally. The route transition is the authoritative
            # success signal; never resend a password based on this DOM field.
            await self.page.wait_for_timeout(120)
            return True
        # ``insert_text`` emits an input event directly. Some Flutter engines
        # ignore synthetic key events from ``type`` even though the hidden input
        # has focus, leaving the canvas field visibly empty.
        await self.page.keyboard.insert_text(value)
        try:
            # Flutter can accept a password and begin an asynchronous route
            # change before its hidden DOM input reflects the final value.
            # Do not clear and retype a password that was already accepted.
            await self.page.wait_for_timeout(450)
            if await self.active_text_input_value() != value:
                await self.set_active_text_input_value(value)
                await self.page.wait_for_timeout(450)
            return True
        except Exception:
            return False

    async def press(self, key: str) -> None:
        assert self.page is not None
        await self.page.keyboard.press(key)


class CanvasBot:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.runtime_monitor = RuntimeMonitor(cfg)
        self.vision = VisionEngine(cfg)
        self.pushplus = PushPlusNotifier()
        self.telegram = TelegramNotifier()
        self.notifiers = [self.pushplus, self.telegram]
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
        self.last_system_ignore_at = 0.0
        self.last_system_notice_close_at = 0.0
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
        self.last_withdraw_unavailable_back_at = 0.0
        self.accept_confirm_until = 0.0
        self.cancel_confirm_until = 0.0
        self.submit_confirm_until = 0.0
        self.multi_pending_since: list[float] = []
        self.multi_batch_complete = False
        self.multi_payment_index = 0
        self.form_values: dict[str, str] = {}
        self.cancelling_multi_order = False
        self.multi_cancel_scroll_steps = 0
        self.multi_cancel_recovering = False
        self.multi_pending_probe_steps = 0
        self.multi_pending_probe_returning = False
        self.multi_pending_probe_at_top = False
        self.last_pending_probe_completed_at = 0.0
        self.last_visual_page = ""
        self.last_orders_refresh_at = 0.0
        self.unknown_page_streak = 0
        self.last_unknown_page_reload_at = 0.0
        self.last_tick_error_capture_at = 0.0
        self.last_password_input_at = 0.0
        self.password_dialog_input_pending = False
        self.password_dialog_input_attempted = False
        self._state_log_times: dict[str, float] = {}
        self._payment_config_mtime_ns: int | None = None

    async def notify_once(self, event: str, title: str, content: str) -> None:
        """Send one notification per event per run; notification failures never stop the bot."""
        notified_events = getattr(self, "notified_events", None)
        if notified_events is None:
            notified_events = self.notified_events = set()
        notifiers = getattr(self, "notifiers", None)
        if notifiers is None:
            notifiers = [getattr(self, "pushplus", None), getattr(self, "telegram", None)]
        enabled_notifiers = [notifier for notifier in notifiers if notifier and notifier.enabled]
        if event in notified_events or not enabled_notifiers:
            return
        notified_events.add(event)
        for notifier in enabled_notifiers:
            name = str(getattr(notifier, "name", notifier.__class__.__name__))
            try:
                send_title, send_content = title, content
                if event == "waiting_transfer" and name == "PushPlus":
                    send_title, send_content = self.pushplus_waiting_transfer_message()
                await asyncio.to_thread(notifier.send, send_title, send_content)
                print(f"[notify] {name} 已提交：{event}")
            except Exception as exc:
                print(f"[notify] {name} 发送失败：{event} {exc}")

    @property
    def texts(self) -> dict[str, list[str]]:
        return self.cfg.get("texts", {})

    def pushplus_waiting_transfer_message(self) -> tuple[str, str]:
        """Build a natural, timestamped waiting-transfer notification for PushPlus only."""
        notification = self.cfg.get("notification", {})
        transfer_name = str(notification.get("transfer_name") or "").strip()
        name_prefix = f"{transfer_name}：" if transfer_name else ""
        title_prefix = f"{transfer_name} " if transfer_name else ""
        title, body = random.choice(
            [
                ("转账提醒", "订单已进入待付款状态，请留意买家转账。"),
                ("待付款提醒", "有一笔订单正在等待付款，请留意转账动态。"),
                ("订单付款提醒", "买家正在付款，请留意到账情况。"),
                ("买家转账提醒", "订单等待买家转账，请及时查看付款状态。"),
            ]
        )
        sent_at = time.strftime("%H:%M:%S")
        return f"{title_prefix}{title}", f"{name_prefix}{body}\n通知时间：{sent_at}"

    @property
    def points(self) -> dict[str, Any]:
        return self.cfg.get("points", {})

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

    def multi_cancel_scroll_step_px(self) -> int:
        """Use a small, reversible scroll step around the active/history boundary."""
        try:
            configured = int(self.multi_order_cfg().get("cancel_search_step_px", 120))
        except (TypeError, ValueError):
            configured = 120
        return max(40, min(configured, 240))

    def multi_pending_probe_max_steps(self) -> int:
        """Bound the passive scroll check used when a pending card is off-screen."""
        try:
            configured = int(self.multi_order_cfg().get("pending_probe_max_steps", 6))
        except (TypeError, ValueError):
            configured = 6
        return max(1, min(configured, 12))

    async def probe_hidden_pending_orders(self, browser: BrowserSession, rect: Rect) -> bool:
        """Search the nearby list area when low balance proves an order may be off-screen.

        The probe is reversible: it scans downward a few small steps and then
        returns to the starting position. It never clicks an order or a modal.
        """
        now = time.time()
        completed_at = getattr(self, "last_pending_probe_completed_at", 0.0)
        if completed_at and now - completed_at < 20.0:
            self.record_state(
                "pending_probe_cooldown",
                "已完成上下滑动检查，等待订单列表或余额变化。",
                interval_sec=5.0,
            )
            return True

        steps = getattr(self, "multi_pending_probe_steps", 0)
        returning = getattr(self, "multi_pending_probe_returning", False)
        max_steps = self.multi_pending_probe_max_steps()
        scroll_step = self.multi_cancel_scroll_step_px()
        if not getattr(self, "multi_pending_probe_at_top", False):
            # The current viewport may be left in the middle of a long list by
            # a prior cancellation. Reset to the top once, then the actual
            # search always proceeds top-to-bottom.
            self.record_action("回到订单列表顶部，从顶部向下检查待接单订单")
            await browser.wheel_relative(rect, 0.50, 0.66, -(scroll_step * max_steps))
            self.multi_pending_probe_at_top = True
            return True
        if not returning and steps < max_steps:
            self.record_action(f"当前视图未识别待接单订单，向下滑动检查 {steps + 1}/{max_steps}")
            await browser.wheel_relative(rect, 0.50, 0.66, scroll_step)
            self.multi_pending_probe_steps = steps + 1
            return True
        if not returning:
            self.multi_pending_probe_returning = True
            returning = True
        if returning and steps:
            self.record_action(f"未发现待接单订单，向上回到原位置 {steps}/{max_steps}")
            await browser.wheel_relative(rect, 0.50, 0.66, -scroll_step)
            self.multi_pending_probe_steps = steps - 1
            return True

        self.multi_pending_probe_returning = False
        self.multi_pending_probe_at_top = False
        self.last_pending_probe_completed_at = now
        self.record_state(
            "pending_probe_complete",
            "上下滑动检查未发现待接单订单，保留本地计时等待下一轮。",
            interval_sec=5.0,
        )
        return True

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

    def diagnostics_cfg(self) -> tuple[int, float]:
        """Return bounded text diagnostic thresholds without retaining page data."""
        cfg = self.cfg.get("diagnostics", {})
        try:
            unknown_after_scans = int(cfg.get("capture_unknown_after_scans", 3))
        except (TypeError, ValueError):
            unknown_after_scans = 3
        try:
            error_cooldown_sec = float(cfg.get("error_capture_cooldown_sec", 30))
        except (TypeError, ValueError):
            error_cooldown_sec = 30.0
        return max(2, min(unknown_after_scans, 10)), max(10.0, error_cooldown_sec)

    def _refresh_hot_payment_settings(self) -> None:
        """Reload only payment keyword options changed by the local dashboard."""
        config_path = str(self.cfg.get("_config_path") or "").strip()
        if not config_path:
            return
        path = Path(config_path)
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            return
        previous_mtime_ns = getattr(self, "_payment_config_mtime_ns", None)
        if previous_mtime_ns is None:
            self._payment_config_mtime_ns = mtime_ns
            return
        if mtime_ns == previous_mtime_ns:
            return
        try:
            with path.open("r", encoding="utf-8") as config_file:
                fresh_cfg = json.load(config_file)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(fresh_cfg, dict):
            return
        payment = fresh_cfg.get("payment", {})
        multi_order = fresh_cfg.get("multi_order", {})
        options = normalize_payment_keyword_options(
            payment.get("keyword_options"),
            multi_order.get("payment_keywords") or payment.get("keywords") or payment.get("keyword", ""),
        )
        if not enabled_payment_keywords(options):
            return
        self.cfg["payment"] = payment
        self.cfg.setdefault("multi_order", {})["payment_keywords"] = enabled_payment_keywords(options)
        self._payment_config_mtime_ns = mtime_ns
        self.multi_payment_index = 0
        print(f"[hot] 收款关键词已更新：{', '.join(enabled_payment_keywords(options))}")

    def payment_keywords(self) -> list[str]:
        self._refresh_hot_payment_settings()
        multi_keywords = self.multi_order_cfg().get("payment_keywords")
        payment = self.cfg.get("payment", {})
        options = normalize_payment_keyword_options(
            payment.get("keyword_options"),
            multi_keywords or payment.get("keywords") or payment.get("keyword") or "",
        )
        return enabled_payment_keywords(options)

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
            self.record_state(
                "low_balance_stop",
                f"搬砖余额 {self.decimal_text(balance)} 小于最小限额 {self.decimal_text(limit_min)}，停止新增挂单。",
            )
            return False
        # Do not leave a balance that cannot create the next order.  When the
        # configured amount fits but its remainder is below the configured
        # minimum, use the form's 全部 control for this order instead.
        use_all_balance = balance < configured_amount or balance - configured_amount < limit_min
        amount = balance if use_all_balance else configured_amount
        configured_limit_max = Decimal(str(self.cfg.get("limit_max") or amount))
        if amount < limit_min:
            self.multi_batch_complete = True
            self.form_values = {}
            print(f"[state] 本次可挂金额 {self.decimal_text(amount)} 小于最小限额，停止新增挂单。")
            return False
        # The app accepts cents for the total amount, but its per-order limit
        # inputs reject decimal values. Round down only the maximum limit so it
        # remains valid and never exceeds the total order amount.
        # 全部变现时，最大限额也要跟随全部金额；否则平台会把订单拆成
        # 配置最大值和一笔不足最低限额的尾单。限额输入框不接受小数，按
        # 平台规则仅对最大值向下取整。
        limit_max_source = amount if use_all_balance else min(configured_limit_max, amount)
        limit_max = limit_max_source.to_integral_value(rounding=ROUND_DOWN)
        if limit_max < limit_min:
            self.multi_batch_complete = True
            self.form_values = {}
            print("[state] 当前余额不足以满足最小限额，停止新增挂单。")
            return False
        self.form_values = {
            "amount": self.decimal_text(amount),
            "limit_min": self.decimal_text(limit_min),
            "limit_max": self.decimal_text(limit_max),
            # Flutter accepts a fractional final balance more reliably through
            # its 全部 button. This also consumes a remainder below the next
            # order's minimum rather than leaving it stranded.
            "use_all_balance": "true" if use_all_balance else "false",
        }
        return True

    def can_act(self) -> bool:
        throttle = float(self.cfg.get("action_throttle_sec", 1.2))
        floor = 0.45 if str(self.cfg.get("speed_mode") or "safe").lower() == "fast" else 0.7
        return time.time() - self.last_action_at >= max(floor, throttle)

    def form_input_timing(self) -> tuple[float, float]:
        """Return focus and OCR-verification waits for the selected speed profile."""
        cfg = self.cfg.get("form_input", {})
        return (
            max(0.08, float(cfg.get("focus_wait_sec", 0.16))),
            max(0.15, float(cfg.get("verify_wait_sec", 0.40))),
        )

    def record_action(self, message: str) -> None:
        self.last_action_at = time.time()
        print(f"[action] {message}")

    def record_state(self, key: str, message: str, interval_sec: float = 30.0) -> None:
        """Throttle repetitive passive state messages without hiding actions or warnings."""
        now = time.time()
        state_log_times = getattr(self, "_state_log_times", {})
        if now - state_log_times.get(key, 0.0) < interval_sec:
            return
        state_log_times[key] = now
        self._state_log_times = state_log_times
        print(f"[state] {message}")

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
        detail_terms = ["出售详情", "搬砖详情", "订单详情"]
        detail_terms.extend(self.texts.get("order_detail", []))
        detail_terms.extend(self.texts.get("buyer_detail", ["买家详情", "订单详情"]))
        detail_terms = list(dict.fromkeys(detail_terms))
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

    def find_trade_success_ack(self, image: Image.Image, boxes: list[TextBox]) -> TextBox | None:
        """Find the acknowledgement button on the non-financial trade-success dialog.

        A completed-order detail page can itself contain words such as ``放行``.  The
        acknowledgement is therefore recognized only when the same visible dialog
        also states that the trade has succeeded; it must never be treated as a
        generic confirmation button.
        """
        width, height = image.size
        success = self.vision.find_text(
            boxes,
            self.texts.get("trade_success", ["交易成功"]),
            prefer_center=True,
            max_text_length=20,
            bounds=(width * 0.20, height * 0.20, width * 0.80, height * 0.68),
        )
        if not success:
            return None
        return self.vision.find_text(
            boxes,
            self.texts.get("trade_success_ack", ["知道了"]),
            prefer_center=True,
            max_text_length=12,
            bounds=(width * 0.20, height * 0.58, width * 0.80, height * 0.92),
        )

    def find_system_notice_dialog(self, image: Image.Image, boxes: list[TextBox]) -> TextBox | None:
        """Recognize the informational system-notice sheet by its own central title.

        The notice can contain warning language such as ``确认转账``.  It is not an
        order confirmation, so it must be routed before generic confirmation guards.
        The handler only uses its close icon and never any button inside the sheet.
        """
        width, height = image.size
        return self.vision.find_text(
            boxes,
            self.texts.get("system_notice", ["安全通知"]),
            prefer_center=True,
            max_text_length=16,
            bounds=(width * 0.20, height * 0.16, width * 0.80, height * 0.52),
        )

    def find_system_ignore_target(self, image: Image.Image, boxes: list[TextBox]) -> TextBox | TemplateMatch | None:
        """Find Ignore only when it belongs to a recognized system modal.

        System reminders can overlay every page.  The Ignore label alone is not
        sufficient evidence because it may belong to ordinary page content, so
        require a configured system-modal title in the same central sheet.
        """
        width, height = image.size
        title_terms = list(self.texts.get("system_dialog", []))
        title_terms.extend(self.texts.get("order_exception", []))
        title_terms.extend(self.texts.get("system_notice", []))
        title_terms = list(dict.fromkeys(title_terms))
        if not title_terms:
            title_terms = ["系统提示", "温馨提示", "安全提示", "安全通知", "订单异常提醒"]
        title = self.vision.find_text(
            boxes,
            title_terms,
            prefer_center=True,
            max_text_length=24,
            bounds=(width * 0.12, height * 0.10, width * 0.88, height * 0.68),
        )
        if not title:
            return None
        return self.vision.find_template(image, "system_dialog_ignore") or self.vision.find_text(
            boxes,
            self.texts.get("system_dialog_ignore", self.texts.get("order_exception_ignore", ["忽略"])),
            prefer_bottom=True,
            max_text_length=12,
            bounds=(width * 0.08, height * 0.45, width * 0.54, height * 0.94),
        )

    def find_submit_confirm_dialog(self, image: Image.Image, boxes: list[TextBox]) -> TextBox | None:
        """Find the bottom-sheet confirmation shown after submitting a cash-out form."""
        width, height = image.size
        title = self.vision.find_text(
            boxes,
            self.texts.get("submit_confirm_title", ["确认变现", "确定变现"]),
            prefer_center=True,
            max_text_length=20,
            bounds=(width * 0.15, height * 0.45, width * 0.85, height * 0.78),
        )
        if not title:
            return None
        return self.vision.find_text(
            boxes,
            ["确认", "确定"],
            prefer_center=True,
            prefer_bottom=True,
            max_text_length=12,
            exclude=self.texts.get("dangerous_confirm", []),
            bounds=(width * 0.10, height * 0.76, width * 0.90, height * 0.99),
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

    def is_withdraw_page_title_visible(self, image: Image.Image, boxes: list[TextBox]) -> bool:
        """Keep a scrolled withdraw form routable when its fields are off-screen."""
        width, height = image.size
        return bool(
            self.vision.find_text(
                boxes,
                ["变现"],
                min_confidence=0.55,
                max_text_length=8,
                bounds=(width * 0.32, 0, width * 0.68, height * 0.14),
            )
        )

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

    def is_withdraw_unavailable_page(self, image: Image.Image, boxes: list[TextBox]) -> bool:
        """Recognize the withdraw page that hides its form when balance is below 10."""
        if self.is_order_form_visible(image, boxes):
            return False
        joined = normalize_text("".join(box.text for box in boxes))
        has_balance = "搬砖余额" in joined or "可变现余额" in joined
        has_minimum_message = "不足10" in joined or "不足于10" in joined
        return has_balance and has_minimum_message

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
        modal_page = classify_modal_page(self, PageContext(None, None, image, boxes))
        if modal_page:
            return modal_page
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
        if self.is_withdraw_unavailable_page(image, boxes):
            return "withdraw_unavailable"
        if self.is_order_form_visible(image, boxes):
            return "withdraw_form"
        if self.is_withdraw_page_title_visible(image, boxes):
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
            "system_notice_dialog": "系统安全通知",
            "password_dialog": "支付密码弹窗",
            "submit_confirm_dialog": "变现二次确认弹窗",
            "trade_success_dialog": "交易成功提示",
            "trade_notification": "交易通知",
            "order_exception_dialog": "订单异常提醒",
            "cancel_confirm_dialog": "取消确认弹窗",
            "waiting_transfer_detail": "待付款订单详情",
            "waiting_transfer": "待付款订单",
            "withdraw_unavailable": "变现页余额不足10",
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
        heading = self.vision.find_text(
            boxes,
            self.texts.get("password_dialog") or ["验证支付密码", "支付密码", "验证密码"],
            prefer_center=True,
            prefer_bottom=True,
            max_text_length=36,
        )
        if heading:
            return heading

        find_slots = getattr(self.vision, "find_password_slots", None)
        slots = find_slots(image) if callable(find_slots) else []
        if len(slots) != 6:
            return None
        first = slots[0]
        last = slots[-1]
        center_x = ((first[0] + first[2] / 2) + (last[0] + last[2] / 2)) / 2
        slot_top = min(slot[1] for slot in slots)
        # Return a synthetic heading so the existing modal/page flow can use the
        # same target resolution even when OCR missed the dialog title.
        return TextBox(
            text="支付密码输入框",
            confidence=0.60,
            left=center_x - image.width * 0.20,
            top=max(0.0, slot_top - image.height * 0.09),
            right=center_x + image.width * 0.20,
            bottom=max(0.0, slot_top - image.height * 0.03),
        )

    def resolve_password_field_target(
        self,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> tuple[float, float] | None:
        """Locate the center of the first six-digit password box from its heading."""
        find_slots = getattr(self.vision, "find_password_slots", None)
        slots = find_slots(image) if callable(find_slots) else []
        if len(slots) == 6:
            first = slots[0]
            return first[0] + first[2] / 2, first[1] + first[3] / 2

        heading = self.find_password_dialog(image, boxes)
        if not heading:
            return None
        width, height = image.size
        heading_x, _ = heading.center
        # The six Flutter boxes are horizontally centered under the heading.  The
        # prior 0.31 ratio landed in the blank margin left of the first box on a
        # 568px-wide canvas, so keyboard events were sent without a focused field.
        # A 0.21 ratio reaches the center of the first visible slot across the
        # supported mobile canvas widths.
        x = heading_x - width * 0.21
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
        while time.time() <= deadline:
            scan = await self.scan_app(browser)
            rect, image, boxes = scan
            modal_page = await self.handle_global_interrupt_modal(browser, rect, image, boxes)
            if modal_page:
                if modal_page == "trade_notification":
                    return None
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
        default_x = width * clamp(x_ratio, 0.08, 0.92)

        # The limit fields sometimes keep their platform default (for example
        # ``10``) in a narrow editable hit area even though their outlined box
        # spans a much wider region. Prefer that existing numeric value when it
        # is on the expected row; an empty field keeps the original midpoint.
        numeric_candidates = [
            box
            for box in boxes
            if re.sub(r"\D", "", box.text)
            and abs(box.center[1] - input_y) <= height * 0.055
            and abs(box.center[0] - default_x) <= width * 0.25
        ]
        if numeric_candidates:
            closest = min(
                numeric_candidates,
                key=lambda box: abs(box.center[0] - default_x) + abs(box.center[1] - input_y),
            )
            return closest.center
        return default_x, input_y

    def resolve_top_left_back_point(self, image: Image.Image, boxes: list[TextBox]) -> tuple[float, float]:
        """Locate the visual page edge before clicking its top-left back arrow.

        Flutter centers its narrow mobile canvas inside a wider desktop view. In
        that layout a static 8% x-coordinate lands in the grey gutter rather
        than on the arrow. Form labels provide a stable left-edge anchor; when
        they are unavailable, retain the configured mobile fallback.
        """
        configured = self.points.get("order_detail_back", [0.08, 0.04])
        fallback_x = float(configured[0])
        fallback_y = float(configured[1])
        width, height = image.size
        anchors = [
            box.left
            for box in boxes
            # Narrow Flutter canvases can start around 3% from the screenshot
            # edge. Keep that first text column; otherwise the first numeric
            # value becomes the false left anchor and the back click misses.
            if width * 0.01 <= box.left <= width * 0.45
            and height * 0.08 <= box.top <= height * 0.92
        ]
        if not anchors:
            return fallback_x, fallback_y
        content_left = min(anchors)
        arrow_x = content_left + clamp(width * 0.01, 4, 10)
        return clamp(arrow_x / width, 0.04, 0.30), fallback_y

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

    async def capture_tick_error(self, browser: BrowserSession, error: Exception) -> None:
        """Log rate-limited browser context without taking or saving another screenshot."""
        _unknown_after_scans, cooldown_sec = self.diagnostics_cfg()
        now = time.time()
        if now - self.last_tick_error_capture_at < cooldown_sec:
            return
        self.last_tick_error_capture_at = now
        try:
            diagnostics = await browser.diagnostics()
            title = str(diagnostics.get("title", ""))[:120]
            url = str(diagnostics.get("url", ""))[:240]
            print(f"[diagnostic] 异常页面信息：{type(error).__name__}; title={title!r}; url={url!r}")
        except Exception as capture_error:
            print(f"[diagnostic] 无法读取异常页面信息：{capture_error}")

    async def scan_app(
        self,
        browser: BrowserSession,
        ocr_region: tuple[float, float, float, float] | None = None,
    ) -> tuple[Rect, Image.Image, list[TextBox]]:
        rect = await browser.app_rect()
        screenshot_started = time.perf_counter()
        image = await browser.screenshot_app(rect)
        screenshot_sec = time.perf_counter() - screenshot_started
        ocr_started = time.perf_counter()
        boxes = self.vision.read_text_region(image, ocr_region) if ocr_region else self.vision.read_text(image)
        self.runtime_monitor.record_scan(screenshot_sec, time.perf_counter() - ocr_started)
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
                print("[state] 检测到交易通知，等待识别同意交易按钮。")
                return True
            if now - self.last_accept_at < 0.5:
                return True
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
                self.record_action("挂单被订单详情打断，返回后检查订单状态")
                back = self.resolve_top_left_back_point(image, boxes)
                await browser.click_relative(rect, float(back[0]), float(back[1]))
                self.last_form_interruption_back_at = now
            else:
                print("[state] 挂单被订单详情打断，等待返回后检查订单状态。")
            return True
        if self.is_waiting_transfer(image, boxes) and not self.is_orders_page(image, boxes):
            if self.is_waiting_transfer_detail(image, boxes):
                if now - self.last_waiting_transfer_back_at >= 1.5:
                    self.record_action("订单待付款，点击左上角返回搬砖列表")
                    back = self.resolve_top_left_back_point(image, boxes)
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
            print("[warn] 识别到订单异常提醒，但未找到忽略按钮，不点击立即查看。")
            return True
        now = time.time()
        if now - self.last_exception_ignore_at < 0.8 or not self.can_act():
            return True
        self.record_action("订单异常提醒，按文字点击忽略")
        self.last_exception_ignore_at = now
        await self.click_visual_target(browser, rect, target)
        return True

    async def handle_system_ignore_dialog(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        """Dismiss a recognized system sheet through its explicit Ignore action."""
        target = self.find_system_ignore_target(image, boxes)
        if not target:
            return True
        now = time.time()
        last_ignore = getattr(self, "last_system_ignore_at", 0.0)
        if now - last_ignore < 0.8 or not self.can_act():
            return True
        self.record_action("系统提示，按文字点击忽略")
        self.last_system_ignore_at = now
        await self.click_visual_target(browser, rect, target)
        return True

    async def handle_trade_success_dialog(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        """Close the completed-trade acknowledgement before reading its detail page."""
        target = self.find_trade_success_ack(image, boxes)
        if not target:
            return True
        now = time.time()
        last_ack = getattr(self, "last_trade_success_ack_at", 0.0)
        if now - last_ack < 0.8 or not self.can_act():
            return True
        self.record_action("交易成功提示，点击知道了关闭")
        await self.click_visual_target(browser, rect, target)
        self.last_trade_success_ack_at = now
        # Closing the success sheet may return directly to 搬砖页. Re-open the
        # orders inspection flow so the script still verifies the resulting
        # order list instead of leaving a stale inspect state behind.
        self.post_accept_order_check = "open"
        self.post_accept_order_check_until = now + 10.0
        return True

    async def handle_system_notice_dialog(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        """Dismiss only the upper-right close icon of the recognized system notice."""
        title = self.find_system_notice_dialog(image, boxes)
        if not title:
            return True
        now = time.time()
        last_close = getattr(self, "last_system_notice_close_at", 0.0)
        if now - last_close < 0.8 or not self.can_act():
            return True

        width, height = image.size
        # The close glyph is not OCR text.  Its position is stable relative to the
        # central “安全通知” title, and this point is used only after that title is
        # recognized in the modal area.
        title_x, title_y = title.center
        close_x = clamp(title_x + width * 0.235, width * 0.58, width * 0.92)
        close_y = clamp(title_y - height * 0.028, height * 0.10, height * 0.45)
        self.record_action("系统安全通知，点击右上角关闭")
        self.last_system_notice_close_at = now
        await browser.click_abs(rect.left + close_x, rect.top + close_y)
        return True

    async def handle_submit_confirm_dialog(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        """Confirm only the bottom sheet produced by this run's cash-out submission."""
        if time.time() > getattr(self, "submit_confirm_until", 0.0):
            self.record_state(
                "stale_submit_confirm",
                "检测到未关联本次提交的变现确认弹窗，等待人工处理。",
                interval_sec=5.0,
            )
            return True
        target = self.find_submit_confirm_dialog(image, boxes)
        if not target or not self.can_act():
            return True
        self.record_action("识别到变现二次确认弹窗，点击确认")
        await self.click_visual_target(browser, rect, target)
        self.submit_confirm_until = 0.0
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
            # This is only a short post-accept observation window.  Leaving it
            # in a permanent blocked state prevented the normal orders-page
            # handler from opening the tab and issuing its pull-to-refresh.
            self.post_accept_order_check = ""
            self.record_state(
                "post_accept_check_timeout",
                "接单后订单确认超时，交由订单页常规检查与下拉刷新。",
                interval_sec=5.0,
            )
            return False

        if self.post_accept_order_check == "open":
            # After accepting, Flutter may navigate straight to 出售详情 rather than
            # back to the tabbed main page. Leave that detail page first; the order
            # tab does not exist there, so waiting for it would otherwise time out.
            if self.is_order_detail_interrupt_page(image, boxes):
                if now - self.last_post_accept_detail_back_at >= 1.5:
                    self.record_action("接单后在订单详情，点击左上角返回订单列表")
                    back = self.resolve_top_left_back_point(image, boxes)
                    await browser.click_relative(rect, float(back[0]), float(back[1]))
                    self.last_post_accept_detail_back_at = now
                else:
                    print("[state] 接单后订单详情正在返回，等待订单页标签出现。")
                return True
            target = self.find_order_page_tab(image, boxes)
            if not target:
                print("[state] 等待订单页标签出现。")
                return True
            self.record_action("返回后点击订单页检查是否有进行中订单")
            await self.click_visual_target(browser, rect, target)
            self.post_accept_order_check = "inspect"
            # The first orders-page scan should refresh immediately, including
            # when a prior observation refreshed the page less than a minute
            # ago.
            self.last_orders_refresh_at = 0.0
            return True

        if self.is_orders_page(image, boxes) and await self.maybe_pull_refresh_orders(browser, rect, image, boxes):
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
            print(f"[warn] 未识别{label}标签，不填写该字段。")
            return False

        self.record_action(f"按文字定位填写{label}")
        focus_wait, verify_wait = self.form_input_timing()
        for attempt in range(2):
            if attempt:
                self.record_action(f"重新填写{label}（第 {attempt + 1} 次）")
                rect, image, boxes = await self.scan_app(browser)
                target = self.resolve_canvas_field_target(image, boxes, label_terms, x_ratio)
                if not target:
                    print(f"[warn] 重试时未识别{label}输入框，停止本次挂单。")
                    return False
            x, y = target
            await browser.click_abs(rect.left + x, rect.top + y)
            await asyncio.sleep(focus_wait)
            if not await browser.replace_numeric(str(value)):
                print(f"[warn] 未获得{label}的 Flutter 输入焦点，准备重试。")
            await asyncio.sleep(verify_wait)

            _, verify_image, verify_boxes = await self.scan_app(browser)
            if self.is_field_value_visible(verify_boxes, str(value), target, verify_image):
                print(f"[state] 已确认{label}显示为 {value}")
                return True
            if attempt == 0:
                self.record_state("field_value_retry", f"{label}视觉校验未通过，重新输入。", interval_sec=5.0)

        print(f"[warn] 未在{label}输入框确认数值 {value}，停止本次挂单。")
        return False

    def configured_amount_matches_limit_max(self) -> bool:
        """The app defaults 最大限额 to the amount only for this matching configuration."""
        configured_amount = self.parse_money(str(self.cfg.get("amount") or ""))
        configured_maximum = self.parse_money(str(self.cfg.get("limit_max") or ""))
        return configured_amount is not None and configured_amount == configured_maximum

    async def keep_default_canvas_limit_max(self, browser: BrowserSession, expected: str) -> bool:
        """Retain the app-provided maximum only after a visual value check."""
        rect, image, boxes = await self.scan_app(browser)
        target = self.resolve_canvas_field_target(
            image,
            boxes,
            self.texts.get("limit_label", ["单笔限额"]),
            0.74,
        )
        if target and self.is_field_value_visible(boxes, expected, target, image):
            print(f"[state] 最大限额已自动同步变现金额：{expected}，跳过填写")
            return True
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
            print("[warn] 未识别变现金额输入框，不点击全部。")
            return False
        width, height = image.size
        target = self.vision.find_text(
            boxes,
            ["全部"],
            # This small blue label is frequently returned by RapidOCR at about
            # 0.5 confidence. It is still safe to accept a lower score here:
            # the match must be the exact word and stay in the amount row's
            # right-side action area, so it cannot select an unrelated control.
            min_confidence=0.35,
            prefer_right=True,
            max_text_length=8,
            bounds=(width * 0.65, field[1] - height * 0.12, width * 0.98, field[1] + height * 0.12),
        )
        if not target:
            print("[warn] 未识别变现金额的全部按钮，不执行坐标点击。")
            return False
        self.record_action("点击变现金额全部")
        await self.click_visual_target(browser, rect, target)
        await asyncio.sleep(0.28)
        _, verify_image, verify_boxes = await self.scan_app(browser)
        actual_balance = self.find_available_balance(verify_image, verify_boxes)
        expected_values = [str(expected_amount)]
        if actual_balance is not None:
            actual_text = self.decimal_text(actual_balance)
            expected_values.append(actual_text)
            # Keep the remaining form steps aligned with the app's live balance.
            # The amount field is rendered as an integer while the balance can
            # still include cents, so the maximum limit follows the rounded value.
            if self.form_values.get("use_all_balance") == "true":
                self.form_values["amount"] = actual_text
                self.form_values["limit_max"] = self.decimal_text(
                    actual_balance.to_integral_value(rounding=ROUND_DOWN)
                )
        rounded_values: list[str] = []
        for value in expected_values:
            try:
                rounded = self.decimal_text(Decimal(value).to_integral_value(rounding=ROUND_DOWN))
            except InvalidOperation:
                continue
            if rounded not in rounded_values:
                rounded_values.append(rounded)

        for value in expected_values:
            if self.is_field_value_visible(verify_boxes, value, field, verify_image):
                print(f"[state] 已确认变现金额全部显示为 {value}")
                return True
        for value in rounded_values:
            if self.is_field_value_visible(verify_boxes, value, field, verify_image):
                print(f"[state] 页面将全部金额取整为 {value}，继续填写限额")
                return True
        print(f"[warn] 点击全部后未确认变现金额 {expected_amount}，停止本次挂单。")
        return False

    async def type_password_with_keypad(self, browser: BrowserSession, rect: Rect, password: str) -> bool:
        keypad = self.points.get("keypad", {})
        for digit in str(password):
            if not digit.isdigit() or digit not in keypad:
                continue
            rx, ry = keypad[digit]
            await browser.click_relative(rect, float(rx), float(ry))
            await asyncio.sleep(0.09)
        await asyncio.sleep(0.12)
        return await browser.active_text_input_value() == str(password)

    async def type_password_with_keyboard(
        self,
        browser: BrowserSession,
        rect: Rect,
        password: str,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        """Fill and verify Flutter's hidden six-digit password input."""
        target = self.resolve_password_field_target(image, boxes)
        if not target:
            print("[warn] 未识别支付密码输入格，不输入密码。")
            return False
        x, y = target
        await browser.click_abs(rect.left + x, rect.top + y)
        await asyncio.sleep(0.12)
        # A six-digit password is submitted exactly once. Do not inspect then
        # repair the hidden Flutter input, and never retry the full password:
        # the page can update asynchronously and a duplicate event may trigger
        # its password-attempt rate limiter.
        return await browser.replace_numeric(str(password), verify_and_repair=False)

    async def handle_runtime_password_dialog(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        """Allow a password entered in the dashboard after the dialog appeared."""
        if getattr(self, "password_dialog_input_pending", False):
            self.record_state(
                "password_dialog_pending",
                "支付密码已输入但弹窗未关闭，暂停重复输入；请检查密码或手动确认。",
                interval_sec=5.0,
            )
            return True
        if getattr(self, "password_dialog_input_attempted", False):
            self.record_state(
                "password_dialog_attempted",
                "支付密码本次输入未确认成功，已停止重复输入；请检查弹窗或手动处理。",
                interval_sec=5.0,
            )
            return True
        now = time.time()
        if now - self.last_password_input_at < 3.0:
            return False
        password = await self.resolve_payment_password()
        if not password:
            print("[state] 支付密码弹窗可见，等待控制台输入支付密码。")
            return False

        self.last_password_input_at = now
        self.password_dialog_input_attempted = True
        password_cfg = self.password_cfg()
        mode = password_cfg.get("input_mode", "keyboard")
        self.record_action("检测到运行中支付密码，输入支付密码")
        if mode == "keypad":
            entered = await self.type_password_with_keypad(browser, rect, password)
        else:
            entered = await self.type_password_with_keyboard(browser, rect, password, image, boxes)
        if not entered:
            print("[warn] 支付密码未确认输入完整，本次挂单停止且不重复输入。")
            return False
        self.password_dialog_input_pending = True
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
            self.record_action(f"取消默认收款方式：{first_card.text}")
            await self.click_payment_card_row(browser, rect, first_card)
            await asyncio.sleep(0.30)

        for attempt in range(attempts):
            # 收款卡会因表单内容、校验提示和页面滚动出现在任意高度；不能假定它
            # 总在下半区，否则会漏掉当前可见的目标卡并持续向下滚动。
            # 静态画面仍由 VisionEngine 的逐像素缓存复用 OCR 结果。
            rect, image, boxes = await self.scan_app(browser)
            target = self.find_payment_card_target(image, boxes, keyword)
            if target:
                self.record_action(f"按文字选择收款卡片：{target.text}")
                await self.click_payment_card_row(browser, rect, target)
                self.payment_selected_for_form = True
                return True

            entry = self.find_payment_picker_entry(image, boxes)
            if entry:
                self.record_action("按文字打开收款方式选择页")
                await self.click_visual_target(browser, rect, entry)
                await asyncio.sleep(0.35)
                continue

            if attempt < attempts - 1:
                self.record_action(f"未找到收款卡片：{keyword}，在卡片区域向下滚动 {attempt + 1}/{attempts}")
                await browser.wheel_relative(rect, 0.5, 0.76, 520)
                await asyncio.sleep(0.42)

        print(f"[warn] 未按文字找到收款卡片：{keyword}，不使用卡片序号坐标兜底。")
        return False

    async def click_payment_card_row(self, browser: BrowserSession, rect: Rect, target: TextBox) -> None:
        _, y = target.center
        x = rect.width * float(self.points.get("payment_card_x", 0.5))
        y = clamp(y, 20, rect.height - 70)
        await browser.click_abs(rect.left + x, rect.top + y)

    async def submit_canvas_order_form(self, browser: BrowserSession, rect: Rect) -> bool:
        self.form_interrupted_by_trade_notification = False
        cfg = self.submit_flow_cfg()
        rect, image, boxes = await self.scan_app(browser)
        submit = self.find_bottom_action_target(image, boxes, self.texts.get("submit", []))
        if not submit:
            print("[warn] 未识别底部确认变现，不执行坐标点击。")
            return False

        self.record_action("按文字点击底部确认变现")
        self.submit_confirm_until = time.time() + max(10.0, cfg["secondary_confirm_timeout_sec"] + 5.0)
        await self.click_visual_target(browser, rect, submit)

        if not await self.click_secondary_confirm(browser):
            return False
        self.submit_confirm_until = 0.0
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
                entered = await self.type_password_with_keypad(browser, rect, password)
            else:
                entered = await self.type_password_with_keyboard(
                    browser,
                    rect,
                    password,
                    password_scan[1],
                    password_scan[2],
                )
            if not entered:
                self.password_dialog_input_attempted = True
                print("[warn] 支付密码未确认输入完整，本次挂单停止且不重复输入。")
                return False
            self.password_dialog_input_pending = True
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
            self.record_action("检测到支付密码确认按钮，点击确认")
            await self.click_visual_target(browser, rect, target)
            return True

        print("[warn] 支付密码弹窗仍在但未识别确认文字，不执行坐标点击。")
        return False

    async def click_secondary_confirm(self, browser: BrowserSession) -> bool:
        cfg = self.submit_flow_cfg()
        started = time.time()

        while time.time() - started <= cfg["secondary_confirm_timeout_sec"]:
            await asyncio.sleep(max(0.15, cfg["state_poll_sec"]))
            rect, image, boxes = await self.scan_app(browser)
            elapsed = time.time() - started

            modal_page = await self.handle_global_interrupt_modal(browser, rect, image, boxes)
            if modal_page:
                # ``handle_incoming_order`` records the form interruption. Let
                # the existing password-state wait observe that interruption and
                # hand control back to the orders flow without clicking anything
                # beneath the notification.
                if modal_page == "trade_notification":
                    return True
                await asyncio.sleep(max(0.15, cfg["state_poll_sec"]))
                continue

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

            target = self.find_submit_confirm_dialog(image, boxes) or self.vision.find_template(image, "secondary_confirm") or self.find_bottom_action_target(
                image,
                boxes,
                self.texts.get("secondary_confirm", self.texts.get("confirm", [])),
            )

            if target:
                self.record_action("按文字点击底部二次确认")
                await self.click_visual_target(browser, rect, target)
                return True

            print(
                "[state] 等待识别底部二次确认 "
                f"{elapsed:.1f}/{cfg['secondary_confirm_timeout_sec']:.1f}s"
            )

        print("[warn] 未检测到底部二次确认，不执行坐标点击。")
        return False

    async def fill_and_submit_canvas_order_form(self, browser: BrowserSession, rect: Rect) -> bool:
        now = time.time()
        if now - self.last_form_submit_at < 5:
            return True
        self.last_form_submit_at = now
        # A new fill attempt may recover normally; only a failed attempt should
        # arm the one-shot back navigation below.
        self.failed_form_exit_attempted = False

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
        limit_max = values.get("limit_max") or str(self.cfg.get("limit_max") or "")
        max_is_default = self.configured_amount_matches_limit_max() and await self.keep_default_canvas_limit_max(
            browser,
            limit_max,
        )
        if not max_is_default and not await self.fill_canvas_text_field(
            browser,
            "最大限额",
            self.texts.get("limit_label", ["单笔限额"]),
            0.74,
            limit_max,
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
            scroll_step = self.multi_cancel_scroll_step_px()
            if not target and self.multi_cancel_scroll_steps:
                self.record_action("未找到进行中订单，向上回滚取消列表")
                await browser.wheel_relative(rect, 0.50, 0.66, -scroll_step)
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
                        print(f"[warn] 已向下扫描 {max_steps} 次仍未确认活动订单尾部，不点击取消。")
                        return True
                    self.record_action(
                        f"按先进先出继续向下查找最尾部挂单 "
                        f"{self.multi_cancel_scroll_steps + 1}/{max_steps}"
                    )
                    await browser.wheel_relative(rect, 0.50, 0.66, scroll_step)
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
                print("[warn] 未识别顶部搬砖余额，不新增挂单。")
                return True
            if not self.prepare_multi_order_values(balance):
                return True

        target = self.find_start_order_target(image, boxes)
        if not target:
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
                    self.record_state("form_submission_failed", "本次挂单未完成视觉校验，暂停重试，请检查浏览器页面和运行日志。")
                    return True
                if self.is_order_form_visible(image, boxes):
                    return await self.fill_and_submit_canvas_order_form(browser, rect)
                self.record_state("form_waiting", "尚未检测到可填写的变现表单。", interval_sec=5.0)
                return True
            self.form_expected_after = 0
            self.form_expected_until = 0

        if self.is_order_form_visible(image, boxes):
            if self.form_submission_failed:
                self.record_state("form_submission_failed", "本次挂单未完成视觉校验，暂停重试，请检查浏览器页面和运行日志。")
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

        if pending_count:
            self.multi_pending_probe_steps = 0
            self.multi_pending_probe_returning = False
            self.multi_pending_probe_at_top = False
            self.last_pending_probe_completed_at = 0.0

        if pending_count and not self.multi_pending_since and self.multi_order_cfg().get("adopt_existing_pending_orders", False):
            self.multi_pending_since = [time.time()] * pending_count
            print(f"[state] 已接管启动前已有的 {pending_count} 笔挂单，从本次启动开始重新计时。")

        balance = self.find_available_balance(image, boxes)
        if balance is None:
            print("[warn] 未识别顶部搬砖余额，不新增挂单。")
            return True
        minimum_remaining = Decimal(str(self.cfg.get("limit_min") or "0"))
        if pending_count == 0 and self.multi_pending_since:
            if balance >= minimum_remaining:
                self.multi_pending_since.clear()
                print("[state] 余额已恢复且未检测到待接单订单，清除手动取消后的本地计时。")
            else:
                self.record_state("no_pending_low_balance", "当前视图未识别待接单订单，但余额未恢复，保留本地计时。")
                # A tail-cancel scan already has a precise return path. Do not
                # replace it with the generic viewport probe mid-recovery.
                if not getattr(self, "multi_cancel_scroll_steps", 0):
                    return await self.probe_hidden_pending_orders(browser, rect)
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
            self.record_state("multi_batch_low_balance", "多挂单批次已完成，余额不足阈值，停止新增挂单。")
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
        self.record_action("订单页每分钟下拉刷新")
        # Start below the status tabs and above the bottom navigation. The gesture
        # lands in empty list content even when a single order card is visible.
        await browser.drag_relative(rect, 0.50, 0.42, 0.50, 0.70)
        await asyncio.sleep(0.45)
        return True

    async def handle_withdraw_form_page(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image | None = None,
        boxes: list[TextBox] | None = None,
    ) -> bool:
        """Only the withdraw form handler may type form values or submit a new order."""
        if self.form_submission_failed:
            if getattr(self, "failed_form_exit_attempted", False):
                self.record_state(
                    "form_submission_failed",
                    "变现表单校验失败且已尝试返回，暂停操作等待页面变化或人工检查。",
                    interval_sec=5.0,
                )
                return True
            now = time.time()
            last_back = getattr(self, "last_failed_form_back_at", 0.0)
            if now - last_back < 2.0 or not self.can_act():
                self.record_state("form_submission_failed", "变现表单视觉校验失败，等待返回搬砖页。", interval_sec=5.0)
                return True
            self.record_action("变现表单视觉校验失败，点击左上角返回搬砖页重新读取余额")
            if image is not None and boxes is not None:
                back = self.resolve_top_left_back_point(image, boxes)
                await browser.click_relative(rect, float(back[0]), float(back[1]))
            else:
                back = self.points.get("order_detail_back", [0.08, 0.04])
                await browser.click_relative(rect, float(back[0]), float(back[1]))
            self.failed_form_exit_attempted = True
            self.last_failed_form_back_at = now
            self.next_post_after = now + 1.5
            return True
        return await self.fill_and_submit_canvas_order_form(browser, rect)

    async def handle_withdraw_unavailable_page(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        """Leave the empty withdraw page so the bot can keep observing the move page."""
        now = time.time()
        last_back = getattr(self, "last_withdraw_unavailable_back_at", 0.0)
        if now - last_back < 2.0 or not self.can_act():
            return True
        self.form_expected_after = 0.0
        self.form_expected_until = 0.0
        self.form_submission_failed = False
        self.form_values = {}
        self.payment_selected_for_form = False
        self.record_action("变现余额不足10，返回搬砖页等待余额恢复")
        back = self.resolve_top_left_back_point(image, boxes)
        await browser.click_relative(rect, *back)
        self.last_withdraw_unavailable_back_at = now
        self.next_post_after = now + 1.5
        self.move_page_opened = True
        return True

    async def handle_order_detail_page(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        """Leave non-payment order details through the top-left back button."""
        now = time.time()
        last_back = getattr(self, "last_order_detail_back_at", 0.0)
        if now - last_back < 1.5 or not self.can_act():
            return True
        self.record_action("订单详情页，点击左上角返回")
        back = self.resolve_top_left_back_point(image, boxes)
        await browser.click_relative(rect, float(back[0]), float(back[1]))
        self.last_order_detail_back_at = now
        return True

    async def open_move_page(self, browser: BrowserSession, rect: Rect, image: Image.Image, boxes: list[TextBox]) -> bool:
        target = self.find_move_page_tab(image, boxes)
        if not target:
            print("[state] 未检测到搬砖标签，等待页面加载。")
            return True
        if not self.can_act():
            return True
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
        capture_after_scans, _error_cooldown_sec = self.diagnostics_cfg()
        if self.unknown_page_streak == capture_after_scans:
            print(f"[diagnostic] 未知页面持续 {capture_after_scans} 次扫描，请检查浏览器页面和运行日志。")
        if self.unknown_page_streak < consecutive_scans:
            print(f"[state] 未识别当前页面，等待刷新阈值 {self.unknown_page_streak}/{consecutive_scans}。")
            return True

        now = time.time()
        last_reload = getattr(self, "last_unknown_page_reload_at", 0.0)
        if now - last_reload < cooldown_sec:
            print("[state] 未识别页面仍在刷新冷却期，不执行任何点击。")
            return True

        self.record_action("连续未识别页面，刷新当前网页重试")
        self.last_unknown_page_reload_at = now
        self.unknown_page_streak = 0
        await browser.reload()
        return True

    async def handle_modal_page(
        self,
        page: str,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        """Delegate high-priority dialog handling to ``canvas_bot.dialogs``."""
        return await dispatch_modal(self, page, PageContext(browser, rect, image, boxes))

    async def handle_global_interrupt_modal(
        self,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> str | None:
        """Consume a system or trade modal before a synchronous workflow continues."""
        page = classify_modal_page(self, PageContext(browser, rect, image, boxes))
        if not is_global_interrupt_modal(page):
            return None
        if page == "trade_notification":
            print("[state] 提交等待期间检测到交易通知，优先处理来单。")
        else:
            print("[state] 提交等待期间检测到系统提示，优先忽略或关闭。")
        await self.handle_modal_page(page, browser, rect, image, boxes)
        return page

    async def handle_content_page(
        self,
        page: str,
        browser: BrowserSession,
        rect: Rect,
        image: Image.Image,
        boxes: list[TextBox],
    ) -> bool:
        """Delegate normal page handling to ``canvas_bot.pages``."""
        return await dispatch_page(self, page, PageContext(browser, rect, image, boxes))

    async def tick(self, browser: BrowserSession) -> None:
        rect = await browser.app_rect()
        screenshot_started = time.perf_counter()
        image = await browser.screenshot_app(rect)
        screenshot_sec = time.perf_counter() - screenshot_started
        ocr_started = time.perf_counter()
        boxes = self.vision.read_text(image)
        self.runtime_monitor.record_scan(screenshot_sec, time.perf_counter() - ocr_started)

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

        if page != "password_dialog":
            self.password_dialog_input_pending = False
            self.password_dialog_input_attempted = False

        if page != "unknown":
            self.unknown_page_streak = 0

        # A manual navigation away from a failed form is an explicit recovery.
        # Forget the stale failure, then let the handler for the new page decide.
        if page != "withdraw_form" and self.form_submission_failed:
            self.form_submission_failed = False
            self.form_expected_after = 0
            self.form_expected_until = 0
            self.form_values = {}
            print("[state] 已离开失败的变现表单，恢复按当前页面识别。")

        if await self.handle_modal_page(page, browser, rect, image, boxes):
            return
        await self.handle_content_page(page, browser, rect, image, boxes)

    async def run(self) -> None:
        async with BrowserSession(self.cfg) as browser:
            print(f"[boot] OCR: {self.vision.ocr_name}")
            unknown_scans, unknown_cooldown = self.unknown_page_refresh_cfg()
            diagnostic_unknown_scans, diagnostic_error_cooldown = self.diagnostics_cfg()
            profile_mode = str(self.cfg.get("browser_profile", {}).get("mode", "managed")).lower()
            print(
                "[boot] "
                f"profile={profile_mode} speed={self.cfg.get('speed_mode', 'safe')} "
                f"poll={float(self.cfg.get('poll_interval_sec', 0.8)):.2f}s "
                f"action_floor={float(self.cfg.get('action_throttle_sec', 0.7)):.2f}s "
                f"unknown_reload={unknown_scans} scans/{unknown_cooldown:.0f}s "
                f"diagnostic_unknown={diagnostic_unknown_scans} scans "
                f"diagnostic_error_cooldown={diagnostic_error_cooldown:.0f}s"
            )
            print("[boot] Browser ready. Press Ctrl+C to stop.")
            while True:
                stop_file = os.getenv("QJD67_STOP_FILE", "").strip()
                if stop_file and Path(stop_file).is_file():
                    print("[stop] 收到控制台停止请求，正在安全关闭浏览器。")
                    return
                tick_started = time.perf_counter()
                try:
                    await self.tick(browser)
                except Exception as exc:
                    if "Target page, context or browser has been closed" in str(exc):
                        print("[error] 浏览器页面已关闭，脚本停止。")
                        break
                    print(f"[error] tick failed: {exc}")
                    await self.capture_tick_error(browser, exc)
                await self.runtime_monitor.report_if_due(time.perf_counter() - tick_started)
                await asyncio.sleep(float(self.cfg.get("poll_interval_sec", 0.8)))


async def snapshot(cfg: dict[str, Any]) -> None:
    bot = CanvasBot(cfg)
    async with BrowserSession(cfg) as browser:
        diag = await browser.diagnostics()
        rect = await browser.app_rect()
        image = await browser.screenshot_app(rect)
        boxes = bot.vision.read_text(image)
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

    requested_ocr_device = str(cfg.get("vision", {}).get("ocr_device", "cuda")).strip().lower()
    try:
        if requested_ocr_device in {"cuda", "gpu", "nvidia"}:
            prepare_cuda_runtime()
        import onnxruntime as ort  # type: ignore

        providers = ort.get_available_providers()
        check("onnxruntime", True, ", ".join(providers))
        if requested_ocr_device in {"cuda", "gpu", "nvidia"}:
            check("ocr-cuda", "CUDAExecutionProvider" in providers, "CUDAExecutionProvider required")
        elif requested_ocr_device in {"directml", "dml"}:
            check("ocr-directml", "DmlExecutionProvider" in providers, "DmlExecutionProvider required")
    except Exception as exc:
        check("onnxruntime", False, str(exc))

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
        "snapshot": "Open browser and print OCR text from an in-memory screenshot.",
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
        try:
            await CanvasBot(cfg).run()
        except Exception as exc:
            print(f"[error] 脚本启动失败：{exc}")
            return 1
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
