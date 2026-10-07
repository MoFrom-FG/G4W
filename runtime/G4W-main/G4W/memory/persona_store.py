"""人设预设（persona presets）—— 结构化存储、md 往返、激活与备份。

结构模型（保存为 ``<state>/memory/persona/presets/<id>.json``）::

    {
      "id": "neko", "name": "猫娘", "builtin": true, "description": "",
      "updatedAt": "2026-10-06T23:40:00",
      "sections": [
        {"title": "人格与关系", "intro": "", "children": [
            {"title": "基础定位", "body": "……markdown……"}
        ]}
      ]
    }

渲染规则（写入运行时唯一的人设文件 ``<state>/memory/persona/weixin-instructions.md``）：

* 只有 ``## 大节`` 与 ``### 小节``，**没有 H1**；
* 大节之间自动插入 ``---``（正文里不要手写，导入时会被当作分隔符丢掉）；
* 大节直属正文 → ``intro``；小节正文 → ``body``，markdown 原样保留（表格/引用/列表都不动）；
* ``{{USER_NAME}}`` 等 4 个占位符原样写入文件，由 ``render_instruction_template`` 在读取时替换。

设计约束（红线）：人设正文只写进 ``G4W-data``；包内模板/预设只随版本发布改，运行时**绝不回写**包内文件。
"""

from __future__ import annotations

import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

MAX_CHARS = 32768          # 超过就只警告，不拦截
BACKUP_KEEP = 3            # 每个预设只保留最近 3 份备份

PERSONA_VARIABLES = [
    {"token": "{{USER_NAME}}", "label": "你的名字", "desc": "来自“环境配置”的用户名"},
    {"token": "{{USER_IDENTITY}}", "label": "我该怎么称呼你", "desc": "用户身份／日常称呼"},
    {"token": "{{BOT_NAME}}", "label": "助手自称的名字", "desc": "来自“环境配置”的机器人名字"},
    {"token": "{{USER_PRONOUN}}", "label": "你的代词", "desc": "按性别显示：他／她／ta"},
]

_H2 = re.compile(r"^##\s+(.*\S)\s*$")
_H3 = re.compile(r"^###\s+(.*\S)\s*$")
_H1 = re.compile(r"^#\s+(.*\S)\s*$")
_RULE = re.compile(r"^-{3,}\s*$")


class PersonaError(ValueError):
    """人设操作失败（参数或内容不合法）。"""


def slugify_id(name: str, taken: set[str] | None = None) -> str:
    """把预设名转成稳定 id（中文直接保留，去掉路径不安全字符）。"""
    base = re.sub(r"[\\/:*?\"<>|\s]+", "-", str(name or "").strip()).strip("-")
    base = base or "persona"
    taken = taken or set()
    if base not in taken:
        return base
    index = 2
    while f"{base}-{index}" in taken:
        index += 1
    return f"{base}-{index}"


def _clean_block(text: str) -> str:
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def parse_markdown(text: str) -> tuple[list[dict], list[str]]:
    """把 markdown 解析成 sections；返回 (sections, warnings)。

    只认 ``##`` / ``###`` 作为结构；``#`` 行被丢弃并记一条警告；``---`` 视为大节分隔符丢弃。
    其余 markdown（表格、引用、列表、``####`` 及以下）原样进入所属槽位。
    """
    warnings: list[str] = []
    sections: list[dict] = []
    current_section: dict | None = None
    current_child: dict | None = None
    buffer: list[str] = []
    dropped_h1 = 0

    def flush() -> None:
        nonlocal buffer
        if current_section is None:
            buffer = []
            return
        body = "\n".join(buffer).strip("\n")
        if current_child is not None:
            current_child["body"] = _clean_block(current_child.get("body", "") + ("\n" + body if body and current_child.get("body") else body))
        elif body.strip():
            current_section["intro"] = _clean_block((current_section.get("intro") or "") + ("\n" + body if current_section.get("intro") else body))
        buffer = []

    for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        h3 = _H3.match(line)
        h2 = _H2.match(line)
        if h3:
            flush()
            if current_section is None:
                warnings.append(f"小节“{h3.group(1)}”出现在任何大节之前，已放入一个新建的“未命名大节”。")
                current_section = {"title": "未命名大节", "intro": "", "children": []}
                sections.append(current_section)
            current_child = {"title": h3.group(1), "body": ""}
            current_section.setdefault("children", []).append(current_child)
            continue
        if h2:
            flush()
            current_section = {"title": h2.group(1), "intro": "", "children": []}
            sections.append(current_section)
            current_child = None
            continue
        if _H1.match(line):
            dropped_h1 += 1
            continue
        if _RULE.match(line):
            # 大节分隔线：结构由 ## 决定，这里只跳过这一行本身（正文内部的空行必须保留，
            # 否则段落间距会在往返中丢失）。格式约定：正文里不要手写 ---。
            continue
        buffer.append(line)
    flush()

    if dropped_h1:
        warnings.append(f"忽略了 {dropped_h1} 行 H1 标题（人设文件只用 ## 与 ###）。")
    if not sections:
        raise PersonaError("没有解析到任何大节（需要 ## 标题）。")
    return sections, warnings


