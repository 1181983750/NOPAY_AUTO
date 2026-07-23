import asyncio
import time
import unittest
from decimal import Decimal

from PIL import Image

from canvas_bot.__main__ import CanvasBot, ROOT, Rect, TextBox, VisionEngine, load_config


class PendingStateTests(unittest.TestCase):
    def test_multi_order_plans_remaining_balance_and_rotates_card_keywords(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "amount": "3000",
            "limit_min": "1000",
            "limit_max": "3000",
            "payment": {"keyword": "备用"},
            "multi_order": {
                "enabled": True,
                "payment_keywords": ["魔仙", "小明"],
            },
        }
        bot.multi_payment_index = 0
        bot.multi_batch_complete = False
        bot.form_values = {}

        self.assertEqual(bot.current_payment_keyword(), "魔仙")
        self.assertTrue(bot.prepare_multi_order_values(Decimal("1816")))
        self.assertEqual(bot.form_values, {"amount": "1816", "limit_min": "1000", "limit_max": "1816", "use_all_balance": "true"})
        self.assertTrue(bot.prepare_multi_order_values(Decimal("1816.75")))
        self.assertEqual(bot.form_values, {"amount": "1816.75", "limit_min": "1000", "limit_max": "1816", "use_all_balance": "true"})
        bot.multi_payment_index = 1
        self.assertEqual(bot.current_payment_keyword(), "小明")
        self.assertFalse(bot.prepare_multi_order_values(Decimal("999.99")))
        self.assertTrue(bot.multi_batch_complete)

    def test_balance_reader_uses_top_balance_not_historical_order_amount(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"texts": {"balance": ["搬砖余额"]}}
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (506, 900), "white")
        boxes = [
            TextBox("搬砖余额", 0.99, 30, 125, 150, 158),
            TextBox("10,459.88", 0.99, 30, 168, 170, 205),
            TextBox("4", 0.99, 30, 285, 45, 315),
            TextBox("19,097", 0.99, 30, 650, 130, 690),
        ]

        self.assertEqual(bot.find_available_balance(image, boxes), Decimal("10459.88"))

    def test_manual_cancellation_with_restored_balance_resumes_multi_order_batch(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "texts": {"balance": ["搬砖余额"], "pending": ["变现中"], "done": ["已取消"], "cancel": ["取消"]},
            "multi_order": {"enabled": True},
        }
        bot.multi_batch_complete = True
        bot.multi_pending_since = [123.0]
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (506, 900), "white")
        boxes = [
            TextBox("搬砖余额", 0.99, 30, 125, 150, 158),
            TextBox("10,459.88", 0.99, 30, 168, 170, 205),
            TextBox("已取消", 0.99, 380, 400, 460, 430),
        ]
        started: list[bool] = []

        async def start_order(*_args) -> bool:
            started.append(True)
            return True

        bot.start_order_flow = start_order
        bot.save_action_debug = lambda *_args: None

        self.assertTrue(asyncio.run(bot.handle_multi_order_workflow(None, Rect(0, 0, 506, 900), image, boxes)))
        self.assertEqual(started, [True])
        self.assertEqual(bot.multi_pending_since, [])
        self.assertFalse(bot.multi_batch_complete)

    def test_scrolled_view_without_pending_keeps_multi_timer_when_balance_is_still_locked(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "limit_min": "1000",
            "texts": {"balance": ["搬砖余额"], "pending": ["变现中"], "done": ["已取消"]},
            "multi_order": {"enabled": True},
        }
        bot.multi_batch_complete = True
        bot.multi_pending_since = [123.0]
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (506, 900), "white")
        boxes = [
            TextBox("搬砖余额", 0.99, 30, 125, 150, 158),
            TextBox("4", 0.99, 30, 168, 70, 205),
        ]
        started: list[bool] = []

        async def start_order(*_args) -> bool:
            started.append(True)
            return True

        bot.start_order_flow = start_order
        bot.save_action_debug = lambda *_args: None

        self.assertTrue(asyncio.run(bot.handle_multi_order_workflow(None, Rect(0, 0, 506, 900), image, boxes)))
        self.assertEqual(bot.multi_pending_since, [123.0])
        self.assertEqual(started, [])

    def test_existing_pending_orders_can_be_adopted_from_current_start_time(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "limit_min": "1000",
            "texts": {"balance": ["搬砖余额"], "pending": ["变现中"], "done": ["已取消"]},
            "multi_order": {"enabled": True, "adopt_existing_pending_orders": True},
        }
        bot.multi_batch_complete = True
        bot.multi_pending_since = []
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (506, 900), "white")
        boxes = [
            TextBox("搬砖余额", 0.99, 30, 125, 150, 158),
            TextBox("4", 0.99, 30, 168, 70, 205),
            TextBox("变现中", 0.99, 390, 420, 470, 450),
        ]
        bot.save_action_debug = lambda *_args: None

        self.assertTrue(asyncio.run(bot.handle_multi_order_workflow(None, Rect(0, 0, 506, 900), image, boxes)))
        self.assertEqual(len(bot.multi_pending_since), 1)

    def test_empty_orders_page_returns_to_move_page_before_reading_balance(self) -> None:
        class FakeBrowser:
            def __init__(self) -> None:
                self.clicked: list[str] = []

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "texts": {
                "move_page_tab": ["搬砖"],
                "order_empty": ["暂无数据"],
                "pending": ["变现中"],
                "done": ["已取消"],
                "balance": ["搬砖余额"],
            },
            "multi_order": {"enabled": True},
        }
        bot.multi_pending_since = [123.0]
        bot.multi_batch_complete = True
        bot.move_page_opened = True
        bot.last_action_at = 0.0
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (506, 900), "white")
        boxes = [
            TextBox("搬砖", 0.99, 120, 14, 180, 55),
            TextBox("进行中", 0.99, 20, 92, 95, 125),
            TextBox("暂无数据", 0.99, 190, 520, 315, 555),
        ]
        browser = FakeBrowser()

        async def save_debug(*_args) -> None:
            return None

        async def click_target(_browser, _rect, target) -> None:
            browser.clicked.append(target.text)

        bot.save_action_debug = save_debug
        bot.click_visual_target = click_target

        self.assertTrue(asyncio.run(bot.handle_multi_order_workflow(browser, Rect(0, 0, 506, 900), image, boxes)))
        self.assertEqual(browser.clicked, ["搬砖"])
        self.assertEqual(bot.multi_pending_since, [])
        self.assertFalse(bot.multi_batch_complete)

    def test_active_order_is_distinguished_from_cancelled_history(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "texts": {
                "active_order": ["待付款", "待接单", "待放行"],
                "done": ["已取消", "已完成"],
            }
        }
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (508, 900), "white")

        self.assertTrue(bot.find_active_order_anchor(image, [TextBox("待付款", 0.99, 300, 300, 370, 330)]))
        self.assertIsNone(bot.find_active_order_anchor(image, [TextBox("已取消", 0.99, 300, 300, 370, 330)]))

    def test_multi_order_cancel_uses_lowest_visible_cancel_button(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"texts": {"cancel": ["取消"], "done": ["已取消"]}}
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (506, 900), "white")
        top_cancel = TextBox("取消", 0.99, 420, 320, 480, 350)
        bottom_cancel = TextBox("取消", 0.99, 420, 720, 480, 750)

        self.assertIs(bot.find_bottom_cancel_target(image, [top_cancel, bottom_cancel]), bottom_cancel)

    def test_multi_order_cancel_scrolls_down_in_small_steps_before_clicking(self) -> None:
        class FakeBrowser:
            def __init__(self) -> None:
                self.wheels: list[tuple[float, float, float]] = []

            async def wheel_relative(self, _rect, x: float, y: float, delta: float) -> None:
                self.wheels.append((x, y, delta))

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "action_throttle_sec": 0.1,
            "texts": {"cancel": ["取消"], "pending": ["变现中"], "done": ["已取消"]},
            "multi_order": {"enabled": True},
        }
        bot.last_cancel_at = 0.0
        bot.last_action_at = 0.0
        bot.multi_cancel_scroll_steps = 0
        bot.multi_cancel_recovering = False
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (506, 900), "white")
        top_cancel = TextBox("取消", 0.99, 420, 320, 480, 350)
        browser = FakeBrowser()

        self.assertTrue(asyncio.run(bot.cancel_pending_order(browser, Rect(0, 0, 506, 900), image, [top_cancel])))
        self.assertEqual(browser.wheels, [(0.50, 0.66, 360)])
        self.assertEqual(bot.multi_cancel_scroll_steps, 1)

    def test_multi_order_tail_scan_does_not_stop_after_four_visible_cards(self) -> None:
        class FakeBrowser:
            def __init__(self) -> None:
                self.wheels: list[tuple[float, float, float]] = []

            async def wheel_relative(self, _rect, x: float, y: float, delta: float) -> None:
                self.wheels.append((x, y, delta))

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "action_throttle_sec": 0.1,
            "texts": {"cancel": ["取消"], "done": ["已取消", "已完成"]},
            "multi_order": {"enabled": True, "cancel_search_max_steps": 8},
        }
        bot.last_cancel_at = 0.0
        bot.last_action_at = 0.0
        bot.multi_cancel_scroll_steps = 4
        bot.multi_cancel_recovering = False
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (506, 900), "white")
        browser = FakeBrowser()
        visible_active_cancel = TextBox("取消", 0.99, 420, 720, 480, 750)

        self.assertTrue(asyncio.run(bot.cancel_pending_order(browser, Rect(0, 0, 506, 900), image, [visible_active_cancel])))
        self.assertEqual(browser.wheels, [(0.50, 0.66, 360)])
        self.assertEqual(bot.multi_cancel_scroll_steps, 5)

    def test_terminal_history_below_cancel_stops_multi_order_scrolling(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"texts": {"done": ["已取消", "已完成", "已接单"]}}
        target = TextBox("取消", 0.99, 420, 320, 480, 350)
        history = TextBox("已完成", 0.99, 390, 620, 480, 655)

        self.assertTrue(bot.has_terminal_history_below(target, [target, history]))

    def test_history_view_recovers_upward_before_cancelling_multi_order(self) -> None:
        class FakeBrowser:
            def __init__(self) -> None:
                self.wheels: list[tuple[float, float, float]] = []

            async def wheel_relative(self, _rect, x: float, y: float, delta: float) -> None:
                self.wheels.append((x, y, delta))

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "action_throttle_sec": 0.1,
            "limit_min": "1500",
            "repost_delay_sec": 30,
            "auto_repost": True,
            "texts": {"balance": ["搬砖余额"], "pending": ["变现中"], "cancel": ["取消"], "done": ["已取消", "已完成"]},
            "multi_order": {"enabled": True},
        }
        bot.multi_batch_complete = True
        bot.multi_pending_since = [time.time() - 60]
        bot.multi_cancel_scroll_steps = 1
        bot.multi_cancel_recovering = False
        bot.last_cancel_at = 0.0
        bot.last_action_at = 0.0
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (506, 900), "white")
        boxes = [
            TextBox("搬砖余额", 0.99, 30, 125, 150, 158),
            TextBox("1459.88", 0.99, 30, 168, 170, 205),
            TextBox("已完成", 0.99, 390, 620, 480, 655),
        ]
        browser = FakeBrowser()

        self.assertTrue(asyncio.run(bot.handle_multi_order_workflow(browser, Rect(0, 0, 506, 900), image, boxes)))
        self.assertEqual(browser.wheels, [(0.50, 0.66, -360)])
        self.assertTrue(bot.multi_cancel_recovering)

    def test_waiting_transfer_detail_requires_the_detail_title(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "texts": {
                "wait_transfer": ["待付款"],
                "order_detail": ["出售详情"],
            }
        }
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (508, 900), "white")
        detail_boxes = [
            TextBox("出售详情", 0.99, 190, 20, 320, 55),
            TextBox("待付款", 0.99, 190, 100, 260, 135),
        ]

        self.assertTrue(bot.is_waiting_transfer_detail(image, detail_boxes))

    def test_waiting_transfer_state_is_detected_before_starting_new_order(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"texts": {"wait_transfer": ["待付款", "请耐心等待买家转账"]}}
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (508, 900), "white")
        boxes = [TextBox("请耐心等待买家转账", 0.99, 80, 170, 320, 210)]

        self.assertTrue(bot.is_waiting_transfer(image, boxes))

    def test_trade_notification_interrupts_form_and_clicks_accept(self) -> None:
        class FakeBrowser:
            pass

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "auto_accept": True,
            "texts": {"trade_notification": ["交易通知"], "accept": ["同意交易"]},
        }
        bot.form_expected_after = 1.0
        bot.form_expected_until = 99.0
        bot.form_submission_failed = True
        bot.form_values = {"amount": "3000"}
        bot.last_accept_at = 0.0
        bot.last_action_at = 0.0
        bot.accept_confirm_until = 0.0
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (506, 900), "white")
        notification = TextBox("交易通知", 0.99, 180, 150, 325, 195)
        accept = TextBox("同意交易", 0.99, 330, 780, 480, 840)
        clicked: list[str] = []

        async def save_debug(*_args) -> None:
            return None

        async def click_target(_browser, _rect, target) -> None:
            clicked.append(target.text)

        bot.save_action_debug = save_debug
        bot.click_visual_target = click_target

        self.assertTrue(asyncio.run(bot.handle_incoming_order(FakeBrowser(), Rect(0, 0, 506, 900), image, [notification, accept])))
        self.assertEqual(clicked, ["同意交易"])
        self.assertEqual(bot.form_expected_until, 0)
        self.assertFalse(bot.form_submission_failed)
        self.assertEqual(bot.form_values, {})

    def test_order_exception_reminder_clicks_ignore_not_view_details(self) -> None:
        class FakeBrowser:
            pass

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "action_throttle_sec": 0.1,
            "texts": {
                "order_exception": ["订单异常提醒"],
                "order_exception_ignore": ["忽略"],
            },
        }
        bot.last_action_at = 0.0
        bot.last_exception_ignore_at = 0.0
        clicked: list[str] = []
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        bot.save_action_debug = lambda *_args: asyncio.sleep(0)

        async def click_target(_browser, _rect, target) -> None:
            clicked.append(target.text)

        bot.click_visual_target = click_target
        image = Image.new("RGB", (506, 900), "white")
        boxes = [
            TextBox("订单异常提醒", 0.99, 165, 280, 340, 315),
            TextBox("忽略", 0.99, 120, 650, 220, 690),
            TextBox("立即查看", 0.99, 300, 650, 440, 690),
        ]

        self.assertEqual(bot.classify_visual_page(image, boxes), "order_exception_dialog")
        self.assertTrue(asyncio.run(bot.handle_order_exception_dialog(FakeBrowser(), Rect(0, 0, 506, 900), image, boxes)))
        self.assertEqual(clicked, ["忽略"])

    def test_orders_page_pull_refresh_runs_once_per_minute(self) -> None:
        class FakeBrowser:
            def __init__(self) -> None:
                self.drags: list[tuple[float, float, float, float]] = []

            async def drag_relative(self, _rect, start_x: float, start_y: float, end_x: float, end_y: float) -> None:
                self.drags.append((start_x, start_y, end_x, end_y))

        bot = CanvasBot.__new__(CanvasBot)
        bot.last_orders_refresh_at = 0.0
        bot.last_action_at = 0.0
        bot.save_action_debug = lambda *_args: asyncio.sleep(0)
        image = Image.new("RGB", (506, 900), "white")
        browser = FakeBrowser()

        self.assertTrue(asyncio.run(bot.maybe_pull_refresh_orders(browser, Rect(0, 0, 506, 900), image, [])))
        self.assertEqual(browser.drags, [(0.50, 0.42, 0.50, 0.70)])
        self.assertFalse(asyncio.run(bot.maybe_pull_refresh_orders(browser, Rect(0, 0, 506, 900), image, [])))

    def test_unknown_page_refreshes_after_configured_consecutive_scans(self) -> None:
        class FakeBrowser:
            def __init__(self) -> None:
                self.reloads = 0

            async def reload(self) -> None:
                self.reloads += 1

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"unknown_page_refresh": {"consecutive_scans": 6, "cooldown_sec": 20}}
        bot.unknown_page_streak = 5
        bot.last_unknown_page_reload_at = 0.0
        bot.last_action_at = 0.0
        bot.save_action_debug = lambda *_args: asyncio.sleep(0)
        image = Image.new("RGB", (506, 900), "white")
        browser = FakeBrowser()

        self.assertTrue(asyncio.run(bot.handle_unknown_page(browser, Rect(0, 0, 506, 900), image, [])))
        self.assertEqual(browser.reloads, 1)
        self.assertEqual(bot.unknown_page_streak, 0)

    def test_page_classifier_prefers_withdraw_form_over_visible_navigation(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "texts": {
                "move_page_tab": ["搬砖"],
                "order_page_tab": ["订单"],
                "amount": ["变现金额"],
                "submit": ["确认变现"],
                "limit": ["单笔限额"],
            }
        }
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (506, 900), "white")
        boxes = [
            TextBox("搬砖", 0.99, 120, 14, 180, 55),
            TextBox("订单", 0.99, 230, 14, 290, 55),
            TextBox("变现金额", 0.99, 35, 260, 150, 290),
            TextBox("单笔限额", 0.99, 35, 430, 150, 460),
            TextBox("确认变现", 0.99, 180, 840, 325, 875),
        ]

        self.assertEqual(bot.classify_visual_page(image, boxes), "withdraw_form")

    def test_page_classifier_identifies_empty_orders_before_navigation(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "texts": {
                "move_page_tab": ["搬砖"],
                "order_page_tab": ["订单"],
                "order_empty": ["暂无数据"],
            }
        }
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (506, 900), "white")
        boxes = [
            TextBox("搬砖", 0.99, 120, 14, 180, 55),
            TextBox("订单", 0.99, 230, 14, 290, 55),
            TextBox("进行中", 0.99, 20, 92, 95, 125),
            TextBox("暂无数据", 0.99, 190, 520, 315, 555),
        ]

        self.assertEqual(bot.classify_visual_page(image, boxes), "orders_empty")

    def test_page_classifier_keeps_waiting_row_inside_orders_list(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "texts": {
                "order_page_tab": ["订单"],
                "wait_transfer": ["待付款"],
                "order_detail": ["出售详情"],
            }
        }
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (506, 900), "white")
        boxes = [
            TextBox("订单", 0.99, 230, 14, 290, 55),
            TextBox("进行中", 0.99, 20, 92, 95, 125),
            TextBox("待付款", 0.99, 390, 225, 475, 260),
        ]

        self.assertEqual(bot.classify_visual_page(image, boxes), "orders_list")

    def test_page_classifier_identifies_move_page_from_balance(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"texts": {"balance": ["搬砖余额"], "move_page_tab": ["搬砖"]}}
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (506, 900), "white")
        boxes = [
            TextBox("搬砖", 0.99, 120, 14, 180, 55),
            TextBox("搬砖余额", 0.99, 30, 125, 150, 158),
            TextBox("10,459.88", 0.99, 30, 168, 170, 205),
        ]

        self.assertEqual(bot.classify_visual_page(image, boxes), "move_page")

    def test_buyer_detail_interrupt_clears_failed_form_and_returns(self) -> None:
        class FakeBrowser:
            def __init__(self) -> None:
                self.relative_clicks: list[tuple[float, float]] = []

            async def click_relative(self, _rect, x: float, y: float) -> None:
                self.relative_clicks.append((x, y))

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "texts": {"order_detail": ["出售详情"], "buyer_detail": ["买家详情"]},
            "points": {"order_detail_back": [0.08, 0.04]},
        }
        bot.form_expected_after = 1.0
        bot.form_expected_until = 99.0
        bot.form_submission_failed = True
        bot.payment_selected_for_form = True
        bot.form_values = {"amount": "1459.88"}
        bot.last_form_interruption_back_at = 0.0
        bot.last_action_at = 0.0
        bot.accept_confirm_until = 0.0
        bot.last_accept_at = 0.0
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        bot.save_action_debug = lambda *_args: asyncio.sleep(0)
        image = Image.new("RGB", (506, 900), "white")
        boxes = [TextBox("买家详情", 0.99, 190, 20, 315, 55)]
        browser = FakeBrowser()

        self.assertTrue(asyncio.run(bot.handle_incoming_order(browser, Rect(0, 0, 506, 900), image, boxes)))
        self.assertEqual(browser.relative_clicks, [(0.08, 0.04)])
        self.assertEqual(bot.form_expected_until, 0)
        self.assertFalse(bot.form_submission_failed)
        self.assertEqual(bot.form_values, {})

    def test_post_accept_detail_returns_before_looking_for_order_tab(self) -> None:
        class FakeBrowser:
            def __init__(self) -> None:
                self.relative_clicks: list[tuple[float, float]] = []

            async def click_relative(self, _rect, x: float, y: float) -> None:
                self.relative_clicks.append((x, y))

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "texts": {"order_detail": ["出售详情"], "buyer_detail": ["买家详情"]},
            "points": {"order_detail_back": [0.08, 0.04]},
        }
        bot.post_accept_order_check = "open"
        bot.post_accept_order_check_until = 9999999999.0
        bot.last_post_accept_detail_back_at = 0.0
        bot.last_action_at = 0.0
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        bot.save_action_debug = lambda *_args: asyncio.sleep(0)
        image = Image.new("RGB", (506, 900), "white")
        boxes = [TextBox("出售详情", 0.99, 190, 20, 315, 55)]
        browser = FakeBrowser()

        self.assertTrue(asyncio.run(bot.handle_post_accept_order_check(browser, Rect(0, 0, 506, 900), image, boxes)))
        self.assertEqual(browser.relative_clicks, [(0.08, 0.04)])
        self.assertEqual(bot.post_accept_order_check, "open")

    def test_keyboard_password_entry_presses_each_digit(self) -> None:
        class FakeBrowser:
            def __init__(self) -> None:
                self.clicks: list[tuple[float, float]] = []
                self.keys: list[str] = []

            async def click_abs(self, x: float, y: float) -> None:
                self.clicks.append((x, y))

            async def press(self, key: str) -> None:
                self.keys.append(key)

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"texts": {"password_dialog": ["验证支付密码"]}}
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        browser = FakeBrowser()
        image = Image.new("RGB", (508, 900), "white")
        heading = TextBox("验证支付密码", 1.0, 201, 367, 337, 395)

        asyncio.run(bot.type_password_with_keyboard(browser, Rect(0, 0, 508, 900), "123456", image, [heading]))

        self.assertEqual(len(browser.clicks), 1)
        self.assertAlmostEqual(browser.clicks[0][0], 111.52, places=2)
        self.assertAlmostEqual(browser.clicks[0][1], 450.8, places=2)
        self.assertEqual(browser.keys, ["1", "2", "3", "4", "5", "6"])

    def test_dashboard_password_is_used_when_config_has_no_password(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"password": {"env": "QJD67_PAY_PASSWORD", "value": ""}}
        bot.dashboard_password = lambda: "123456"

        self.assertEqual(asyncio.run(bot.resolve_payment_password()), "123456")

    def test_password_target_is_derived_from_dialog_heading(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"texts": {"password_dialog": ["验证支付密码"]}}
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (508, 900), "white")
        heading = TextBox("验证支付密码", 1.0, 201, 367, 337, 395)

        target = bot.resolve_password_field_target(image, [heading])

        self.assertIsNotNone(target)
        self.assertAlmostEqual(target[0], 111.52, places=2)
        self.assertGreater(target[1], heading.bottom)

    def test_configured_submit_terms_match_determine_withdraw_button(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = load_config(ROOT / "configs" / "qjd67.json")
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (506, 900), "white")
        button = TextBox("确定变现", 0.99, 160, 820, 346, 870)

        self.assertIsNotNone(bot.find_bottom_action_target(image, [button], bot.texts["submit"]))

    def test_field_target_is_derived_from_its_ocr_label(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"texts": {"amount_label": ["变现金额"]}}

        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (506, 900), "white")
        label = TextBox("变现金额", 0.99, 30, 330, 130, 366)

        target = bot.resolve_canvas_field_target(image, [label], ["变现金额"], 0.5)

        self.assertIsNotNone(target)
        self.assertEqual(target[0], 253.0)
        self.assertGreater(target[1], label.bottom)

    def test_field_value_validation_accepts_left_aligned_input_text(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        image = Image.new("RGB", (506, 900), "white")
        target = (253.0, 337.0)
        value = TextBox("3000", 1.0, 44, 320, 91, 354)

        self.assertTrue(bot.is_field_value_visible([value], "3000", target, image))

    def test_all_amount_can_be_verified_after_page_rounds_down_cents(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        image = Image.new("RGB", (506, 900), "white")
        target = (253.0, 337.0)
        rounded_value = TextBox("1459", 1.0, 44, 320, 91, 354)

        rounded = bot.decimal_text(Decimal("1459.88").to_integral_value(rounding="ROUND_DOWN"))
        self.assertEqual(rounded, "1459")
        self.assertTrue(bot.is_field_value_visible([rounded_value], rounded, target, image))

    def test_move_page_tab_is_limited_to_the_top_navigation(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"texts": {"move_page_tab": ["搬砖"]}}

        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (506, 900), "white")
        top_tab = TextBox("搬砖", 0.99, 150, 18, 205, 58)
        lower_text = TextBox("开始搬砖", 0.99, 350, 420, 470, 460)

        self.assertIs(bot.find_move_page_tab(image, [top_tab, lower_text]), top_tab)

    def test_manually_cancelled_orders_clear_the_stale_pending_timer(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "texts": {
                "pending": ["待接单", "接单中"],
                "cancel": ["取消"],
                "done": ["已取消", "已完成"],
            }
        }
        bot.pending_since = 123.0

        # Use the real text matcher while stubbing only template lookup.
        from canvas_bot.__main__ import VisionEngine

        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (506, 900), "white")
        boxes = [TextBox("已取消", 0.99, 410, 430, 480, 460)]

        anchor = bot.find_pending_anchor(image, boxes)
        self.assertIsNone(anchor)
        self.assertTrue(bot.clear_stale_pending_if_missing(anchor, boxes))
        self.assertEqual(bot.pending_since, 0.0)


if __name__ == "__main__":
    unittest.main()
