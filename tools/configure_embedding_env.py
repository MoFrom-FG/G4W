#!/usr/bin/env python3
"""LEGACY optional helper: write EMBEDDING_* into package-local .env.

Product path (2026-07): ST thin HTTP under runtime/G4W-embedding.
Use 5_embedding_for_G4W.bat → install_embedding, then WeChat /vector on.
This menu is kept only for rare remote OpenAI-compatible overrides.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# portable: this file is tools/ under G4W root
PORTABLE_ROOT = Path(__file__).resolve().parents[1]
G4W_HOME = PORTABLE_ROOT / "runtime" / "G4W-main"
ENV_FILE = G4W_HOME / ".env"
DEFAULT_INDEX = PORTABLE_ROOT / "runtime" / "G4W-vector-index"

sys.path.insert(0, str(G4W_HOME))

from G4W.core.config import _read_env_file  # noqa: E402
from G4W.memory.instructions import update_env_file  # noqa: E402

EMBED_KEYS = (
    "EMBEDDING_PROVIDER",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_MODEL",
    "EMBEDDING_API_KEY",
    "EMBEDDING_DIM",
    "EMBEDDING_TIMEOUT_S",
    "EMBEDDING_BATCH_SIZE",
    "G4W_VECTOR_RETRIEVAL",
    "G4W_VECTOR_INDEX_DIR",
)


def _mask(key: str, value: str) -> str:
    if "KEY" in key.upper() and value:
        return "***"
    return value if value else "(unset)"


def show() -> int:
    if not ENV_FILE.is_file():
        print(f"[G4W] .env missing: {ENV_FILE}")
        return 1
    data = _read_env_file(ENV_FILE)
    print(f"--- from {ENV_FILE}")
    for k in EMBED_KEYS:
        print(f"  {k}={_mask(k, data.get(k, ''))}")
    return 0


def clear_keys() -> int:
    if not ENV_FILE.is_file():
        print(f"[G4W] .env missing: {ENV_FILE}")
        return 1
    keys = set(EMBED_KEYS)
    lines = ENV_FILE.read_text(encoding="utf-8-sig").splitlines()
    out: list[str] = []
    removed: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = (
            stripped.split("=", 1)[0].strip()
            if "=" in stripped and not stripped.startswith("#")
            else ""
        )
        if key in keys:
            removed.append(key)
            continue
        out.append(line)
    ENV_FILE.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8", newline="\n")
    print(f"[G4W] cleared: {sorted(set(removed)) or '(none present)'}")
    return 0


def write_lmstudio() -> int:
    updates = {
        "EMBEDDING_PROVIDER": "openai",
        "EMBEDDING_BASE_URL": "http://127.0.0.1:1234/v1",
        "EMBEDDING_MODEL": "text-embedding-nomic-embed-text-v1.5",
        "EMBEDDING_API_KEY": "lm-studio",
        "EMBEDDING_DIM": "768",
        "G4W_VECTOR_RETRIEVAL": "1",
        "G4W_VECTOR_INDEX_DIR": str(DEFAULT_INDEX),
    }
    update_env_file(ENV_FILE, updates)
    print("[G4W] wrote LM Studio defaults:")
    for k, v in updates.items():
        print(f"  {k}={_mask(k, v)}")
    return 0


def write_hash() -> int:
    # clear remote fields then set provider=hash
    if ENV_FILE.is_file():
        clear_remote = {
            "EMBEDDING_BASE_URL",
            "EMBEDDING_MODEL",
            "EMBEDDING_API_KEY",
            "EMBEDDING_DIM",
            "EMBEDDING_TIMEOUT_S",
            "EMBEDDING_BATCH_SIZE",
        }
        lines = ENV_FILE.read_text(encoding="utf-8-sig").splitlines()
        out = []
        for line in lines:
            stripped = line.strip()
            key = (
                stripped.split("=", 1)[0].strip()
                if "=" in stripped and not stripped.startswith("#")
                else ""
            )
            if key in clear_remote:
                continue
            out.append(line)
        ENV_FILE.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8", newline="\n")
    update_env_file(ENV_FILE, {"EMBEDDING_PROVIDER": "hash"})
    print("[G4W] wrote EMBEDDING_PROVIDER=hash (remote fields cleared)")
    return 0


def write_custom(args: argparse.Namespace) -> int:
    if not args.model:
        print("[G4W] --model required for custom")
        return 2
    updates = {
        "EMBEDDING_PROVIDER": args.provider or "openai",
        "EMBEDDING_BASE_URL": args.base_url or "http://127.0.0.1:1234/v1",
        "EMBEDDING_MODEL": args.model,
        "EMBEDDING_API_KEY": args.api_key or "lm-studio",
        "EMBEDDING_DIM": str(args.dim or 768),
        "G4W_VECTOR_RETRIEVAL": "1" if args.vector else "0",
        "G4W_VECTOR_INDEX_DIR": str(args.index_dir or DEFAULT_INDEX),
    }
    update_env_file(ENV_FILE, updates)
    print("[G4W] wrote custom embedding config:")
    for k, v in updates.items():
        print(f"  {k}={_mask(k, v)}")
    return 0


def set_vector(on: bool) -> int:
    update_env_file(ENV_FILE, {"G4W_VECTOR_RETRIEVAL": "1" if on else "0"})
    print(f"[G4W] G4W_VECTOR_RETRIEVAL={'1' if on else '0'}")
    return 0


def interactive() -> int:
    if not ENV_FILE.is_file():
        print(f"[G4W] .env missing. Run 3_env_for_G4W.bat first.")
        print(f"  expected: {ENV_FILE}")
        return 1
    while True:
        print()
        print("========================================")
        print(" [LEGACY] Embedding .env 菜单")
        print(" 产品路径请用: 5_embedding_for_G4W.bat")
        print(" (ST 外挂 + /vector on；本菜单仅远程/调试覆盖)")
        print("========================================")
        print(f"  目标: {ENV_FILE}")
        print("  1) LM Studio 本地 (nomic · dim=768 · 开向量)")
        print("  2) 自定义 OpenAI 兼容接口")
        print("  3) 仅 hash (关闭真 embedding)")
        print("  4) 开启向量检索 (G4W_VECTOR_RETRIEVAL=1)")
        print("  5) 关闭向量检索 (G4W_VECTOR_RETRIEVAL=0)")
        print("  6) 查看当前配置")
        print("  7) 清除 Embedding/向量相关项")
        print("  0) 退出")
        choice = input("请选择 [0-7]: ").strip()
        if choice in ("0", "q", "Q"):
            print("[G4W] 完成。")
            return 0
        if choice == "1":
            print("预览: openai / 127.0.0.1:1234 / nomic / dim=768 / vector=1")
            if input("写入 .env? [Y/n]: ").strip().lower() not in ("n", "no"):
                write_lmstudio()
        elif choice == "2":
            base = input("EMBEDDING_BASE_URL [http://127.0.0.1:1234/v1]: ").strip() or (
                "http://127.0.0.1:1234/v1"
            )
            model = input("EMBEDDING_MODEL: ").strip()
            if not model:
                print("模型名不能为空。")
                continue
            key = input("EMBEDDING_API_KEY [lm-studio]: ").strip() or "lm-studio"
            dim = input("EMBEDDING_DIM [768]: ").strip() or "768"
            idx = input(
                f"G4W_VECTOR_INDEX_DIR [回车={DEFAULT_INDEX}]: "
            ).strip() or str(DEFAULT_INDEX)
            ns = argparse.Namespace(
                provider="openai",
                base_url=base,
                model=model,
                api_key=key,
                dim=int(dim),
                vector=True,
                index_dir=idx,
            )
            write_custom(ns)
        elif choice == "3":
            if input("写入 hash 模式? [Y/n]: ").strip().lower() not in ("n", "no"):
                write_hash()
        elif choice == "4":
            set_vector(True)
        elif choice == "5":
            set_vector(False)
        elif choice == "6":
            show()
        elif choice == "7":
            if input("确认清除相关项? [y/N]: ").strip().lower() in ("y", "yes"):
                clear_keys()
        else:
            print("无效选择。")
        print()
        print("提示: 真语义需 LM Studio 已加载同模型；再运行 start_G4W_ga.bat")
        if input("返回菜单? [Y/n]: ").strip().lower() in ("n", "no"):
            print("[G4W] 完成。")
            return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Configure G4W embedding env")
    ap.add_argument(
        "action",
        nargs="?",
        default="interactive",
        choices=(
            "interactive",
            "lmstudio",
            "hash",
            "show",
            "clear",
            "vector-on",
            "vector-off",
            "custom",
        ),
    )
    ap.add_argument("--provider", default="openai")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--dim", type=int, default=768)
    ap.add_argument("--index-dir", default="")
    ap.add_argument("--vector", action="store_true", default=True)
    ap.add_argument("--no-vector", action="store_true")
    args = ap.parse_args()
    if args.no_vector:
        args.vector = False

    if not ENV_FILE.is_file() and args.action not in ("show",):
        print(f"[G4W] .env missing. Run 3_env_for_G4W.bat first.")
        print(f"  expected: {ENV_FILE}")
        return 1

    if args.action == "interactive":
        return interactive()
    if args.action == "lmstudio":
        return write_lmstudio()
    if args.action == "hash":
        return write_hash()
    if args.action == "show":
        return show()
    if args.action == "clear":
        return clear_keys()
    if args.action == "vector-on":
        return set_vector(True)
    if args.action == "vector-off":
        return set_vector(False)
    if args.action == "custom":
        return write_custom(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