def render_markdown(sections: list[dict]) -> str:
    """sections → md（## 大节 / ### 小节 / 大节之间 --- / 无 H1）。"""
    blocks: list[str] = []
    for section in sections or []:
        title = str(section.get("title") or "").strip() or "未命名大节"
        chunk = [f"## {title}"]
        intro = _clean_block(section.get("intro") or "")
        if intro:
            chunk.append(intro)
        for child in section.get("children") or []:
            child_title = str(child.get("title") or "").strip() or "未命名小节"
            chunk.append(f"### {child_title}")
            body = _clean_block(child.get("body") or "")
            if body:
                chunk.append(body)
        blocks.append("\n\n".join(chunk))
    return "\n\n---\n\n".join(blocks).rstrip() + "\n"


def document_chars(sections: list[dict]) -> int:
    return len(render_markdown(sections))


def validate_sections(sections: list[dict]) -> list[str]:
    """返回警告列表；结构性错误直接抛 PersonaError。"""
    if not isinstance(sections, list) or not sections:
        raise PersonaError("人设至少要有一个大节。")
    warnings: list[str] = []
    for section in sections:
        if not isinstance(section, dict):
            raise PersonaError("大节结构不合法。")
        if not str(section.get("title") or "").strip():
            raise PersonaError("每个大节都必须有标题。")
        for child in section.get("children") or []:
            if not isinstance(child, dict) or not str(child.get("title") or "").strip():
                raise PersonaError("每个小节都必须有标题。")
    if not render_markdown(sections).strip():
        raise PersonaError("人设内容是空的。")
    chars = document_chars(sections)
    if chars > MAX_CHARS:
        warnings.append(f"人设长度 {chars} 字符，超过建议上限 {MAX_CHARS}（会显著增加每次请求的 token 成本）。")
    if not any(("{{" in json.dumps(section, ensure_ascii=False)) for section in sections):
        warnings.append("没有使用任何 {{占位符}}：确认你不需要自动填入用户名／助手名（页面顶部有变量说明）。")
    return warnings


