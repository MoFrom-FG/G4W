from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from pathlib import Path


INDEX_FILENAME = "global_mem_insight.txt"
FACTS_FILENAME = "global_mem.txt"
MANAGEMENT_FILENAME = "memory_management_sop.md"


class SopCatalog:
    """GA-style L1 index + L3 SOP files, independent from permissions.

    Ordinary SOP discovery never depends on sop.json or capability IDs.
    capabilities.json is compiled separately and remains only the deterministic
    permission/routing boundary.
    """

    def __init__(self, root: Path, compiled_registry: Path | None = None):
        self.root = Path(root).resolve()
        self.compiled_registry = Path(compiled_registry).resolve() if compiled_registry else None
        self.lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def index_path(self) -> Path:
        return self.root / INDEX_FILENAME

    @property
    def management_path(self) -> Path:
        return self.root / MANAGEMENT_FILENAME

    @property
    def template_root(self) -> Path:
        return Path(__file__).resolve().parents[1] / "templates" / "memory" / "sop"

    @staticmethod
    def _normal(value: str) -> str:
        return re.sub(r"[\\/]+", "/", str(value or "").strip()).strip("./").lower()

    @staticmethod
    def _natural_key(path: Path) -> str:
        stem = path.stem
        if stem.lower() == "sop":
            return path.parent.name
        return re.sub(r"(?:[_-]sop)$", "", stem, flags=re.I)

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def _candidate_paths(self, include_references: bool = False) -> list[Path]:
        result = []
        for path in sorted(self.root.rglob("*.md")):
            relative = self._relative(path)
            parts = Path(relative).parts
            name = path.name.lower()
            if name in (INDEX_FILENAME.lower(), "readme.md"):
                continue
            if "references" in parts and not include_references:
                continue
            if name == "sop.md" or re.search(r"(?:^|[_-])sop\.md$", name):
                result.append(path)
        return result

    def _parse_index(self) -> dict[str, dict]:
        # GA treats L1 as prompt text, not as a machine-readable registry.
        # G4W follows that rule: the raw L1 is injected into System and
        # agents use file_read/code_run to inspect SOP files when needed.
        return {}

    def _entry(self, path: Path, indexed: dict[str, dict]) -> dict:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        relative = self._relative(path)
        index = indexed.get(self._normal(relative), {})
        title_match = re.search(r"^#\s+(.+)$", text, flags=re.MULTILINE)
        description_match = re.search(
            r"^(?:摘要|用途|Purpose|Description)\s*[:：]\s*(.+)$",
            text, flags=re.MULTILINE | re.I,
        )
        key = str(index.get("key") or self._natural_key(path)).strip()
        aliases = list(index.get("aliases") or [])
        automatic_aliases = [
            path.stem,
            self._natural_key(path),
            relative,
            str(Path(relative).with_suffix("")),
            relative.replace("/", ".").removesuffix(".SOP").removesuffix(".sop"),
        ]
        if path.name.lower() == "sop.md":
            automatic_aliases.extend((path.parent.name, str(Path(relative).parent).replace("/", ".")))
        deduplicated = []
        seen = {self._normal(key)}
        for alias in [*aliases, *automatic_aliases]:
            value = str(alias or "").strip()
            normalized = self._normal(value)
            if value and normalized not in seen:
                seen.add(normalized)
                deduplicated.append(value)
        parts = Path(relative).parts
        # `sop-user/` is the user-custom SOP directory at `memory/sop-user/`,
        # parallel to `memory/sop/` (G4W native SOPs).  At runtime its
        # SOPs have the same discovery and execution semantics as native L3 files.
        visibility = "reference" if "references" in parts else "shared"
        return {
            "id": key,  # compatibility field; this is now an L1 index key, not a permission ID
            "key": key,
            "title": title_match.group(1).strip() if title_match else key,
            "description": str(index.get("description") or (description_match.group(1).strip() if description_match else "")),
            "aliases": deduplicated,
            "roles": ["conductor", "worker"],
            "tags": aliases,
            "visibility": visibility,
            "provider": "",
            "path": str(path),
            "relativePath": relative,
            "indexed": bool(index),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }

    def entries(self, role: str = "", include_references: bool = False) -> list[dict]:
        # Ordinary G4W SOPs are shared by Conductor and Worker. `role`
        # remains accepted for API compatibility but no longer hides files.
        with self.lock:
            indexed = self._parse_index()
            result = [self._entry(path, indexed) for path in self._candidate_paths(include_references)]
        return sorted(result, key=lambda item: (item["key"].lower(), item["relativePath"].lower()))

    @staticmethod
    def _public_item(item: dict) -> dict:
        keys = ("id", "key", "title", "description", "aliases", "roles", "tags", "visibility", "provider", "path", "relativePath", "indexed")
        return {key: item[key] for key in keys}

    def index_text(self, role: str = "") -> str:
        try:
            text = self.index_path.read_text(encoding="utf-8-sig", errors="replace").strip()
        except Exception:
            text = ""
        return text or self.render_index()

    def render_index(self) -> str:
        items = self.entries()
        lines = [
            "# [G4W SOP Insight]",
            "需要时用GA file_read读L3；不确定时用code_run搜索共享MemoryRoot",
        ]
        for item in items:
            aliases = [alias for alias in item.get("aliases", []) if len(alias) <= 20][:3]
            alias_text = f"（{'、'.join(aliases)}）" if aliases else ""
            lines.append(f"- `{item['key']}` → `{item['relativePath']}`{alias_text}")
        lines.extend([
            "",
            "[RULES]",
            "1. 普通SOP不是Capability；权限路由只看Capability Registry",
            "2. 读SOP禁凭印象；引用不确定先搜索文件；新SOP按任务领域分类落盘",
        ])
        return "\n".join(lines).rstrip()

    def ensure_index(self) -> Path:
        if not self.index_path.exists():
            template = self.template_root / INDEX_FILENAME
            if template.is_file():
                self.index_path.write_text(template.read_text(encoding="utf-8-sig"), encoding="utf-8")
            else:
                self.index_path.write_text(self.render_index() + "\n", encoding="utf-8")
        return self.index_path

    def ensure_facts(self) -> Path:
        path = self.root / FACTS_FILENAME
        if not path.exists():
            template = self.template_root / FACTS_FILENAME
            content = template.read_text(encoding="utf-8-sig") if template.is_file() else "# [Global Memory - L2]\n"
            path.write_text(content, encoding="utf-8")
        return path

    def unindexed(self, relative_paths: list[str] | set[str]) -> list[str]:
        # Kept only for compatibility with older callers. L1 is not parsed as
        # a structured directory, so runtime validation must not depend on it.
        return []

    def export_public(self, destination: Path) -> dict:
        """Stage the publishable SOP tree without user/local knowledge (sop-user).

        The source tree is never changed.  A release builder must provide an
        empty destination, which prevents a stale file from surviving
        an incremental copy.
        """
        target = Path(destination).resolve()
        if target == self.root or self.root in target.parents:
            raise ValueError("public SOP destination must be outside the source SOP tree")
        if target.exists() and any(target.iterdir()):
            raise ValueError("public SOP destination must be empty")
        target.mkdir(parents=True, exist_ok=True)
        copied = []
        for source in sorted(self.root.rglob("*")):
            relative = source.relative_to(self.root)
            if "demo" in relative.parts or "__pycache__" in relative.parts:
                continue
            output = target / relative
            if source.is_dir():
                output.mkdir(parents=True, exist_ok=True)
                continue
            if source.suffix.lower() in (".pyc", ".pyo"):
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, output)
            copied.append(relative.as_posix())
        for filename in (INDEX_FILENAME, FACTS_FILENAME):
            template = self.template_root / filename
            if template.is_file():
                shutil.copy2(template, target / filename)
        public_index_path = target / INDEX_FILENAME
        if not public_index_path.exists():
            public_index_path.write_text(SopCatalog(target).render_index() + "\n", encoding="utf-8")
        return {
            "ok": True,
            "source": str(self.root),
            "destination": str(target),
            "files": copied,
            "indexPath": str(public_index_path),
            "privateExcluded": True,
            "demoExcluded": True,
        }

    def list(self, role: str = "") -> dict:
        items = self.entries(role=role)
        return {
            "root": str(self.root),
            "indexPath": str(self.ensure_index()),
            "index": self.index_text(role=role),
            "count": len(items),
            "items": [self._public_item(item) for item in items],
        }

    @staticmethod
    def _query_terms(query: str) -> list[str]:
        value = str(query or "").strip().lower()
        terms = [item for item in re.findall(r"[a-z0-9_.-]+|[\u3400-\u9fff]+", value) if item]
        return list(dict.fromkeys([value, *terms])) if value else []

    def search(self, query: str, role: str = "", limit: int = 10) -> dict:
        raw = str(query or "").strip()
        terms = self._query_terms(raw)
        scored = []
        for item in self.entries(role=role):
            text = Path(item["path"]).read_text(encoding="utf-8-sig", errors="replace")[:30000].lower()
            key = item["key"].lower()
            aliases = "\n".join(item.get("aliases", [])).lower()
            title = item["title"].lower()
            relative = item["relativePath"].lower()
            description = item.get("description", "").lower()
            score = 0
            reasons = []
            for term in terms:
                if not term:
                    continue
                if term == key:
                    score += 30; reasons.append("index-key")
                elif term in key:
                    score += 12; reasons.append("key")
                if term in aliases:
                    score += 16; reasons.append("alias")
                if term in title:
                    score += 10; reasons.append("title")
                if term in relative:
                    score += 7; reasons.append("path")
                if term in description:
                    score += 5; reasons.append("description")
                if term in text:
                    score += 2; reasons.append("content")
            if score or not terms:
                scored.append((score, item, list(dict.fromkeys(reasons))))
        scored.sort(key=lambda value: (-value[0], value[1]["key"].lower(), value[1]["relativePath"].lower()))
        selected = scored[: max(1, min(50, int(limit or 10)))]
        return {
            "ok": True,
            "query": raw,
            "count": len(selected),
            "items": [{**self._public_item(item), "score": score, "matchReasons": reasons} for score, item, reasons in selected],
        }

    def _resolve(self, reference: str, role: str = "") -> tuple[dict | None, list[dict]]:
        value = str(reference or "").strip()
        normalized = self._normal(value)
        entries = self.entries(role=role, include_references=True)
        exact = []
        for item in entries:
            names = [item["key"], item["relativePath"], item["path"], *item.get("aliases", [])]
            if normalized and normalized in {self._normal(name) for name in names}:
                exact.append(item)
        if len(exact) == 1:
            return exact[0], []
        if len(exact) > 1:
            exact.sort(key=lambda item: (not item.get("indexed"), len(item["relativePath"])))
            return exact[0], [self._public_item(item) for item in exact[1:6]]
        searched = self.search(value, role=role, limit=5)["items"] if value else []
        if searched and (searched[0]["score"] >= 12 or len(searched) == 1):
            return next((item for item in entries if item["path"] == searched[0]["path"]), None), searched[1:]
        return None, searched

    def read(self, sop_id: str, section: str = "", role: str = "", max_chars: int = 30000) -> dict:
        reference = str(sop_id or "").strip()
        item, alternatives = self._resolve(reference, role=role)
        if not item:
            return {
                "ok": False,
                "status": "not_found",
                "reference": reference,
                "message": "没有找到唯一匹配的G4W SOP；请从候选中选择索引名或路径后继续。",
                "candidates": alternatives,
                "indexPath": str(self.ensure_index()),
            }
        text = Path(item["path"]).read_text(encoding="utf-8-sig", errors="replace")
        if section:
            pattern = re.compile(
                rf"^(?P<marks>#+)\s+.*{re.escape(section)}.*$\n(?P<body>[\s\S]*?)(?=^\1\s+|\Z)",
                flags=re.MULTILINE | re.I,
            )
            match = pattern.search(text)
            if match:
                text = match.group(0)
        clipped = len(text) > max(1000, int(max_chars))
        if clipped:
            text = text[: max(1000, int(max_chars))].rstrip() + "\n\n<!-- truncated -->\n"
        return {"ok": True, **item, "content": text, "truncated": clipped, "alternatives": alternatives}

    def compile_capabilities(self) -> dict:
        """Compile only deterministic permission/routing manifests."""
        capabilities = []
        sources = []
        with self.lock:
            for manifest in sorted(self.root.rglob("capabilities.json")):
                document = json.loads(manifest.read_text(encoding="utf-8"))
                values = document.get("capabilities", document if isinstance(document, list) else [])
                if not isinstance(values, list):
                    raise ValueError(f"capability manifest must contain a list: {manifest}")
                for value in values:
                    item = dict(value)
                    item.setdefault("sop", str(manifest.parent))
                    capabilities.append(item)
                sources.append(str(manifest))
            ids = [str(item.get("id") or "") for item in capabilities]
            duplicates = sorted({value for value in ids if value and ids.count(value) > 1})
            if duplicates:
                raise ValueError("duplicate capability ids: " + ", ".join(duplicates))
            document = {
                "version": 3,
                "generatedAt": time.time(),
                "sourceRoot": str(self.root),
                "purpose": "permission-routing-only",
                "sources": sources,
                "capabilities": capabilities,
            }
            if self.compiled_registry:
                self.compiled_registry.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.compiled_registry.with_suffix(self.compiled_registry.suffix + ".tmp")
                temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temporary.replace(self.compiled_registry)
            return document
