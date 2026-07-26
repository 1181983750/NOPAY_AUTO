"""Modal routes. A dialog always takes priority over its underlying page."""

from __future__ import annotations

from typing import Any

from ..core.context import PageContext


async def dispatch_modal(bot: Any, page: str, context: PageContext) -> bool:
    handlers = {
        "system_notice_dialog": bot.handle_system_notice_dialog,
        "submit_confirm_dialog": bot.handle_submit_confirm_dialog,
        "trade_success_dialog": bot.handle_trade_success_dialog,
        "trade_notification": bot.handle_incoming_order,
        "order_exception_dialog": bot.handle_order_exception_dialog,
        "cancel_confirm_dialog": bot.handle_cancel_confirm_dialog,
        "password_dialog": bot.handle_runtime_password_dialog,
    }
    handler = handlers.get(page)
    if handler is None:
        return False
    await handler(context.browser, context.rect, context.image, context.boxes)
    return True