class PersonaStore:
    """预设的落盘、激活、备份。所有写入只发生在 G4W-data。"""

    def __init__(self, config):
        self.config = config
        self.persona_dir = Path(config.persona_dir)
        self.presets_dir = self.persona_dir / "presets"
        self.backups_dir = self.persona_dir / ".backups"
        self.active_file = self.persona_dir / "active.json"
        self.runtime_file = Path(config.persona_file)
        self.templates_dir = Path(config.templates_dir) / "persona" / "presets"
        self.presets_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 内置预设 ----------
    def seed_builtins(self) -> list[str]:
        """包内预设缺则复制到用户态（**绝不覆盖**已有文件）。"""
        created: list[str] = []
        if self.templates_dir.is_dir():
            for source in sorted(self.templates_dir.glob("*.json")):
                target = self.presets_dir / source.name
                if not target.exists():
                    shutil.copy2(source, target)
                    created.append(target.stem)
        return created

    def builtin_ids(self) -> set[str]:
        ids = set()
        if self.templates_dir.is_dir():
            ids = {path.stem for path in self.templates_dir.glob("*.json")}
        return ids

    # ---------- 读取 ----------
    def _read_doc(self, preset_id: str) -> dict:
        path = self.presets_dir / f"{preset_id}.json"
        if not path.is_file():
            raise PersonaError(f"预设不存在：{preset_id}")
        try:
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as error:
            raise PersonaError(f"预设文件损坏：{preset_id}（{error}）")
        if not isinstance(doc, dict):
            raise PersonaError(f"预设文件格式不对：{preset_id}")
        doc.setdefault("id", preset_id)
        doc.setdefault("name", preset_id)
        doc.setdefault("sections", [])
        doc["builtin"] = preset_id in self.builtin_ids()
        return doc

    def list_presets(self) -> list[dict]:
        self.seed_builtins()
        active = self.active_id()
        rows: list[dict] = []
        for path in sorted(self.presets_dir.glob("*.json")):
            try:
                doc = self._read_doc(path.stem)
            except PersonaError:
                continue
            sections = doc.get("sections") or []
            rows.append({
                "id": doc["id"],
                "name": doc.get("name") or doc["id"],
                "builtin": bool(doc.get("builtin")),
                "description": str(doc.get("description") or ""),
                "updatedAt": str(doc.get("updatedAt") or ""),
                "active": doc["id"] == active,
                "chars": document_chars(sections),
                "sections": sections,
            })
        rows.sort(key=lambda item: (not item["active"], not item["builtin"], item["name"]))
        return rows

    def get(self, preset_id: str) -> dict:
        doc = self._read_doc(preset_id)
        doc["chars"] = document_chars(doc.get("sections") or [])
        return doc

    # ---------- 激活状态 ----------
    def active_id(self) -> str:
        try:
            value = json.loads(self.active_file.read_text(encoding="utf-8-sig"))
            return str(value.get("activeId") or "") if isinstance(value, dict) else ""
        except Exception:
            return ""

    def _write_active(self, preset_id: str) -> None:
        payload = {"activeId": preset_id, "updatedAt": datetime.now().isoformat(timespec="seconds")}
        tmp = self.active_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.active_file)

    def runtime_markdown(self) -> str:
        try:
            return self.runtime_file.read_text(encoding="utf-8-sig")
        except OSError:
            return ""

    # ---------- 备份 ----------
    def _backup(self, preset_id: str, markdown_text: str) -> str:
        # 毫秒级时间戳 + 冲突自增：同一秒内连续保存也要各自留一份备份
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        path = self.backups_dir / f"persona-{preset_id}-{stamp}.md"
        index = 2
        while path.exists():
            path = self.backups_dir / f"persona-{preset_id}-{stamp}-{index}.md"
            index += 1
        try:
            path.write_text(markdown_text, encoding="utf-8")
        except OSError:
            return ""
        self._prune_backups(preset_id)
        return str(path)

    def _prune_backups(self, preset_id: str, keep: int = BACKUP_KEEP) -> list[str]:
        paths = sorted(self.backups_dir.glob(f"persona-{preset_id}-*.md"), key=lambda item: item.stat().st_mtime, reverse=True)
        removed: list[str] = []
        for stale in paths[max(1, int(keep)):]:
            try:
                stale.unlink()
                removed.append(stale.name)
            except OSError:
                pass
        return removed

    def backups(self, preset_id: str = "") -> list[dict]:
        pattern = f"persona-{preset_id}-*.md" if preset_id else "persona-*.md"
        rows = []
        for path in sorted(self.backups_dir.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True):
            rows.append({"name": path.name, "path": str(path), "bytes": path.stat().st_size,
                         "modifiedAt": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")})
        return rows

    # ---------- 写入 ----------
    def _write_doc(self, preset_id: str, name: str, sections: list[dict], description: str = "") -> dict:
        warnings = validate_sections(sections)
        doc = {
            "id": preset_id,
            "name": str(name or preset_id).strip() or preset_id,
            "description": str(description or ""),
            "updatedAt": datetime.now().isoformat(timespec="seconds"),
            "sections": sections,
        }
        if preset_id in self.builtin_ids():
            doc["builtin"] = True
        path = self.presets_dir / f"{preset_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        doc["chars"] = document_chars(sections)
        doc["warnings"] = warnings
        return doc

    def save(self, preset_id: str, sections: list[dict], name: str = "") -> dict:
        """保存预设。若它正好是当前激活预设 → 同步重渲染运行时文件（内容一致，下一条消息生效）。"""
        existing = self._read_doc(preset_id)
        previous_md = render_markdown(existing.get("sections") or [])
        self._backup(preset_id, previous_md)
        doc = self._write_doc(preset_id, name or existing.get("name") or preset_id, sections,
                              description=str(existing.get("description") or ""))
        rendered = ""
        if self.active_id() == preset_id:
            rendered = self._write_runtime(render_markdown(sections))
        doc["runtimeUpdated"] = bool(rendered)
        return doc

    def create(self, name: str, sections: list[dict] | None = None, source_id: str = "") -> dict:
        taken = {path.stem for path in self.presets_dir.glob("*.json")}
        preset_id = slugify_id(name, taken)
        if source_id:
            sections = self.get(source_id).get("sections") or []
        if not sections:
            sections = [{"title": "人格与关系", "intro": "", "children": [{"title": "基础定位", "body": ""}]}]
        return self._write_doc(preset_id, name, sections)

    def rename(self, preset_id: str, name: str) -> dict:
        doc = self._read_doc(preset_id)
        return self._write_doc(preset_id, name, doc.get("sections") or [], description=str(doc.get("description") or ""))

    def delete(self, preset_id: str) -> dict:
        if preset_id in self.builtin_ids():
            raise PersonaError("内置预设不能删除（可以改内容，或“恢复默认模板”）。")
        if self.active_id() == preset_id:
            raise PersonaError("这是当前激活的人设，请先切换到其它预设再删除。")
        path = self.presets_dir / f"{preset_id}.json"
        if not path.is_file():
            raise PersonaError(f"预设不存在：{preset_id}")
        path.unlink()
        return {"id": preset_id, "deleted": True}

    def restore_builtin(self, preset_id: str) -> dict:
        source = self.templates_dir / f"{preset_id}.json"
        if not source.is_file():
            raise PersonaError(f"包内没有这个内置预设：{preset_id}")
        shutil.copy2(source, self.presets_dir / f"{preset_id}.json")
        doc = self._read_doc(preset_id)
        if self.active_id() == preset_id:
            self._write_runtime(render_markdown(doc.get("sections") or []))
        return doc

    def import_markdown(self, name: str, text: str, preset_id: str = "") -> dict:
        sections, warnings = parse_markdown(text)
        taken = {path.stem for path in self.presets_dir.glob("*.json")}
        target = preset_id.strip() or slugify_id(name, taken)
        if target in taken and preset_id:
            existing = self._read_doc(target)
            self._backup(target, render_markdown(existing.get("sections") or []))
        doc = self._write_doc(target, name or target, sections)
        doc["parseWarnings"] = warnings
        return doc

    def export_markdown(self, preset_id: str) -> dict:
        doc = self._read_doc(preset_id)
        markdown = render_markdown(doc.get("sections") or [])
        return {"id": preset_id, "name": doc.get("name") or preset_id, "markdown": markdown}

    def import_runtime_as_preset(self, name: str = "当前人设") -> dict:
        markdown = self.runtime_markdown()
        if not markdown.strip():
            raise PersonaError("运行时人设文件是空的，无法导入。")
        return self.import_markdown(name, markdown)

    def _write_runtime(self, markdown: str) -> str:
        """写运行时人设文件（唯一被模型读取的那份）；写前备份。"""
        if not str(markdown or "").strip():
            raise PersonaError("拒绝写入空人设。")
        previous = self.runtime_markdown()
        if previous.strip():
            self._backup("runtime", previous)
        self.runtime_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.runtime_file.with_suffix(".md.tmp")
        tmp.write_text(markdown, encoding="utf-8")
        tmp.replace(self.runtime_file)
        return str(self.runtime_file)

    def activate(self, preset_id: str) -> dict:
        """激活预设：渲染到运行时文件 + 记录 activeId（不触发 reread，那是看板的事）。"""
        doc = self._read_doc(preset_id)
        sections = doc.get("sections") or []
        warnings = validate_sections(sections)
        path = self._write_runtime(render_markdown(sections))
        self._write_active(preset_id)
        return {"id": preset_id, "name": doc.get("name") or preset_id, "runtimeFile": path, "warnings": warnings}
