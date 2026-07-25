"""One handler per recognized non-modal page."""

from __future__ import annotations

import time
from typing import Any

from ..core.context import PageContext
from ..workflows.orders import OrderWorkflow


async def handle_auth_page(bot: Any, _context: PageContext) -> bool:
    print("[state] 当前是登录/注册页，请先手动登录；不刷新页面。")
    return True


async def handle_withdraw_unavailable_page(bot: Any, context: PageContext) -> bool:
    return await bot.handle_withdraw_unavailable_page(context.browser, context.rect)


async def handle_withdraw_form_page(bot: Any, context: PageContext) -> bool:
    return await OrderWorkflow(bot).run_withdraw_form(context)


async def handle_orders_page(bot: Any, context: PageContext) -> bool:
    return await bot.handle_orders_list_page(context.browser, context.rect, context.image, context.boxes)


async def handle_move_page(bot: Any, context: PageContext) -> bool:
    return await OrderWorkflow(bot).run_move_page(context)


async def handle_waiting_page(bot: Any, _context: PageContext) -> bool:
    bot.record_state("order_detail_loading", "订单详情正在加载，等待订单状态文字出现。", interval_sec=5.0)
    return True


async def handle_order_detail_page(bot: Any, context: PageContext) -> bool:
    return await bot.handle_order_detail_page(context.browser, context.rect, context.image, context.boxes)


async def handle_navigation_page(bot: Any, context: PageContext) -> bool:
    return await bot.open_move_page(context.browser, context.rect, context.image, context.boxes)


async def handle_unknown_page(bot: Any, context: PageContext) -> bool:
    return await bot.handle_unknown_page(context.browser, context.rect, context.image, context.boxes)


async def dispatch_page(bot: Any, page: str, context: PageContext) -> bool:
    """Dispatch a normal page after modal routing and cross-page guards."""
    if page == "unknown":
        return await handle_unknown_page(bot, context)

    if page in {"waiting_transfer", "waiting_transfer_detail"}:
        await bot.notify_once("waiting_transfer", "待付款", "订单已进入待付款状态，请留意买家转账。")

    if await bot.handle_post_accept_order_check(context.browser, context.rect, context.image, context.boxes):
        return True
    if await bot.handle_incoming_order(context.browser, context.rect, context.image, context.boxes):
        return True

    handlers = {
        "auth_page": handle_auth_page,
        "withdraw_unavailable": handle_withdraw_unavailable_page,
        "withdraw_form": handle_withdraw_form_page,
        "orders_empty": handle_orders_page,
        "orders_list": handle_orders_page,
        "move_page": handle_move_page,
        "waiting_transfer": handle_waiting_page,
        "waiting_transfer_detail": handle_waiting_page,
        "order_detail": handle_order_detail_page,
        "navigation": handle_navigation_page,
    }
    handler = handlers.get(page)
    if handler is not None:
        return await handler(bot, context)

    if bot.form_expected_until and time.time() <= bot.form_expected_until:
        bot.record_state("form_loading", "等待变现页面加载。", interval_sec=5.0)
        return True
    return False
