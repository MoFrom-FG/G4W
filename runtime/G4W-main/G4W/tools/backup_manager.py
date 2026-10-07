# -*- coding: utf-8 -*-
"""G4W 全局备份仓库管理工具。

统一目录: 由环境变量 G4W_BACKUPS_ROOT 指定，默认 ~/G4W-backups
索引表:   <root>/index.csv  (UTF-8 with BOM, Excel 可直接打开)

用法:
  python backup_manager.py add --type release-sync --source <dir> --note "同步前备份"
  python backup_manager.py add --type pre-test --source <dir> --move --note "迁移前备份"
  python backup_manager.py add --type manual --source <existing-dir> --no-copy --note "登记已有目录"
  python backup_manager.py list [--type t] [--pinned]
  python backup_manager.py info <name>
  python backup_manager.py pin <name> / unpin <name>
  python backup_manager.py delete <name> [--yes]
  python backup_manager.py prune [--type t] [--keep N] [--dry-run]

保留策略: prune 按类型保留最新 N 个(默认 5),pinned=1 的备份永不自动删除。
"""

from __future__ import annotations

import argparse
import csv
import datetime
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(os.environ.get("G4W_BACKUPS_ROOT") or (Path.home() / "G4W-backups")).expanduser().resolve()
INDEX = ROOT / "index.csv"
FIELDS = ["name", "workspace", "type", "created_at", "source", "size_bytes", "note", "pinned"]
DEFAULT_KEEP = 5


def _now() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _load_index() -> list[dict]:
    if not INDEX.is_file():
        return []
    with open(INDEX, "r", encoding="utf-8-sig", newline="") as f:
        return [row for row in csv.DictReader(f)]


