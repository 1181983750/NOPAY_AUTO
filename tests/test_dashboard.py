import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from canvas_bot.dashboard import DashboardState, public_config, validate_dashboard_settings, write_config
from canvas_bot.services.telegram import TelegramNotifier


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
        values.update({"amount": "4000", "auto_accept": False, "payment_keyword": "收款人", "payment_keywords": "收款人\n备用卡", "pushplus_transfer_name": "张三", "password": "123456"})
        updated = validate_dashboard_settings(self.cfg, values)
        self.assertEqual(updated["amount"], "4000")
        self.assertFalse(updated["auto_accept"])
        self.assertEqual(updated["payment"]["keyword"], "收款人")
        self.assertEqual(updated["multi_order"]["payment_keywords"], ["收款人", "备用卡"])
        self.assertFalse(updated["multi_order"]["adopt_existing_pending_orders"])
        self.assertNotIn("minimum_remaining_balance", updated["multi_order"])
        self.assertEqual(updated["password"]["value"], "")
        self.assertEqual(updated["notification"]["transfer_name"], "张三")

    def test_payment_keyword_options_keep_disabled_entries_and_active_order(self) -> None:
        values = public_config(self.cfg)
        values.pop("payment_keywords", None)
        values["payment_keyword_options"] = [
            {"keyword": "魔仙", "enabled": True},
            {"keyword": "备用卡", "enabled": False},
        ]

        updated = validate_dashboard_settings(self.cfg, values)

        self.assertEqual(updated["payment"]["keyword_options"], values["payment_keyword_options"])
        self.assertEqual(updated["payment"]["keywords"], ["魔仙"])
        self.assertEqual(updated["multi_order"]["payment_keywords"], ["魔仙"])

    def test_rejects_when_all_payment_keywords_are_disabled(self) -> None:
        values = public_config(self.cfg)
        values.pop("payment_keywords", None)
        values["payment_keyword_options"] = [{"keyword": "魔仙", "enabled": False}]

        with self.assertRaisesRegex(ValueError, "启用一项"):
            validate_dashboard_settings(self.cfg, values)

    def test_fast_speed_profile_updates_only_reversible_timing(self) -> None:
        values = public_config(self.cfg)
        values["speed_mode"] = "fast"

        updated = validate_dashboard_settings(self.cfg, values)

        self.assertEqual(updated["speed_mode"], "fast")
        self.assertEqual(updated["poll_interval_sec"], 0.5)
        self.assertEqual(updated["action_throttle_sec"], 0.45)
        self.assertEqual(updated["submit_flow"]["secondary_confirm_delay_sec"], 0.35)
        self.assertEqual(updated["submit_flow"]["state_poll_sec"], 0.25)
        self.assertEqual(updated["form_input"], {"focus_wait_sec": 0.12, "verify_wait_sec": 0.25})
        self.assertEqual(updated["cancel_flow"]["confirm_delay_sec"], 1.2)
        self.assertEqual(updated["submit_flow"]["password_timeout_sec"], 8.0)

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

    @patch("canvas_bot.dashboard.save_remembered_secrets")
    @patch("canvas_bot.dashboard.load_remembered_secrets", return_value={})
    def test_dashboard_password_is_validated_and_persisted(
        self, _load: Mock, save: Mock
    ) -> None:
        state = DashboardState(Path("settings.json"), Path.cwd())
        self.assertIn("加密保存", state.set_payment_password("123456"))
        self.assertEqual(state.payment_password, "123456")
        save.assert_called_with({"payment_password": "123456", "pushplus_token": ""})
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
            self.assertEqual(environment["QJD67_DASHBOARD_LOG"], str(state.log_path))
            self.assertEqual(environment["PYTHONUNBUFFERED"], "1")
            self.assertNotIn("QJD67_PAY_PASSWORD", environment)

    @patch("canvas_bot.dashboard.os.name", "nt")
    @patch("canvas_bot.dashboard.subprocess.run")
    def test_stop_requests_graceful_browser_shutdown_before_force_kill(self, run: Mock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "settings.json"
            write_config(config_path, self.cfg)
            state = DashboardState(config_path, Path(directory))
            process = Mock()
            process.pid = 4321
            process.poll.return_value = None
            state.process = process

            ok, message = state.stop()

            self.assertTrue(ok)
            self.assertIn("安全停止", message)
            self.assertFalse(run.called)
            process.wait.assert_called_once_with(timeout=8)
            self.assertFalse(state.stop_signal_path.exists())

    def test_telegram_requires_token_and_chat_id(self) -> None:
        self.assertFalse(TelegramNotifier(token="token-only", chat_id="").enabled)
        self.assertFalse(TelegramNotifier(token="", chat_id="123").enabled)
        self.assertTrue(TelegramNotifier(token="token", chat_id="123").enabled)


if __name__ == "__main__":
    unittest.main()
