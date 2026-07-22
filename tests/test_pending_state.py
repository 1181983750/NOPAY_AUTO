import asyncio
import unittest

from PIL import Image

from canvas_bot.__main__ import CanvasBot, ROOT, Rect, TextBox, VisionEngine, load_config


class PendingStateTests(unittest.TestCase):
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