def _save_index(rows: list[dict]) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with open(INDEX, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def cmd_add(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    if not source.exists():
        print(f"[错误] 来源不存在: {source}")
        return 1
    ws = args.workspace or "g4w"
    name = f"backup-{ws}-{args.type}-{_now()}"
    dest = ROOT / name
    if not args.no_copy:
        ROOT.mkdir(parents=True, exist_ok=True)
        if args.move:
            shutil.move(str(source), str(dest))
            print(f"[移动] {source} -> {dest}")
        else:
            shutil.copytree(source, dest)
            print(f"[复制] {source} -> {dest}")
    else:
        print(f"[登记] {source} (不复制)")
    rows = _load_index()
    rows.append({
        "name": name,
        "workspace": ws,
        "type": args.type,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": str(source),
        "size_bytes": str(_dir_size(dest) if not args.no_copy else _dir_size(source)),
        "note": args.note or "",
        "pinned": "0",
    })
    _save_index(rows)
    print(f"[完成] {name} 已登记到索引 ({INDEX})")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows = _load_index()
    if args.workspace:
        rows = [r for r in rows if r.get("workspace") == args.workspace]
    if args.type:
        rows = [r for r in rows if r.get("type") == args.type]
    if args.pinned:
        rows = [r for r in rows if r.get("pinned") == "1"]
    if not rows:
        print("(空)")
        return 0
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    header = f"{'名称':<42} {'工作区':<10} {'类型':<14} {'创建时间':<19} {'大小':>9}  备注"
    print(header)
    print("-" * len(header.encode("gbk", errors="replace")))
    for r in rows:
        pin = "★" if r.get("pinned") == "1" else " "
        name = r.get("name", "")[:32]
        print(f"{name:<42} {r.get('workspace','-'):<10} {r.get('type',''):<14} {r.get('created_at','')[:19]:<19} {_fmt_size(float(r.get('size_bytes') or 0)):>9}  {pin} {r.get('note','')[:40]}")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    rows = _load_index()
    row = next((r for r in rows if r.get("name") == args.name), None)
    if not row:
        print(f"[错误] 索引中无此备份: {args.name}")
        return 1
    for key in FIELDS:
        print(f"{key:<12}: {row.get(key, '')}")
    target = ROOT / args.name
    print(f"{'exists':<12}: {target.is_dir()}")
    return 0


def cmd_pin(args: argparse.Namespace) -> int:
    return _set_pin(args.name, "1" if args.pin else "0")


def _set_pin(name: str, value: str) -> int:
    rows = _load_index()
    found = False
    for r in rows:
        if r.get("name") == name:
            r["pinned"] = value
            found = True
    if not found:
        print(f"[错误] 索引中无此备份: {name}")
        return 1
    _save_index(rows)
    print(f"[完成] {name} pinned={value}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    rows = _load_index()
    row = next((r for r in rows if r.get("name") == args.name), None)
    if not row:
        print(f"[错误] 索引中无此备份: {args.name}")
        return 1
    target = ROOT / args.name
    if target.is_dir() and not args.yes:
        size = _fmt_size(float(row.get("size_bytes") or 0))
        answer = input(f"确认删除备份 {args.name} ({size})? [y/N] ").strip().lower()
        if answer != "y":
            print("已取消")
            return 0
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    _save_index([r for r in rows if r.get("name") != args.name])
    print(f"[完成] 已删除 {args.name} (目录+索引)")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    rows = _load_index()
    groups = {(r.get("workspace") or "g4w", r.get("type") or "") for r in rows}
    if args.workspace:
        groups = {g for g in groups if g[0] == args.workspace}
    if args.type:
        groups = {g for g in groups if g[1] == args.type}
    doomed: set[str] = set()
    total_freed = 0
    for ws, t in sorted(groups):
        group = [r for r in rows if (r.get("workspace") or "g4w") == ws and r.get("type") == t]
        group.sort(key=lambda r: r.get("created_at", ""), reverse=True)  # 新在前
        removable = [r for r in group if r.get("pinned") != "1"][args.keep:]
        for r in removable:
            target = ROOT / r.get("name", "")
            size = float(r.get("size_bytes") or 0)
            if args.dry_run:
                print(f"[计划] 删除 {r.get('name')} ({_fmt_size(size)}) [{ws}/{t}]")
            else:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                print(f"[删除] {r.get('name')} ({_fmt_size(size)}) [{ws}/{t}]")
            doomed.add(r.get("name", ""))
            total_freed += size
    print(f"合计释放: {_fmt_size(total_freed)}" + (" (dry-run, 未实际删除)" if args.dry_run else ""))
    if not args.dry_run and doomed:
        _save_index([r for r in rows if r.get("name") not in doomed])
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="G4W 全局备份仓库管理")
    sub = ap.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="新增备份(复制/移动/登记)")
    p_add.add_argument("--workspace", default=None, help="工作区标识,如 g4w(默认 g4w)")
    p_add.add_argument("--type", required=True, help="类型,如 release-sync / pre-test / migrate / manual")
    p_add.add_argument("--source", required=True, help="来源路径")
    p_add.add_argument("--note", default="", help="备注")
    p_add.add_argument("--move", action="store_true", help="移动而非复制")
    p_add.add_argument("--no-copy", action="store_true", help="只登记已存在的目录,不复制")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="列出索引")
    p_list.add_argument("--workspace", default=None, help="按工作区过滤")
    p_list.add_argument("--type", default=None, help="按类型过滤")
    p_list.add_argument("--pinned", action="store_true", help="只看手动保留的")
    p_list.set_defaults(func=cmd_list)

    p_info = sub.add_parser("info", help="查看单条详情")
    p_info.add_argument("name")
    p_info.set_defaults(func=cmd_info)

    p_pin = sub.add_parser("pin", help="标记手动保留")
    p_pin.add_argument("name")
    p_pin.set_defaults(func=cmd_pin, pin=True)

    p_unpin = sub.add_parser("unpin", help="取消手动保留")
    p_unpin.add_argument("name")
    p_unpin.set_defaults(func=cmd_pin, pin=False)

    p_del = sub.add_parser("delete", help="删除备份")
    p_del.add_argument("name")
    p_del.add_argument("--yes", action="store_true", help="跳过确认")
    p_del.set_defaults(func=cmd_delete)

    p_prune = sub.add_parser("prune", help="按类型清理旧备份")
    p_prune.add_argument("--workspace", default=None, help="只清理该工作区")
    p_prune.add_argument("--type", default=None, help="只清理该类型")
    p_prune.add_argument("--keep", type=int, default=DEFAULT_KEEP, help=f"每类保留数量(默认 {DEFAULT_KEEP})")
    p_prune.add_argument("--dry-run", action="store_true", help="只显示计划")
    p_prune.set_defaults(func=cmd_prune)

    args = ap.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()

