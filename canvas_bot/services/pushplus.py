"""PushPlus WeChat notification adapter."""

from __future__ import annotations

import json
import os
import urllib.request


class PushPlusNotifier:
    """Best-effort low-frequency notifications through the PushPlus WeChat channel."""

    name = "PushPlus"
    endpoint = "https://www.pushplus.plus/send/"

    def __init__(self) -> None:
        self.token = str(os.getenv("QJD67_PUSHPLUS_TOKEN") or "").strip()
        if not self.token and os.name == "nt":
            try:
                import winreg

                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                    self.token = str(winreg.QueryValueEx(key, "QJD67_PUSHPLUS_TOKEN")[0] or "").strip()
            except (FileNotFoundError, OSError):
                pass

    def dashboard_token(self) -> str:
        base_url = str(os.getenv("QJD67_DASHBOARD_URL") or "").rstrip("/")
        dashboard_token = str(os.getenv("QJD67_DASHBOARD_TOKEN") or "")
        if not base_url or not dashboard_token:
            return ""
        request = urllib.request.Request(f"{base_url}/api/internal/pushplus", headers={"X-Dashboard-Token": dashboard_token})
        try:
            with urllib.request.urlopen(request, timeout=1.2) as response:  # noqa: S310 - local token endpoint
                return str(json.loads(response.read().decode("utf-8")).get("token") or "").strip()
        except Exception:
            return ""

    def active_token(self) -> str:
        return self.dashboard_token() or self.token

    @property
    def enabled(self) -> bool:
        return bool(self.active_token())

    def send(self, title: str, content: str) -> str:
        token = self.active_token()
        if not token:
            return "disabled"
        payload = json.dumps(
            {
                "token": token,
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
