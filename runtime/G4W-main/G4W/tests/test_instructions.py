import tempfile
import types
import unittest
from pathlib import Path

from G4W.core.capabilities import CapabilityRegistry
from G4W.core.config import Config, _read_env_file
from G4W.core.storage import JsonStore
from G4W.agents.controller import G4WController
from G4W.memory.instructions import InstructionManager, render_instruction_template, update_env_file
from G4W.memory.sop_catalog import SopCatalog


class InstructionTests(unittest.TestCase):
    def test_env_update_collapses_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            env_file.write_text(
                "G4W_CHECKIN_ENABLED=false\n# keep this comment\nG4W_CHECKIN_ENABLED=true\nOTHER=value\n",
                encoding="utf-8",
            )

            update_env_file(env_file, {"G4W_CHECKIN_ENABLED": "false"})

            rendered = env_file.read_text(encoding="utf-8")
            self.assertEqual(rendered.count("G4W_CHECKIN_ENABLED="), 1)
            self.assertIn("G4W_CHECKIN_ENABLED=false", rendered)
            self.assertIn("# keep this comment", rendered)
            self.assertIn("OTHER=value", rendered)

    def test_pronoun_rendering_uses_explicit_placeholder_only(self):
        template = "{{USER_NAME}}说{{USER_PRONOUN}}喜欢data，机器人是{{BOT_NAME}}。"
        self.assertEqual(
            render_instruction_template(template, user_name="小明", user_gender="male", bot_name="猫猫"),
            "小明说他喜欢data，机器人是猫猫。",
        )
        self.assertIn(
            "她喜欢data",
            render_instruction_template(template, user_name="小红", user_gender="female", bot_name="猫猫"),
        )
        self.assertIn(
            "ta喜欢data",
            render_instruction_template(template, user_name="用户", user_gender="neutral", bot_name="猫猫"),
        )

    def test_data_instruction_files_win_over_templates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            persona_template = root / "templates" / "persona.md"
            operations_template = root / "templates" / "operations.md"
            persona_file = root / "memory" / "persona.md"
            operations_file = root / "memory" / "operations.md"
            persona_template.parent.mkdir(parents=True)
            persona_template.write_text("默认{{USER_PRONOUN}}", encoding="utf-8")
            operations_template.write_text("默认操作", encoding="utf-8")
            persona_file.parent.mkdir(parents=True)
            persona_file.write_text("自定义{{USER_PRONOUN}}", encoding="utf-8")
            config = types.SimpleNamespace(
                persona_template_file=persona_template,
                operations_template_file=operations_template,
                persona_file=persona_file,
                operations_file=operations_file,
            )
            manager = InstructionManager(config)
            persona, operations = manager.load(user_name="用户", user_gender="male", bot_name="G4W")
            self.assertEqual(persona, "自定义他")
            self.assertEqual(operations, "默认操作")

    def test_reread_reuses_active_session_and_history(self):
        calls = []

        class Instructions:
            def ensure_runtime_files(self): calls.append("ensure")
            def clear(self): calls.append("clear")

        class Session:
            def run(self, prompt, **kwargs):
                calls.append((prompt, kwargs))
                return "刷新完成"

        controller = G4WController.__new__(G4WController)
        controller.instructions = Instructions()
        session = Session()
        controller.sessions = {"sender": session}
        result = controller.reread("sender")
        self.assertEqual(result, "刷新完成")
        self.assertIs(controller.sessions["sender"], session)
        self.assertEqual(calls[:2], ["ensure", "clear"])
        self.assertFalse(calls[2][1]["user_message"])

    def test_capability_registry_is_compiled_from_sop_and_invalid_reload_keeps_last_valid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sop_root = root / "sop"
            demo = sop_root / "core" / "demo"
            demo.mkdir(parents=True)
            (demo / "SOP.md").write_text("# Demo\n", encoding="utf-8")
            (demo / "capabilities.json").write_text(
                '{"capabilities":[{"id":"demo.direct","route":"direct","allowedTools":[],"risk":"low","confirmation":"none"}]}',
                encoding="utf-8",
            )
            capabilities_file = root / "runtime" / "cache" / "capability-registry.json"
            SopCatalog(sop_root, capabilities_file).compile_capabilities()
            self.assertTrue(capabilities_file.is_file())
            registry = CapabilityRegistry(capabilities_file)
            self.assertIsNotNone(registry.get("demo.direct"))
            capabilities_file.write_text('{"capabilities":[{"id":"broken"}]}', encoding="utf-8")
            result = registry.try_reload()
            self.assertFalse(result["ok"])
            self.assertIsNotNone(registry.get("demo.direct"))

    def test_identity_update_writes_env_and_changes_rendered_placeholders(self):
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            env_file.write_text(
                "G4W_USER_NAME=小明\nG4W_USER_IDENTITY=\nG4W_USER_GENDER=male\nG4W_BOT_NAME=猫猫\n",
                encoding="utf-8",
            )
            controller = G4WController.__new__(G4WController)
            controller.config = Config(
                state_dir=Path(td) / "state", env_path=env_file,
                user_name="小明", user_identity="", user_gender="male", bot_name="猫猫",
            )
            cleared = []
            controller.instructions = types.SimpleNamespace(clear=lambda: cleared.append(True))
            controller.profiles = JsonStore(Path(td) / "profiles.json", {"senders": {}})

            updated = controller.update_identity("sender", "userIdentity", "主人")

            self.assertEqual(updated["userIdentity"], "主人")
            self.assertEqual(_read_env_file(env_file)["G4W_USER_IDENTITY"], "主人")
            self.assertEqual(controller.config.user_identity, "主人")
            self.assertEqual(controller.identity_profile("sender")["userIdentity"], "主人")
            self.assertEqual(cleared, [True])
            self.assertEqual(
                render_instruction_template(
                    "{{USER_NAME}}/{{USER_IDENTITY}}/{{BOT_NAME}}/{{USER_PRONOUN}}",
                    user_name=controller.config.user_name,
                    user_identity=controller.config.user_identity,
                    user_gender=controller.config.user_gender,
                    bot_name=controller.config.bot_name,
                ),
                "小明/主人/猫猫/他",
            )


if __name__ == "__main__":
    unittest.main()
