import json
import os
import tempfile
import unittest
from pathlib import Path

from canvas_bot.dashboard import DashboardState, public_config, validate_dashboard_settings, write_config


class DashboardConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = {
            "amount": "3000",
            "limit_min": "1000",
            "limit_max": "3000",
            "poll_interval_sec": 0.8,
            "action_throttle_sec": 1.2,
            "repost_delay_sec": 30,
            "auto_make_order": True,
            "auto_repost": True,
            "auto_accept": True,
            "payment": {"keyword": "魔仙", "ocr_attempts": 8},
            "cancel_flow": {"confirm_delay_sec": 1.2, "retry_interval_sec": 2, "confirm_timeout_sec": 8},
            "submit_flow": {"secondary_confirm_delay_sec": 1, "secondary_confirm_timeout_sec": 7, "password_timeout_sec": 8},
            "password": {"env": "QJD67_PAY_PASSWORD", "value": ""},
            "debug": {"save_each_tick": False},
        }

    def test_rejects_inverted_limits(self) -> None:
        values = public_config(self.cfg)
        values["limit_min"] = "3001"
        with self.assertRaisesRegex(ValueError, "最小限额"):
            validate_dashboard_settings(self.cfg, values)

    def test_rejects_empty_payment_keyword_list(self) -> None:
        values = public_config(self.cfg)
        values["payment_keywords"] = ""
        with self.assertRaisesRegex(ValueError, "关键词列表"):
            validate_dashboard_settings(self.cfg, values)

    def test_updates_only_supported_settings(self) -> None:
        values = public_config(self.cfg)
        values.update({"amount": "4000", "auto_accept": False, "payment_keyword": "收款人", "payment_keywords": "收款人\n备用卡", "password": "123456"})
        updated = validate_dashboard_settings(self.cfg, values)
        self.assertEqual(updated["amount"], "4000")
        self.assertFalse(updated["auto_accept"])
        self.assertEqual(updated["payment"]["keyword"], "收款人")
        self.assertEqual(updated["multi_order"]["payment_keywords"], ["收款人", "备用卡"])
        self.assertFalse(updated["multi_order"]["adopt_existing_pending_orders"])
        self.assertNotIn("minimum_remaining_balance", updated["multi_order"])
        self.assertEqual(updated["password"]["value"], "")

    def test_public_config_never_returns_password_value(self) -> None:
        cfg = json.loads(json.dumps(self.cfg))
        cfg["password"]["value"] = "secret"
        self.assertNotIn("password_value", public_config(cfg))
        self.assertNotIn("secret", json.dumps(public_config(cfg)))

    def test_write_config_replaces_complete_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            write_config(path, self.cfg)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["amount"], "3000")

    def test_dashboard_password_is_validated_in_memory(self) -> None:
        state = DashboardState(Path("settings.json"), Path.cwd())
        self.assertIn("内存", state.set_payment_password("123456"))
        self.assertEqual(state.payment_password, "123456")
        with self.assertRaisesRegex(ValueError, "6 位数字"):
            state.set_payment_password("12345")

    def test_clears_only_previous_run_screenshots_and_keeps_log_handle_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "settings.json"
            cfg = json.loads(json.dumps(self.cfg))
            cfg["debug"]["dir"] = "debug-screenshots"
            write_config(config_path, cfg)
            debug_dir = root / "debug-screenshots"
            debug_dir.mkdir()
            (debug_dir / "old_tick.png").write_bytes(b"image")
            (debug_dir / "dashboard-bot.log").write_text("old log", encoding="utf-8")
            (debug_dir / "keep.txt").write_text("keep", encoding="utf-8")
            state = DashboardState(config_path, root)

            state.clear_previous_run_artifacts()

            self.assertFalse((debug_dir / "old_tick.png").exists())
            self.assertTrue((debug_dir / "dashboard-bot.log").exists())
            self.assertTrue((debug_dir / "keep.txt").exists())

    def test_bot_environment_contains_token_but_not_the_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "settings.json"
            write_config(config_path, self.cfg)
            state = DashboardState(config_path, Path.cwd(), server_url="http://127.0.0.1:8765", dashboard_token="token")
            state.set_payment_password("123456")
            environment = state.process_environment()
            self.assertEqual(environment["QJD67_DASHBOARD_URL"], "http://127.0.0.1:8765")
            self.assertEqual(environment["QJD67_DASHBOARD_TOKEN"], "token")
            self.assertNotIn("QJD67_PAY_PASSWORD", environment)


if __name__ == "__main__":
    unittest.main()
