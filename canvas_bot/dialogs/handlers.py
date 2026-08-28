"""Modal routes. A dialog always takes priority over its underlying page."""

from __future__ import annotations

from typing import Any

from ..core.context import PageContext


# These sheets can cover any underlying page, including a form while a workflow
# is synchronously waiting for Flutter to navigate. Keep their ordering here so
# both the main tick and in-workflow scans make the same decision.
GLOBAL_INTERRUPT_MODAL_PAGES = frozenset(
    {
        "system_ignore_dialog",
        "system_notice_dialog",
        "trade_notification",
    }
)


def classify_modal_page(bot: Any, context: PageContext) -> str | None:
    """Return the highest-priority modal visible in this OCR scan, if any."""
    image = context.image
    boxes = context.boxes

    # A system sheet with an explicit Ignore action is harmless to dismiss and
    # must win over every underlying page or confirmation workflow.
    if bot.find_system_ignore_target(image, boxes):
        return "system_ignore_dialog"
    if bot.find_system_notice_dialog(image, boxes):
        return "system_notice_dialog"
    if bot.find_trade_notification(image, boxes):
        return "trade_notification"
    if bot.find_submit_confirm_dialog(image, boxes):
        return "submit_confirm_dialog"
    if bot.find_trade_success_ack(image, boxes):
        return "trade_success_dialog"
    if bot.find_order_exception_dialog(image, boxes):
        return "order_exception_dialog"
    if getattr(bot, "cancel_confirm_until", 0) and bot.find_cancel_confirm_target(image, boxes):
        return "cancel_confirm_dialog"
    if bot.find_password_dialog(image, boxes):
        return "password_dialog"
    return None


def is_global_interrupt_modal(page: str | None) -> bool:
    """Whether a modal must interrupt an in-progress page workflow immediately."""
    return page in GLOBAL_INTERRUPT_MODAL_PAGES


async def dispatch_modal(bot: Any, page: str, context: PageContext) -> bool:
    handlers = {
        "system_ignore_dialog": bot.handle_system_ignore_dialog,
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
