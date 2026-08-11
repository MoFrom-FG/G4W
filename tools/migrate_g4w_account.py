# -*- coding: utf-8 -*-
"""迁移 G4W 微信账号数据:旧账号 -> 新账号(账号 ID 由命令行参数提供)。

用法:
  python migrate_g4w_account.py --dry-run
      --data <G4W-data路径>
      --old-user <旧微信OpenID> --new-user <新微信OpenID>
      --old-acct <旧bot账号ID> --new-acct <新bot账号ID>
      [--stale-acct <更早残留账号ID>]   # 只打印计划
  python migrate_g4w_account.py          # 执行迁移(参数同上)
      --data ... --old-user ... --new-user ... --old-acct ... --new-acct ...

备份: 迁移前请确认已备份 G4W-data（本脚本不负责备份）。
"""
import argparse
import json
import os
from pathlib import Path

# 配置（由命令行参数注入，不入库硬编码）
DATA: Path = None
OLD_USER = NEW_USER = OLD_ACCT = NEW_ACCT = STALE_ACCT = ""
OLD_USER_SEG = NEW_USER_SEG = OLD_ACCT_FILE = NEW_ACCT_FILE = ""
REPLACEMENTS = []
RENAMES = []

# 排除目录（历史/内容寻址/缓存，不做内容替换）
EXCLUDE_DIRS = {"backups", "hybrid", "short-path-mirror", ".git", "__pycache__", "kb_staging"}
# 参与内容替换的扩展名
TEXT_EXTS = {".json", ".md", ".txt", ".html", ".csv", ".log", ".ini", ".cfg"}

# 特殊处理的目标文件（相对 DATA 路径）
BINDINGS = Path("memory/conversations/bindings.json")
CHECKIN = Path("checkin-config.json")
ACCOUNTS_DIR = Path("accounts")

# 目录/文件重命名计划（相对 DATA）: 旧 -> 新（由 configure 构建）


def configure(*, data, old_user, new_user, old_acct, new_acct, stale_acct=""):
    """注入账号与数据目录配置，构建派生常量。"""
    global DATA, OLD_USER, NEW_USER, OLD_ACCT, NEW_ACCT, STALE_ACCT
    global OLD_USER_SEG, NEW_USER_SEG, OLD_ACCT_FILE, NEW_ACCT_FILE
    global REPLACEMENTS, RENAMES
    DATA = Path(data)
    OLD_USER, NEW_USER = old_user, new_user
    OLD_ACCT, NEW_ACCT = old_acct, new_acct
    STALE_ACCT = stale_acct
    OLD_USER_SEG = OLD_USER.replace("@", "_")
    NEW_USER_SEG = NEW_USER.replace("@", "_")
    OLD_ACCT_FILE = OLD_ACCT.replace("@", "-")
    NEW_ACCT_FILE = NEW_ACCT.replace("@", "-")
    # 内容替换对（先长后短，防止部分重叠）
    REPLACEMENTS = [
        (OLD_USER, NEW_USER),
        (OLD_USER_SEG, NEW_USER_SEG),
        (OLD_ACCT, NEW_ACCT),
        (OLD_ACCT_FILE, NEW_ACCT_FILE),
    ]
    if STALE_ACCT:
        REPLACEMENTS.append((STALE_ACCT, NEW_ACCT))  # 残留旧账号一并并入新账号
    RENAMES = [
        (Path("memory/conversations") / OLD_USER_SEG, Path("memory/conversations") / NEW_USER_SEG),
        (Path("memory/users") / f"{OLD_USER_SEG}.md", Path("memory/users") / f"{NEW_USER_SEG}.md"),
        (Path("memory/operational") / f"{OLD_USER_SEG}.md", Path("memory/operational") / f"{NEW_USER_SEG}.md"),
    ]


def log(msg):
    print(msg)


def apply_replacements(text: str) -> str:
    for old, new in REPLACEMENTS:
        text = text.replace(old, new)
    return text


