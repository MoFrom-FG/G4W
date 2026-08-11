import unittest
from pathlib import Path


class LauncherScriptTests(unittest.TestCase):
    def test_start_script_checks_real_module_entrypoint(self):
        portable_root = Path(__file__).resolve().parents[4]
        script = (portable_root / "start_G4W_ga.bat").read_text(encoding="utf-8")

        self.assertIn(r"%G4W_HOME%\G4W\__main__.py", script)
        self.assertNotIn(r"%G4W_HOME%\G4W\main.py", script)

    def test_G4W_batch_files_use_cmd_safe_crlf(self):
        portable_root = Path(__file__).resolve().parents[4]
        names = (
            "1_prepare_G4W_ga.bat",
            "2_key_for_ga.bat",
            "3_env_for_G4W.bat",
            "4_login_G4W_ga.bat",
            "start_G4W_ga.bat",
            "stop_G4W_ga.bat",
        )
        for name in names:
            raw = (portable_root / name).read_bytes()
            self.assertNotIn(b"\xef\xbb\xbf", raw[:3], name)
            self.assertNotIn(b"\r\r\n", raw, name)
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""), name)


if __name__ == "__main__":
    unittest.main()
