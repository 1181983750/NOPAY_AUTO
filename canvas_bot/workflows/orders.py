"""Order workflow entry points kept separate from page routing."""

from __future__ import annotations

from typing import Any

from ..core.context import PageContext


class OrderWorkflow:
    """Owns workflow-level transitions initiated from page handlers."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    async def run_move_page(self, context: PageContext) -> bool:
        self.bot.move_page_opened = True
        return await self.bot.handle_order_workflow(
            context.browser,
            context.rect,
            context.image,
            context.boxes,
        )

    async def run_withdraw_form(self, context: PageContext) -> bool:
        self.bot.move_page_opened = True
        return await self.bot.handle_withdraw_form_page(context.browser, context.rect)
