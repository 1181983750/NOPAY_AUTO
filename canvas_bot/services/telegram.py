"""Telegram Bot API notification adapter."""

from __future__ import annotations

import json
import os
import urllib.request


def _user_environment_value(name: str) -> str:
    value = str(os.getenv(name) or "").strip()
    if value or os.name != "nt":
        return value
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0] or "").strip()
    except (FileNotFoundError, OSError):
        return ""


class TelegramNotifier:
    """Best-effort Telegram text notifications using a bot token and chat ID."""

    name = "Telegram"

    def __init__(self, token: str | None = None, chat_id: str | None = None) -> None:
        self.token = str(token if token is not None else _user_environment_value("QJD67_TELEGRAM_BOT_TOKEN")).strip()
        self.chat_id = str(chat_id if chat_id is not None else _user_environment_value("QJD67_TELEGRAM_CHAT_ID")).strip()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, title: str, content: str) -> str:
        if not self.enabled:
            return "disabled"
        payload = json.dumps(
            {
                "chat_id": self.chat_id,
                "text": f"{title}\n{content}",
                "disable_web_page_preview": True,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=8) as response:  # noqa: S310 - Telegram public HTTPS API
            result = json.loads(response.read().decode("utf-8", errors="replace"))
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API rejected the message: {result.get('description', 'unknown error')}")
        return "ok"
