"""TASK-D: install_embedding dry-run / scaffold / model download / mark."""
from __future__ import annotations

import json
import importlib.util
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


_INSTALLER = Path(__file__).resolve().parents[1] / "memory" / "vector" / "install_embedding.py"
_SPEC = importlib.util.spec_from_file_location("_G4W_test_install_embedding", _INSTALLER)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"cannot load {_INSTALLER}")
ie = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ie)


def _write_ready_model(model: Path) -> None:
    model.mkdir(parents=True, exist_ok=True)
    (model / "config.json").write_text("{}", encoding="utf-8")
    (model / "tokenizer.json").write_text("{}", encoding="utf-8")
    (model / "model.safetensors").write_bytes(b"weights")


class TestInstallEmbedding(unittest.TestCase):
    def test_probe_empty(self):
        with tempfile.TemporaryDirectory() as td:
            r = ie.probe_layout(Path(td))
            self.assertFalse(r["root_exists"])
            self.assertFalse(r["launchable"])
            self.assertIn("root", r["missing"])
            self.assertEqual(r.get("backend"), "st")

    def test_scaffold_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = ie.scaffold_layout(root=root, dry_run=True, port=18080)
            self.assertTrue(r["ok"])
            self.assertTrue(r["dry_run"])
            self.assertEqual(list(root.rglob("server.py")), [])

    def test_scaffold_writes_st_layout(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = ie.scaffold_layout(root=root, dry_run=False, port=18080)
            self.assertTrue(r["ok"])
            emb = Path(r["root"])
            self.assertTrue((emb / "server.py").is_file())
            bat = emb / "start_embed.bat"
            self.assertTrue(bat.is_file())
            text = bat.read_text(encoding="utf-8")
            self.assertIn("server.py", text)
            self.assertIn('EMBED_DEVICE=auto', text)
            self.assertNotIn("text-embeddings-inference", text)
            requirements = (emb / "requirements.txt").read_text(encoding="utf-8")
            self.assertIn("numpy", requirements)
            self.assertIn("huggingface-hub", requirements)

    def test_torch_plan_auto_prefers_gpu_and_nju_cuda_index(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch.object(
                ie, "_nvidia_gpu_probe", return_value={"available": True, "gpus": ["NVIDIA Test GPU, 999.0"]}
            ):
                plan = ie._torch_install_plan()
        self.assertEqual(plan["requested"], "auto")
        self.assertEqual(plan["resolved"], "gpu")
        self.assertEqual(plan["channel"], "cu128")
        self.assertEqual(plan["indexes"][0], "https://mirrors.nju.edu.cn/pytorch/whl/cu128")
        self.assertEqual(plan["indexes"][1], "https://mirrors.aliyun.com/pytorch-wheels/cu128")
        self.assertEqual(plan["indexes"][-1], "https://download.pytorch.org/whl/cu128")

    def test_torch_plan_auto_falls_back_cpu_without_nvidia(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch.object(ie, "_nvidia_gpu_probe", return_value={"available": False}):
                plan = ie._torch_install_plan()
        self.assertEqual(plan["resolved"], "cpu")
        self.assertEqual(plan["channel"], "cpu")
        self.assertEqual(plan["indexes"][0], "https://mirrors.nju.edu.cn/pytorch/whl/cpu")
        self.assertEqual(plan["indexes"][1], "https://mirrors.aliyun.com/pytorch-wheels/cpu")

    def test_torch_plan_force_gpu_and_custom_index(self):
        env = {
            "G4W_TORCH_MODE": "gpu",
            "G4W_TORCH_CUDA_CHANNEL": "cu128",
            "G4W_TORCH_INDEX_URL": "https://custom.example/cu128/",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            with mock.patch.object(ie, "_nvidia_gpu_probe", return_value={"available": False}):
                plan = ie._torch_install_plan()
        self.assertEqual(plan["resolved"], "gpu")
        self.assertEqual(plan["indexes"][0], "https://custom.example/cu128")

    def test_aliyun_torch_source_uses_flat_find_links_without_dependencies(self):
        args = ie._torch_source_args("https://mirrors.aliyun.com/pytorch-wheels/cu128")
        self.assertEqual(
            args,
            [
                "--no-deps",
                "--no-index",
                "--find-links",
                "https://mirrors.aliyun.com/pytorch-wheels/cu128",
            ],
        )

    def test_nju_torch_source_is_a_pip_index(self):
        args = ie._torch_source_args("https://mirrors.nju.edu.cn/pytorch/whl/cu128/")
        self.assertEqual(args, ["--index-url", "https://mirrors.nju.edu.cn/pytorch/whl/cu128"])

    def test_official_torch_source_remains_index_url(self):
        args = ie._torch_source_args("https://download.pytorch.org/whl/cu128/")
        self.assertEqual(args, ["--index-url", "https://download.pytorch.org/whl/cu128"])

    def test_write_config_refuses_not_launchable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            emb = root / "G4W-embedding"
            emb.mkdir(parents=True)
            (emb / "server.py").write_text("# stub\n", encoding="utf-8")
            (emb / "start_embed.bat").write_text("@echo off\n", encoding="utf-8")
            vpy = emb / ".venv" / "Scripts" / "python.exe"
            vpy.parent.mkdir(parents=True)
            vpy.write_bytes(b"")
            probe = ie.probe_layout(root)
            self.assertFalse(probe["launchable"])
            r = ie.write_config(
                installed=True, enabled=False, require_ready=True, root=root, port=18080
            )
            self.assertFalse(r.get("ok"))
            self.assertIn("not launchable", str(r.get("error", "")))

    def test_mark_only_launchable_with_server_venv_model(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            emb = root / "G4W-embedding"
            model = emb / "models" / ie.PINNED_MODEL
            _write_ready_model(model)
            (emb / "server.py").write_text("# stub\n", encoding="utf-8")
            (emb / "start_embed.bat").write_text("@echo off\n", encoding="utf-8")
            vpy = emb / ".venv" / "Scripts" / "python.exe"
            vpy.parent.mkdir(parents=True)
            vpy.write_bytes(b"MZ")
            probe = ie.probe_layout(root)
            self.assertTrue(probe["launchable"], probe)
            r = ie.run_install(
                yes=True, mark_only=True, skip_pip=True, root=root, port=18080
            )
            self.assertTrue(r.get("ok"), r)
            cfg_path = emb / "vector_config.json"
            self.assertTrue(cfg_path.is_file(), r)
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertTrue(cfg.get("installed"))
            self.assertFalse(cfg.get("enabled"))
            self.assertEqual(cfg.get("base_url"), "http://127.0.0.1:18080")
            self.assertEqual(cfg.get("backend"), "st")

    def test_probe_rejects_partial_model(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            emb = root / "G4W-embedding"
            model = emb / "models" / ie.PINNED_MODEL
            model.mkdir(parents=True)
            (model / "config.json").write_text("{}", encoding="utf-8")
            (model / "tokenizer.json").write_text("{}", encoding="utf-8")
            (emb / "server.py").write_text("# stub\n", encoding="utf-8")
            vpy = emb / ".venv" / "Scripts" / "python.exe"
            vpy.parent.mkdir(parents=True)
            vpy.write_bytes(b"MZ")
            probe = ie.probe_layout(root)
            self.assertFalse(probe["model_ok"], probe)
            self.assertFalse(probe["launchable"], probe)
            self.assertFalse(probe["model_checks"]["weights"])

    def test_model_endpoints_prefer_env_then_mirror_and_official(self):
        with mock.patch.dict(
            "os.environ", {"G4W_HF_ENDPOINT": "https://custom.example/"}, clear=False
        ):
            endpoints = ie._model_endpoints()
        self.assertEqual(endpoints[0], "https://custom.example")
        self.assertIn("https://hf-mirror.com", endpoints)
        self.assertEqual(endpoints[-1], "https://huggingface.co")

    def test_download_model_dry_run_plans_mirror_fallback(self):
        with mock.patch.dict(
            "os.environ", {"G4W_HF_ENDPOINT": "", "G4W_HF_ENDPOINT": "", "HF_ENDPOINT": ""}, clear=False
        ):
            with tempfile.TemporaryDirectory() as td:
                result = ie.download_model(Path(td), dry_run=True)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["dry_run"], result)
        self.assertEqual(result["repo"], ie.PINNED_MODEL_REPO)
        self.assertEqual(result["endpoints"][0], "https://hf-mirror.com")
        self.assertEqual(result["endpoints"][-1], "https://huggingface.co")
        self.assertTrue(result["resume"])

    def test_download_model_falls_back_and_validates_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ie.scaffold_layout(root=root, dry_run=False)
            emb = root / "G4W-embedding"
            vpy = emb / ".venv" / "Scripts" / "python.exe"
            vpy.parent.mkdir(parents=True)
            vpy.write_bytes(b"MZ")
            calls = []

            def fake_stream(cmd, **kwargs):
                calls.append(kwargs["action"])
                if len(calls) == 1:
                    return {"action": kwargs["action"], "returncode": 1}
                _write_ready_model(emb / "models" / ie.PINNED_MODEL)
                return {"action": kwargs["action"], "returncode": 0}

            with mock.patch.object(ie, "_stream_command", side_effect=fake_stream):
                result = ie.download_model(
                    root,
                    endpoints=["https://mirror.example", "https://huggingface.co"],
                )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["endpoint"], "https://huggingface.co")
            self.assertEqual(len(calls), 2)
            self.assertTrue(result["probe"]["model_ok"])

    def test_run_install_dry_run_includes_model_download_plan(self):
        with tempfile.TemporaryDirectory() as td:
            result = ie.run_install(yes=True, dry_run=True, root=Path(td))
        self.assertTrue(result["ok"], result)
        model_steps = [step["model_download"] for step in result["steps"] if "model_download" in step]
        self.assertEqual(len(model_steps), 1)
        self.assertTrue(model_steps[0]["dry_run"])

    def test_vc_runtime_dry_run_uses_official_microsoft_url(self):
        with tempfile.TemporaryDirectory() as td:
            result = ie.ensure_windows_vc_runtime(Path(td), dry_run=True)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["dry_run"], result)
        self.assertEqual(result["download"], "https://aka.ms/vs/17/release/vc_redist.x64.exe")

    def test_vc_runtime_skips_when_torch_imports(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vpy = root / "G4W-embedding" / ".venv" / "Scripts" / "python.exe"
            vpy.parent.mkdir(parents=True)
            vpy.write_bytes(b"MZ")
            with mock.patch.object(
                ie, "_probe_torch", return_value={"ok": True, "version": "test"}
            ):
                result = ie.ensure_windows_vc_runtime(root, venv_python=vpy)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["skipped"], "torch_import_ready")

    def test_vc_runtime_installs_cached_official_installer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            emb = root / "G4W-embedding"
            vpy = emb / ".venv" / "Scripts" / "python.exe"
            vpy.parent.mkdir(parents=True)
            vpy.write_bytes(b"MZ")
            installer = emb / "bin" / "vc_redist.x64.exe"
            installer.parent.mkdir(parents=True)
            installer.write_bytes(b"MZ" + (b"0" * 1_000_000))
            probes = [
                {"ok": False, "error_tail": "c10.dll Microsoft Visual C++ Redistributable"},
                {"ok": True, "version": "test"},
            ]
            completed = types.SimpleNamespace(returncode=0, stdout="", stderr="")
            with mock.patch.object(ie, "_probe_torch", side_effect=probes):
                with mock.patch.object(ie.subprocess, "run", return_value=completed) as run:
                    result = ie.ensure_windows_vc_runtime(root, venv_python=vpy)
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["installed"], result)
            self.assertIn("/quiet", run.call_args.args[0])

    def test_mark_only_rejects_scaffold_without_venv_or_model(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ie.scaffold_layout(root=root, dry_run=False, port=18080)
            r = ie.run_install(
                yes=True, mark_only=True, skip_pip=True, root=root, port=18080
            )
            self.assertFalse(r.get("ok"), r)
            self.assertFalse(r.get("ready"), r)
            self.assertIn("not ready", r.get("error", ""))
            cfg_path = root / "G4W-embedding" / "vector_config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertFalse(cfg.get("installed"))

    def test_refuses_without_yes(self):
        with tempfile.TemporaryDirectory() as td:
            r = ie.run_install(yes=False, dry_run=False, root=Path(td))
            self.assertFalse(r["ok"])
            self.assertIn("yes", r.get("error", "").lower())


if __name__ == "__main__":
    unittest.main()
