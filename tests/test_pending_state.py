import asyncio
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from canvas_bot.__main__ import BrowserSession, CanvasBot, ROOT, Rect, TextBox, VisionEngine, load_config
from canvas_bot.dialogs.handlers import classify_modal_page


class PendingStateTests(unittest.TestCase):
    def test_pushplus_waiting_transfer_message_uses_name_template_and_time(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"notification": {"transfer_name": "张三"}}

        with patch("canvas_bot.__main__.random.choice", return_value=("转账提醒", "订单已进入待付款状态，请留意买家转账。")), patch(
            "canvas_bot.__main__.time.strftime", return_value="14:32:08"
        ):
            title, content = bot.pushplus_waiting_transfer_message()

        self.assertEqual(title, "张三 转账提醒")
        self.assertEqual(content, "张三：订单已进入待付款状态，请留意买家转账。\n通知时间：14:32:08")

    def test_payment_keywords_hot_reload_from_dashboard_config(self) -> None:
        from canvas_bot.dashboard import write_config

        with self.subTest("dashboard updates are read without rebuilding the bot"):
            with tempfile.TemporaryDirectory() as directory:
                config_path = Path(directory) / "settings.json"
                cfg = {
                    "payment": {
                        "keyword_options": [
                            {"keyword": "旧关键词", "enabled": True},
                            {"keyword": "停用关键词", "enabled": False},
                        ]
                    },
                    "multi_order": {"payment_keywords": ["旧关键词"]},
                }
                write_config(config_path, cfg)
                bot = CanvasBot.__new__(CanvasBot)
                bot.cfg = load_config(config_path)
                bot.multi_payment_index = 0

                self.assertEqual(bot.current_payment_keyword(), "旧关键词")

                cfg["payment"]["keyword_options"] = [{"keyword": "新关键词", "enabled": True}]
                write_config(config_path, cfg)
                self.assertEqual(bot.current_payment_keyword(), "新关键词")

    def test_waiting_transfer_varies_only_the_pushplus_message(self) -> None:
        class FakeNotifier:
            def __init__(self, name: str) -> None:
                self.name = name
                self.enabled = True
                self.sent: list[tuple[str, str]] = []

            def send(self, title: str, content: str) -> None:
                self.sent.append((title, content))

        bot = CanvasBot.__new__(CanvasBot)
        pushplus = FakeNotifier("PushPlus")
        telegram = FakeNotifier("Telegram")
        bot.cfg = {"notification": {"transfer_name": "张三"}}
        bot.notified_events = set()
        bot.notifiers = [pushplus, telegram]

        with patch("canvas_bot.__main__.random.choice", return_value=("待付款提醒", "有一笔订单正在等待付款，请留意转账动态。")), patch(
            "canvas_bot.__main__.time.strftime", return_value="14:32:08"
        ):
            asyncio.run(bot.notify_once("waiting_transfer", "待付款", "订单已进入待付款状态，请留意买家转账。"))

        self.assertEqual(pushplus.sent, [("张三 待付款提醒", "张三：有一笔订单正在等待付款，请留意转账动态。\n通知时间：14:32:08")])
        self.assertEqual(telegram.sent, [("待付款", "订单已进入待付款状态，请留意买家转账。")])

    def test_replace_numeric_uses_playwright_insert_text(self) -> None:
        class FakeKeyboard:
            def __init__(self) -> None:
                self.presses: list[str] = []
                self.inserted: list[str] = []

            async def press(self, key: str) -> None:
                self.presses.append(key)

            async def insert_text(self, value: str) -> None:
                self.inserted.append(value)

        class FakePage:
            def __init__(self) -> None:
                self.keyboard = FakeKeyboard()

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        browser = BrowserSession.__new__(BrowserSession)
        browser.page = FakePage()
        browser.focus_flutter_text_input = lambda: asyncio.sleep(0, result=True)
        browser.active_text_input_value = lambda: asyncio.sleep(0, result="1500")

        self.assertTrue(asyncio.run(browser.replace_numeric("1500")))

        self.assertEqual(browser.page.keyboard.inserted, ["1500"])

    def test_replace_numeric_enters_password_one_digit_at_a_time_and_verifies_once(self) -> None:
        class FakeKeyboard:
            def __init__(self) -> None:
                self.inserted: list[str] = []

            async def press(self, _key: str) -> None:
                return None

            async def insert_text(self, value: str) -> None:
                self.inserted.append(value)

        class FakePage:
            def __init__(self) -> None:
                self.keyboard = FakeKeyboard()

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        browser = BrowserSession.__new__(BrowserSession)
        browser.page = FakePage()
        browser.focus_flutter_text_input = lambda: asyncio.sleep(0, result=True)
        # Flutter clears the bridge input after dispatching each digit; the
        # canvas route, not this DOM value, confirms password acceptance.
        browser.active_text_input_value = lambda: asyncio.sleep(0, result="")

        self.assertTrue(asyncio.run(browser.replace_numeric("990314", verify_and_repair=False)))
        self.assertEqual(browser.page.keyboard.inserted, list("990314"))

    def test_vision_reuses_only_identical_canvas_frames(self) -> None:
        calls: list[object] = []

        def fake_ocr(_image):
            calls.append(object())
            return [([[1, 2], [8, 2], [8, 10], [1, 10]], "测试", 0.99)], None

        vision = VisionEngine.__new__(VisionEngine)
        vision.ocr = fake_ocr
        vision.reuse_identical_frames = True
        vision._last_frame_key = None
        vision._last_frame_boxes = []
        vision.last_read_used_cache = False
        image = Image.new("RGB", (40, 40), "white")

        self.assertEqual(len(vision.read_text(image)), 1)
        self.assertEqual(len(vision.read_text(image.copy())), 1)
        self.assertEqual(len(calls), 1)
        self.assertTrue(vision.last_read_used_cache)
        changed = image.copy()
        changed.putpixel((0, 0), (0, 0, 0))
        self.assertEqual(len(vision.read_text(changed)), 1)
        self.assertEqual(len(calls), 2)

    def test_vision_initializes_all_ocr_models_with_cuda(self) -> None:
        class FakeSession:
            def get_providers(self) -> list[str]:
                return ["CUDAExecutionProvider", "CPUExecutionProvider"]

        class FakeInfer:
            def __init__(self) -> None:
                self.session = FakeSession()

        class FakeRapidOCR:
            kwargs: dict[str, object] = {}

            def __init__(self, **kwargs: object) -> None:
                self.__class__.kwargs = kwargs
                self.text_det = SimpleNamespace(infer=FakeInfer())
                self.text_cls = SimpleNamespace(infer=FakeInfer())
                self.text_rec = SimpleNamespace(session=FakeInfer())

        fake_module = SimpleNamespace(RapidOCR=FakeRapidOCR)
        with patch.dict(sys.modules, {"rapidocr_onnxruntime": fake_module}), patch(
            "canvas_bot.__main__.prepare_cuda_runtime"
        ) as prepare_cuda_runtime:
            vision = VisionEngine({"vision": {"ocr_device": "cuda"}})

        prepare_cuda_runtime.assert_called_once_with()
        self.assertEqual(vision.ocr_device, "cuda")
        self.assertEqual(vision.ocr_name, "rapidocr-onnxruntime/CUDAExecutionProvider")
        self.assertEqual(
            FakeRapidOCR.kwargs,
            {
                "det_use_cuda": True,
                "cls_use_cuda": True,
                "rec_use_cuda": True,
                "det_use_dml": False,
                "cls_use_dml": False,
                "rec_use_dml": False,
            },
        )

    def test_vision_region_ocr_restores_canvas_coordinates(self) -> None:
        vision = VisionEngine.__new__(VisionEngine)
        vision.read_text = lambda _image: [TextBox("卡片", 0.99, 10, 20, 50, 40)]
        image = Image.new("RGB", (500, 900), "white")

        boxes = vision.read_text_region(image, (0.0, 0.40, 1.0, 0.95))

        self.assertEqual(len(boxes), 1)
        self.assertEqual((boxes[0].left, boxes[0].top, boxes[0].right, boxes[0].bottom), (10, 380, 50, 400))

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

    def test_multi_order_uses_all_when_configured_order_would_leave_subminimum_remainder(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"amount": "3000", "limit_min": "1500", "limit_max": "3000"}
        bot.multi_batch_complete = False
        bot.form_values = {}

        self.assertTrue(bot.prepare_multi_order_values(Decimal("4164.6")))
        self.assertEqual(
            bot.form_values,
            {"amount": "4164.6", "limit_min": "1500", "limit_max": "4164", "use_all_balance": "true"},
        )

    def test_form_balance_replans_stale_multi_order_to_use_all(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"amount": "3000", "limit_min": "1000", "limit_max": "3000"}
        bot.multi_batch_complete = False
        bot.form_values = {
            "amount": "3000",
            "limit_min": "1000",
            "limit_max": "3000",
            "use_all_balance": "false",
        }

        self.assertTrue(bot.replan_form_values_for_current_balance(Decimal("1816.75"), "3000"))
        self.assertEqual(
            bot.form_values,
            {"amount": "1816.75", "limit_min": "1000", "limit_max": "1816", "use_all_balance": "true"},
        )

    def test_form_balance_replans_existing_all_order_when_balance_increases(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"amount": "3000", "limit_min": "1500", "limit_max": "3000"}
        bot.multi_batch_complete = False
        bot.form_values = {
            "amount": "3000.81",
            "limit_min": "1500",
            "limit_max": "3000",
            "use_all_balance": "true",
        }

        self.assertTrue(bot.replan_form_values_for_current_balance(Decimal("4010.81"), "3000.81"))
        self.assertEqual(
            bot.form_values,
            {"amount": "4010.81", "limit_min": "1500", "limit_max": "4010", "use_all_balance": "true"},
        )

    def test_form_balance_below_minimum_refuses_stale_multi_order(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"amount": "3000", "limit_min": "1500", "limit_max": "3000"}
        bot.multi_batch_complete = False
        bot.form_values = {"amount": "3000", "use_all_balance": "false"}

        self.assertFalse(bot.replan_form_values_for_current_balance(Decimal("1164.6"), "3000"))
        self.assertTrue(bot.multi_batch_complete)

    def test_withdraw_unavailable_page_uses_dynamic_left_back_position(self) -> None:
        class FakeBrowser:
            def __init__(self) -> None:
                self.relative_clicks: list[tuple[float, float]] = []

            async def click_relative(self, _rect, x: float, y: float) -> None:
                self.relative_clicks.append((x, y))

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"action_throttle_sec": 0.1, "points": {"order_detail_back": [0.08, 0.04]}}
        bot.last_action_at = 0.0
        bot.last_withdraw_unavailable_back_at = 0.0
        image = Image.new("RGB", (608, 730), "white")
        boxes = [
            TextBox("搬砖余额", 0.99, 20, 92, 120, 122),
            TextBox("0.81", 0.99, 132, 92, 180, 122),
            TextBox("您的可变现余额不足10", 0.99, 180, 520, 440, 550),
        ]
        browser = FakeBrowser()

        self.assertTrue(
            asyncio.run(bot.handle_withdraw_unavailable_page(browser, Rect(0, 0, 608, 730), image, boxes))
        )
        self.assertEqual(len(browser.relative_clicks), 1)
        x, y = browser.relative_clicks[0]
        self.assertAlmostEqual(x, 26.08 / 608, places=3)
        self.assertEqual(y, 0.04)

    def test_matching_configured_amount_and_maximum_can_keep_form_default(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"amount": "3000", "limit_max": "3000.00"}
        self.assertTrue(bot.configured_amount_matches_limit_max())

        bot.cfg["limit_max"] = "2800"
        self.assertFalse(bot.configured_amount_matches_limit_max())

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
        class FakeBrowser:
            def __init__(self) -> None:
                self.wheels: list[tuple[float, float, float]] = []

            async def wheel_relative(self, _rect, x: float, y: float, delta: float) -> None:
                self.wheels.append((x, y, delta))

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

        browser = FakeBrowser()
        self.assertTrue(asyncio.run(bot.handle_multi_order_workflow(browser, Rect(0, 0, 506, 900), image, boxes)))
        self.assertEqual(bot.multi_pending_since, [123.0])
        self.assertEqual(started, [])
        self.assertEqual(browser.wheels, [(0.50, 0.66, -720)])

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
        self.assertEqual(browser.wheels, [(0.50, 0.66, 120)])
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
        self.assertEqual(browser.wheels, [(0.50, 0.66, 120)])
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
        self.assertEqual(browser.wheels, [(0.50, 0.66, -120)])
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

        async def unexpected_notification(*_args) -> None:
            raise AssertionError("交易通知不应触发 PushPlus")

        bot.notify_once = unexpected_notification

        self.assertTrue(asyncio.run(bot.handle_incoming_order(FakeBrowser(), Rect(0, 0, 506, 900), image, [notification, accept])))
        self.assertEqual(clicked, ["同意交易"])
        self.assertEqual(bot.form_expected_until, 0)
        self.assertFalse(bot.form_submission_failed)
        self.assertEqual(bot.form_values, {})

    def test_order_exception_reminder_uses_global_ignore_route_not_view_details(self) -> None:
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

        async def unexpected_notification(*_args) -> None:
            raise AssertionError("订单异常不应触发 PushPlus")

        bot.notify_once = unexpected_notification

        async def click_target(_browser, _rect, target) -> None:
            clicked.append(target.text)

        bot.click_visual_target = click_target
        image = Image.new("RGB", (506, 900), "white")
        boxes = [
            TextBox("订单异常提醒", 0.99, 165, 280, 340, 315),
            TextBox("忽略", 0.99, 120, 650, 220, 690),
            TextBox("立即查看", 0.99, 300, 650, 440, 690),
        ]

        self.assertEqual(bot.classify_visual_page(image, boxes), "system_ignore_dialog")
        self.assertTrue(asyncio.run(bot.handle_system_ignore_dialog(FakeBrowser(), Rect(0, 0, 506, 900), image, boxes)))
        self.assertEqual(clicked, ["忽略"])

    def test_system_ignore_dialog_overrides_the_underlying_move_page(self) -> None:
        class FakeBrowser:
            pass

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "action_throttle_sec": 0.1,
            "texts": {
                "system_dialog": ["系统提示"],
                "system_dialog_ignore": ["忽略"],
            },
        }
        bot.last_action_at = 0.0
        bot.last_system_ignore_at = 0.0
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda _image, _name: None
        bot.vision = vision
        bot.save_action_debug = lambda *_args: asyncio.sleep(0)
        clicked: list[str] = []

        async def click_target(_browser, _rect, target) -> None:
            clicked.append(target.text)

        bot.click_visual_target = click_target
        image = Image.new("RGB", (506, 900), "white")
        boxes = [
            TextBox("系统提示", 0.99, 185, 260, 320, 300),
            TextBox("请留意系统公告", 0.99, 130, 345, 380, 380),
            TextBox("忽略", 0.99, 125, 650, 220, 690),
            TextBox("搬砖余额", 0.99, 35, 95, 180, 125),
            TextBox("开始搬砖", 0.99, 365, 145, 485, 185),
        ]

        self.assertEqual(bot.classify_visual_page(image, boxes), "system_ignore_dialog")
        self.assertTrue(asyncio.run(bot.handle_system_ignore_dialog(FakeBrowser(), Rect(0, 0, 506, 900), image, boxes)))
        self.assertEqual(clicked, ["忽略"])

    def test_ignore_text_without_a_system_modal_title_is_not_routed_as_a_dialog(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "texts": {
                "system_dialog": ["系统提示"],
                "system_dialog_ignore": ["忽略"],
                "start": ["开始搬砖"],
            },
        }
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda _image, _name: None
        bot.vision = vision
        image = Image.new("RGB", (506, 900), "white")
        boxes = [
            TextBox("开始搬砖", 0.99, 365, 145, 485, 185),
            TextBox("忽略", 0.99, 125, 650, 220, 690),
        ]

        self.assertEqual(bot.classify_visual_page(image, boxes), "move_page")

    def test_trade_success_dialog_closes_only_its_acknowledgement(self) -> None:
        class FakeBrowser:
            pass

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "action_throttle_sec": 0.1,
            "texts": {
                "trade_success": ["交易成功"],
                "trade_success_ack": ["知道了"],
            },
        }
        bot.last_action_at = 0.0
        bot.last_trade_success_ack_at = 0.0
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        bot.save_action_debug = lambda *_args: asyncio.sleep(0)
        clicked: list[str] = []

        async def click_target(_browser, _rect, target) -> None:
            clicked.append(target.text)

        bot.click_visual_target = click_target
        image = Image.new("RGB", (506, 900), "white")
        boxes = [
            TextBox("交易成功", 0.99, 190, 335, 320, 375),
            TextBox("知道了", 0.99, 185, 670, 320, 720),
            TextBox("确认放行", 0.99, 360, 500, 460, 530),
        ]

        self.assertEqual(bot.classify_visual_page(image, boxes), "trade_success_dialog")
        self.assertTrue(asyncio.run(bot.handle_trade_success_dialog(FakeBrowser(), Rect(0, 0, 506, 900), image, boxes)))
        self.assertEqual(clicked, ["知道了"])
        self.assertEqual(bot.post_accept_order_check, "open")
        self.assertGreater(bot.post_accept_order_check_until, time.time())

    def test_system_notice_dialog_closes_only_recognized_upper_right_icon(self) -> None:
        class FakeBrowser:
            def __init__(self) -> None:
                self.clicks: list[tuple[float, float]] = []

            async def click_abs(self, x: float, y: float) -> None:
                self.clicks.append((x, y))

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"action_throttle_sec": 0.1, "texts": {"system_notice": ["安全通知"]}}
        bot.last_action_at = 0.0
        bot.last_system_notice_close_at = 0.0
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        bot.save_action_debug = lambda *_args: asyncio.sleep(0)
        image = Image.new("RGB", (595, 726), "white")
        boxes = [
            TextBox("安全通知", 0.99, 250, 235, 346, 260),
            TextBox("系统检测您目前交易存在安全隐患", 0.99, 150, 310, 450, 342),
            TextBox("确认转账", 0.99, 210, 420, 330, 450),
        ]
        browser = FakeBrowser()

        self.assertEqual(bot.classify_visual_page(image, boxes), "system_notice_dialog")
        self.assertTrue(
            asyncio.run(bot.handle_system_notice_dialog(browser, Rect(100, 200, 595, 726), image, boxes))
        )
        self.assertEqual(len(browser.clicks), 1)
        x, y = browser.clicks[0]
        self.assertAlmostEqual(x, 538.0, delta=2.0)
        self.assertAlmostEqual(y, 427.0, delta=2.0)

    def test_post_accept_check_timeout_releases_orders_page_flow(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.post_accept_order_check = "inspect"
        bot.post_accept_order_check_until = 0.0

        self.assertFalse(
            asyncio.run(
                bot.handle_post_accept_order_check(
                    object(),
                    Rect(0, 0, 506, 900),
                    Image.new("RGB", (506, 900), "white"),
                    [],
                )
            )
        )
        self.assertEqual(bot.post_accept_order_check, "")

    def test_submit_confirm_dialog_clicks_only_after_this_run_submitted_the_form(self) -> None:
        class FakeBrowser:
            pass

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"action_throttle_sec": 0.1, "texts": {"submit_confirm_title": ["确认变现"]}}
        bot.last_action_at = 0.0
        bot.submit_confirm_until = time.time() + 10.0
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        bot.save_action_debug = lambda *_args: asyncio.sleep(0)
        clicked: list[str] = []

        async def click_target(_browser, _rect, target) -> None:
            clicked.append(target.text)

        bot.click_visual_target = click_target
        image = Image.new("RGB", (568, 730), "white")
        boxes = [
            TextBox("确认变现", 0.99, 245, 456, 326, 483),
            TextBox("确认", 0.99, 263, 677, 307, 703),
        ]

        self.assertEqual(bot.classify_visual_page(image, boxes), "submit_confirm_dialog")
        self.assertTrue(asyncio.run(bot.handle_submit_confirm_dialog(FakeBrowser(), Rect(0, 0, 568, 730), image, boxes)))
        self.assertEqual(clicked, ["确认"])

    def test_password_dialog_overrides_underlying_submit_button(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "texts": {
                "password_dialog": ["验证支付密码"],
                "submit_confirm_title": ["确认变现"],
            }
        }
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (568, 730), "white")
        boxes = [
            TextBox("验证支付密码", 0.99, 220, 360, 350, 390),
            TextBox("确认变现", 0.99, 245, 456, 326, 483),
            TextBox("确认", 0.99, 263, 677, 307, 703),
        ]

        self.assertEqual(classify_modal_page(bot, SimpleNamespace(image=image, boxes=boxes)), "password_dialog")

    def test_runtime_password_dialog_does_not_repeat_an_unacknowledged_input(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.password_dialog_input_pending = True
        messages: list[str] = []
        bot.record_state = lambda _key, message, interval_sec=0: messages.append(message)

        self.assertTrue(
            asyncio.run(
                bot.handle_runtime_password_dialog(
                    None,
                    Rect(0, 0, 506, 900),
                    Image.new("RGB", (506, 900), "white"),
                    [],
                )
            )
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("暂停重复输入", messages[0])

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

    def test_unknown_page_saves_one_diagnostic_before_reload(self) -> None:
        class FakeBrowser:
            async def reload(self) -> None:
                return None

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "unknown_page_refresh": {"consecutive_scans": 6, "cooldown_sec": 20},
            "diagnostics": {"capture_unknown_after_scans": 3},
        }
        bot.unknown_page_streak = 2
        bot.last_unknown_page_reload_at = 0.0
        saved: list[str] = []

        async def save_debug(_image, _boxes, note, *, force=False) -> None:
            if force:
                saved.append(note)

        bot.maybe_save_debug = save_debug
        image = Image.new("RGB", (506, 900), "white")

        self.assertTrue(asyncio.run(bot.handle_unknown_page(FakeBrowser(), Rect(0, 0, 506, 900), image, [])))
        self.assertEqual(saved, ["unknown_page"])

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

    def test_page_classifier_identifies_withdraw_page_below_platform_minimum(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {
            "texts": {
                "balance": ["搬砖余额"],
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
            TextBox("变现", 0.99, 220, 10, 285, 55),
            TextBox("搬砖余额", 0.99, 30, 125, 150, 158),
            TextBox("0.4", 0.99, 170, 125, 215, 158),
            TextBox("您的可变现余额不足10", 0.99, 160, 660, 390, 700),
        ]

        self.assertEqual(bot.classify_visual_page(image, boxes), "withdraw_unavailable")

    def test_page_classifier_identifies_move_order_detail_page(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"texts": {"move_page_tab": ["搬砖"], "order_detail": ["出售详情"]}}
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (506, 900), "white")
        boxes = [TextBox("搬砖详情", 0.99, 190, 20, 315, 55)]

        self.assertEqual(bot.classify_visual_page(image, boxes), "order_detail")

    def test_move_order_detail_returns_through_top_left_back(self) -> None:
        class FakeBrowser:
            def __init__(self) -> None:
                self.relative_clicks: list[tuple[float, float]] = []

            async def click_relative(self, _rect, x: float, y: float) -> None:
                self.relative_clicks.append((x, y))

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"action_throttle_sec": 0.7, "points": {"order_detail_back": [0.08, 0.04]}}
        bot.last_action_at = 0.0
        bot.last_order_detail_back_at = 0.0
        bot.save_action_debug = lambda *_args: asyncio.sleep(0)
        browser = FakeBrowser()

        self.assertTrue(
            asyncio.run(
                bot.handle_order_detail_page(
                    browser,
                    Rect(0, 0, 506, 900),
                    Image.new("RGB", (506, 900), "white"),
                    [TextBox("搬砖详情", 0.99, 190, 20, 315, 55)],
                )
            )
        )
        self.assertEqual(browser.relative_clicks, [(0.08, 0.04)])

    def test_order_detail_back_uses_centered_canvas_left_edge(self) -> None:
        class FakeBrowser:
            def __init__(self) -> None:
                self.relative_clicks: list[tuple[float, float]] = []

            async def click_relative(self, _rect, x: float, y: float) -> None:
                self.relative_clicks.append((x, y))

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"action_throttle_sec": 0.1, "points": {"order_detail_back": [0.08, 0.04]}}
        bot.last_action_at = 0.0
        bot.last_order_detail_back_at = 0.0
        bot.save_action_debug = lambda *_args: asyncio.sleep(0)
        image = Image.new("RGB", (574, 722), "white")
        boxes = [TextBox("挂单单号", 0.99, 110, 120, 190, 145)]
        browser = FakeBrowser()

        self.assertTrue(asyncio.run(bot.handle_order_detail_page(browser, Rect(0, 0, 574, 722), image, boxes)))
        self.assertAlmostEqual(browser.relative_clicks[0][0], (110 + 5.74) / 574, places=4)
        self.assertEqual(browser.relative_clicks[0][1], 0.04)

    def test_failed_withdraw_form_returns_through_top_left_back(self) -> None:
        class FakeBrowser:
            def __init__(self) -> None:
                self.relative_clicks: list[tuple[float, float]] = []

            async def click_relative(self, _rect, x: float, y: float) -> None:
                self.relative_clicks.append((x, y))

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"action_throttle_sec": 0.7, "points": {"order_detail_back": [0.08, 0.04]}}
        bot.last_action_at = 0.0
        bot.last_failed_form_back_at = 0.0
        bot.form_submission_failed = True
        bot.next_post_after = 0.0
        browser = FakeBrowser()

        self.assertTrue(asyncio.run(bot.handle_withdraw_form_page(browser, Rect(0, 0, 506, 900))))
        self.assertEqual(browser.relative_clicks, [(0.08, 0.04)])
        self.assertGreater(bot.next_post_after, 0.0)

        self.assertTrue(asyncio.run(bot.handle_withdraw_form_page(browser, Rect(0, 0, 506, 900))))
        self.assertEqual(browser.relative_clicks, [(0.08, 0.04)])

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

    def test_keyboard_password_entry_verifies_flutter_hidden_input(self) -> None:
        class FakeBrowser:
            def __init__(self) -> None:
                self.clicks: list[tuple[float, float]] = []
                self.replacements: list[str] = []

            async def click_abs(self, x: float, y: float) -> None:
                self.clicks.append((x, y))

            async def replace_numeric(self, value: str, *, verify_and_repair: bool = True) -> bool:
                self.replacements.append(value)
                return True

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"texts": {"password_dialog": ["验证支付密码"]}}
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        browser = FakeBrowser()
        image = Image.new("RGB", (508, 900), "white")
        heading = TextBox("验证支付密码", 1.0, 201, 367, 337, 395)

        self.assertTrue(asyncio.run(bot.type_password_with_keyboard(browser, Rect(0, 0, 508, 900), "123456", image, [heading])))

        self.assertEqual(len(browser.clicks), 1)
        self.assertAlmostEqual(browser.clicks[0][0], 162.32, places=2)
        self.assertAlmostEqual(browser.clicks[0][1], 450.8, places=2)
        self.assertEqual(browser.replacements, ["123456"])

    def test_keyboard_password_entry_never_retries_a_full_password(self) -> None:
        class FakeBrowser:
            def __init__(self) -> None:
                self.clicks: list[tuple[float, float]] = []
                self.replacements: list[str] = []

            async def click_abs(self, x: float, y: float) -> None:
                self.clicks.append((x, y))

            async def replace_numeric(self, value: str, *, verify_and_repair: bool = True) -> bool:
                self.replacements.append(value)
                return False

        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"texts": {"password_dialog": ["验证支付密码"]}}
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        browser = FakeBrowser()
        image = Image.new("RGB", (508, 900), "white")
        heading = TextBox("验证支付密码", 1.0, 201, 367, 337, 395)

        self.assertFalse(asyncio.run(bot.type_password_with_keyboard(browser, Rect(0, 0, 508, 900), "123456", image, [heading])))
        self.assertEqual(len(browser.clicks), 1)
        self.assertEqual(browser.replacements, ["123456"])

    def test_password_dialog_layout_fallback_works_without_heading_ocr(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"texts": {"password_dialog": ["验证支付密码"]}}
        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        slots = [(100 + index * 68, 420, 52, 58) for index in range(6)]
        vision.find_text = lambda *_args, **_kwargs: None
        vision.find_password_slots = lambda _image: slots
        bot.vision = vision
        image = Image.new("RGB", (508, 900), "white")

        dialog = bot.find_password_dialog(image, [])
        self.assertIsNotNone(dialog)
        self.assertEqual(dialog.text, "支付密码输入框")
        self.assertEqual(bot.resolve_password_field_target(image, []), (126, 449))

    def test_password_dialog_failed_attempt_is_not_repeated(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.password_dialog_input_pending = False
        bot.password_dialog_input_attempted = True
        messages: list[str] = []
        bot.record_state = lambda _key, message, interval_sec=0: messages.append(message)

        self.assertTrue(
            asyncio.run(
                bot.handle_runtime_password_dialog(
                    None,
                    Rect(0, 0, 506, 900),
                    Image.new("RGB", (506, 900), "white"),
                    [],
                )
            )
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("停止重复输入", messages[0])

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
        self.assertAlmostEqual(target[0], 162.32, places=2)
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

    def test_field_target_prefers_existing_numeric_value_on_the_input_row(self) -> None:
        bot = CanvasBot.__new__(CanvasBot)
        bot.cfg = {"texts": {"limit_label": ["单笔限额"]}}

        vision = VisionEngine.__new__(VisionEngine)
        vision.find_template = lambda image, name: None
        bot.vision = vision
        image = Image.new("RGB", (550, 1018), "white")
        label = TextBox("单笔限额", 0.99, 37, 415, 125, 441)
        default_minimum = TextBox("10", 0.99, 48, 476, 78, 500)
        default_maximum = TextBox("3000", 0.99, 313, 477, 365, 499)

        target = bot.resolve_canvas_field_target(
            image,
            [label, default_minimum, default_maximum],
            ["单笔限额"],
            0.26,
        )

        self.assertEqual(target, default_minimum.center)

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
