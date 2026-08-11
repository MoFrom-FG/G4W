import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from G4W.wechat.account_store import WeixinAccountStore, account_file_name
from G4W.core.config import Config, _config_values
from G4W.cli import initializer


class InitializerTests(unittest.TestCase):
    def test_env_initialization_is_portable_and_keeps_optional_integrations_blank(self):
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / "G4W-main" / ".env"
            package_main = Path(td) / "portable" / "runtime" / "G4W-main"
            with mock.patch.object(initializer, "ENV_FILE", env_file), mock.patch.object(initializer, "PORTABLE_ROOT", Path(td) / "portable"), mock.patch.object(initializer, "MAIN_DIR", package_main):
                result = initializer.configure_env({
                    "G4W_USER_NAME": "小明",
                    "G4W_USER_GENDER": "male",
                    "G4W_BOT_NAME": "猫猫",
                    "G4W_CONDUCTOR_MODEL": "deepseek-v4-flash",
                    "G4W_WORKER_MODEL": "deepseek-v4-pro",
                    "G4W_TIMELINE_UI_THEME": "neko",
                })
            text = env_file.read_text(encoding="utf-8")
            self.assertTrue(result["pathsAreLocationDerived"])
            self.assertIn("G4W_USER_NAME=小明", text)
            self.assertIn("G4W_USER_GENDER=male", text)
            self.assertIn("G4W_TIMELINE_UI_THEME=neko", text)
            self.assertIn("G4W_DIDA_COMMAND=\n", text)
            self.assertIn("G4W_DIDA_TOKEN=\n", text)
            self.assertNotIn("G4W_TODAY_TASK_", text)
            self.assertIn("G4W_CHECKIN_MIN_INTERVAL_MS=600000", text)
            self.assertIn("G4W_CHECKIN_MAX_INTERVAL_MS=5400000", text)
            self.assertIn(f"G4W_WORKSPACE_ROOT={(Path(td) / 'portable').resolve()}", text)
            self.assertIn("G4W_SHARED_MEMORY_ROOT=${G4W_WORKSPACE_ROOT}/runtime/G4W-main/G4W/memory/sop", text)

    def test_env_initialization_drops_obsolete_today_task_keys(self):
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / "G4W-main" / ".env"
            env_file.parent.mkdir(parents=True)
            env_file.write_text(
                "G4W_TODAY_TASK_AUTH_CODE=obsolete-secret\n"
                "G4W_TODAY_TASK_PUSH_URL=https://obsolete.invalid\n"
                "G4W_TODAY_TASK_TIMEOUT_MS=12345\n",
                encoding="utf-8",
            )
            package_main = Path(td) / "portable" / "runtime" / "G4W-main"
            with mock.patch.object(initializer, "ENV_FILE", env_file), mock.patch.object(initializer, "PORTABLE_ROOT", Path(td) / "portable"), mock.patch.object(initializer, "MAIN_DIR", package_main):
                initializer.configure_env({
                    "G4W_USER_NAME": "小明",
                    "G4W_USER_GENDER": "neutral",
                    "G4W_BOT_NAME": "猫猫",
                    "G4W_CONDUCTOR_MODEL": "deepseek-v4-flash",
                    "G4W_WORKER_MODEL": "deepseek-v4-flash",
                    "G4W_TIMELINE_UI_THEME": "default",
                })
            self.assertNotIn("G4W_TODAY_TASK_", env_file.read_text(encoding="utf-8"))

    def test_key_initializer_writes_both_models_and_backs_up_existing_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            template = root / "template.py"
            target = root / "mykey.py"
            template.write_text(
                "a={'name':'deepseek-v4-pro','apikey': '',}\n"
                "b={'name':'deepseek-v4-flash','apikey': '',}\n",
                encoding="utf-8",
            )
            target.write_text("old=True\n", encoding="utf-8")
            with mock.patch.object(initializer, "KEY_TEMPLATE", template), mock.patch.object(initializer, "MYKEY_FILE", target):
                result = initializer.configure_ga_key("中文-key'\\value", replace_existing=True)
            content = target.read_text(encoding="utf-8")
            self.assertEqual(content.count("中文-key"), 2)
            compile(content, str(target), "exec")
            self.assertTrue(Path(result["backupFile"]).is_file())

    def test_startup_path_sync_is_visible_and_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            main = root / "runtime" / "G4W-main"
            env = main / ".env"
            env.parent.mkdir(parents=True)
            env.write_text("G4W_BOT_NAME=neko\n", encoding="utf-8")
            with mock.patch.object(initializer, "ENV_FILE", env), mock.patch.object(initializer, "MAIN_DIR", main), mock.patch.object(initializer, "PORTABLE_ROOT", root):
                first = initializer.sync_runtime_paths()
                second = initializer.sync_runtime_paths()
            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            text = env.read_text(encoding="utf-8")
            self.assertIn(f"G4W_WORKSPACE_ROOT={root.resolve()}", text)
            self.assertIn("G4W_SHARED_MEMORY_ROOT=${G4W_WORKSPACE_ROOT}/runtime/G4W-main/G4W/memory/sop", text)
            self.assertIn("G4W_BOT_NAME=neko", text)

    def test_config_never_scans_parent_G4W_env(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            package_main = root / "portable" / "runtime" / "G4W-main"
            package_main.mkdir(parents=True)
            external = root / "G4W-main"
            external.mkdir()
            (external / ".env").write_text("G4W_BOT_NAME=external\n", encoding="utf-8")
            with mock.patch("G4W.core.config.MAIN_DIR", package_main):
                self.assertEqual(_config_values(root / "state"), {})
                (package_main / ".env").write_text("G4W_BOT_NAME=local\n", encoding="utf-8")
                self.assertEqual(_config_values(root / "state")["G4W_BOT_NAME"], "local")

    def test_config_reads_original_compatible_checkin_interval_env(self):
        with tempfile.TemporaryDirectory() as td:
            main_dir = Path(td) / "G4W-main"
            main_dir.mkdir()
            (main_dir / ".env").write_text(
                "G4W_CHECKIN_MIN_INTERVAL_MS=600000\n"
                "G4W_CHECKIN_MAX_INTERVAL_MS=5400000\n",
                encoding="utf-8",
            )
            cleared = {key: "" for key in (
                "G4W_CHECKIN_MIN_INTERVAL_MS", "G4W_CHECKIN_MAX_INTERVAL_MS",
            )}
            with mock.patch("G4W.core.config.MAIN_DIR", main_dir), mock.patch.dict(os.environ, cleared):
                config = Config.load()
            self.assertEqual(config.checkin_minimum_minutes, 10)
            self.assertEqual(config.checkin_maximum_minutes, 90)

    def test_account_store_migrates_aggregate_to_per_account_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            account = {"accountId": "abc@im.bot", "token": "secret", "baseUrl": "https://example.invalid"}
            (root / "accounts.json").write_text(json.dumps({"accounts": {account["accountId"]: account}}), encoding="utf-8")
            store = WeixinAccountStore(root)
            self.assertTrue((root / account_file_name(account["accountId"])).is_file())
            self.assertEqual(store.read()["accounts"][account["accountId"]]["token"], "secret")


if __name__ == "__main__":
    unittest.main()