def plan_text_replacements():
    """收集所有需要做内容替换的文件。"""
    files = []
    for root, dirs, names in os.walk(DATA):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for name in names:
            p = Path(root) / name
            rel = p.relative_to(DATA)
            if rel.parts and rel.parts[0] in EXCLUDE_DIRS:
                continue
            if p.suffix.lower() not in TEXT_EXTS:
                continue
            try:
                with open(p, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if any(old in text for old, _ in REPLACEMENTS):
                files.append(rel)
    return files


def execute_text_replacements(files, dry_run):
    """对文件做内容替换写回。bindings/checkin 稍后由 merge_dup_bindings 重写，这里先跳过。"""
    special = {str(BINDINGS), str(CHECKIN)}
    for rel in files:
        if str(rel) in special:
            continue
        p = DATA / rel
        with open(p, encoding="utf-8") as fh:
            text = fh.read()
        new_text = apply_replacements(text)
        if new_text != text and not dry_run:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(new_text)
        if new_text != text:
            log(f"  [replace] {rel}")


def merge_dup_bindings(rel_path, dry_run):
    """bindings.json / checkin-config.json: 多条绑定替换后 key 重复则合并（保留 updatedAt 最新者）。"""
    p = DATA / rel_path
    with open(p, encoding="utf-8") as fh:
        obj = json.load(fh)
    if "bindings" not in obj or not isinstance(obj["bindings"], dict):
        return
    merged = {}
    for key, entry in obj["bindings"].items():
        new_key = apply_replacements(key)
        new_entry = json.loads(json.dumps(entry))
        for k in new_entry:
            if isinstance(new_entry[k], str):
                new_entry[k] = apply_replacements(new_entry[k])
        if new_key in merged:
            old_entry = merged[new_key]
            t_new = new_entry.get("updatedAt") or new_entry.get("lastInboundAt") or 0
            t_old = old_entry.get("updatedAt") or old_entry.get("lastInboundAt") or 0
            keep = new_entry if t_new >= t_old else old_entry
            for k, v in new_entry.items():
                keep.setdefault(k, v)
            merged[new_key] = keep
            log(f"  [merge] {rel_path}: 重复绑定合并 -> {new_key} (updatedAt={keep.get('updatedAt')})")
        else:
            merged[new_key] = new_entry
    obj["bindings"] = merged
    if not dry_run:
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
    log(f"  [rewrite] {rel_path}: 绑定数 {len(merged)}")


def handle_accounts(dry_run):
    """accounts 目录: 删除旧账号文件（新账号文件保留，其 userId 已是新值）。"""
    old_file = ACCOUNTS_DIR / f"{OLD_ACCT_FILE}.json"
    new_file = ACCOUNTS_DIR / f"{NEW_ACCT_FILE}.json"
    if (DATA / old_file).exists():
        log(f"  [delete] accounts/{old_file.name}")
        if not dry_run:
            (DATA / old_file).unlink()
    if (DATA / new_file).exists():
        p = DATA / new_file
        with open(p, encoding="utf-8") as fh:
            obj = json.load(fh)
        changed = False
        if obj.get("userId") != NEW_USER:
            obj["userId"] = NEW_USER
            changed = True
        if obj.get("accountId") != NEW_ACCT:
            obj["accountId"] = NEW_ACCT
            changed = True
        if changed:
            log(f"  [fix] accounts/{new_file.name}: accountId/userId 校正为新账号")
            if not dry_run:
                with open(p, "w", encoding="utf-8") as fh:
                    json.dump(obj, fh, ensure_ascii=False, indent=2)


def execute_renames(dry_run):
    for old, new in RENAMES:
        src = DATA / old
        dst = DATA / new
        exists = src.exists()
        log(f"  {'OK ' if exists else 'MISS'} {old} -> {new}")
        if exists and not dry_run:
            src.rename(dst)


def main():
    ap = argparse.ArgumentParser(description="迁移 G4W 微信账号数据（账号 ID 通过参数提供，不硬编码）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--data", required=True, help="G4W-data 目录路径")
    ap.add_argument("--old-user", required=True, help="旧微信 OpenID（如 xxx@im.wechat）")
    ap.add_argument("--new-user", required=True, help="新微信 OpenID")
    ap.add_argument("--old-acct", required=True, help="旧 bot 账号 ID（如 xxx@im.bot）")
    ap.add_argument("--new-acct", required=True, help="新 bot 账号 ID")
    ap.add_argument("--stale-acct", default="", help="可选:更早的残留账号 ID（并入新账号）")
    args = ap.parse_args()
    configure(
        data=args.data,
        old_user=args.old_user,
        new_user=args.new_user,
        old_acct=args.old_acct,
        new_acct=args.new_acct,
        stale_acct=args.stale_acct,
    )
    dry = args.dry_run
    mode = "DRY-RUN（不写入）" if dry else "EXECUTE（写入）"
    log(f"== G4W 账号数据迁移 [{mode}] ==")
    log(f"   旧账号: {OLD_ACCT} / {OLD_USER}")
    log(f"   新账号: {NEW_ACCT} / {NEW_USER}")
    log(f"   数据目录: {DATA}")
    log("")

    log("[1/4] 内容替换文件清单：")
    files = plan_text_replacements()
    for rel in files:
        log(f"  {rel}")
    log(f"  共 {len(files)} 个文件")

    log("")
    log("[2/4] 内容替换写回：")
    execute_text_replacements(files, dry)

    log("")
    log("[3/4] bindings/checkin 重复键合并：")
    for rel in (BINDINGS, CHECKIN):
        if (DATA / rel).exists():
            merge_dup_bindings(rel, dry)

    log("")
    log("[4/4] 目录/文件重命名 + accounts 处理：")
    execute_renames(dry)
    handle_accounts(dry)

    log("")
    if dry:
        log("== 以上为计划。确认后去掉 --dry-run 执行。==")
    else:
        log("== 迁移完成。==")


if __name__ == "__main__":
    main()
