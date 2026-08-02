import json
from pathlib import Path


class CapabilityRegistry:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.last_error = ""
        self.reload()

    @classmethod
    def from_sop_root(cls, sop_root: Path, compiled_path: Path):
        from ..memory.sop_catalog import SopCatalog

        SopCatalog(sop_root, compiled_path).compile_capabilities()
        return cls(compiled_path)

    def reload(self) -> None:
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        capabilities = doc.get("capabilities", [])
        required = {"id", "route", "sop", "allowedTools", "risk", "confirmation"}
        for item in capabilities:
            missing = sorted(required.difference(item))
            if missing:
                raise ValueError(f"capability {item.get('id', '<unknown>')} missing fields: {', '.join(missing)}")
            if item["route"] not in ("direct", "worker"):
                raise ValueError(f"capability {item['id']} has invalid route")
            if item["route"] == "worker" and item.get("lifecycle") not in ("ephemeral", "persistent"):
                raise ValueError(f"worker capability {item['id']} has invalid lifecycle")
        self.capabilities = {item["id"]: item for item in capabilities}
        self.last_error = ""

    def try_reload(self) -> dict:
        try:
            self.reload()
            return {"ok": True, "path": str(self.path), "count": len(self.capabilities), "error": ""}
        except Exception as error:
            self.last_error = str(error)
            return {"ok": False, "path": str(self.path), "count": len(getattr(self, "capabilities", {})), "error": self.last_error}

    def get(self, capability_id: str) -> dict | None:
        return self.capabilities.get(str(capability_id or "").strip())

    def require_route(self, capability_id: str, route: str) -> dict:
        item = self.get(capability_id)
        if not item:
            raise PermissionError(f"unknown capability: {capability_id}")
        if item.get("route") != route:
            raise PermissionError(f"capability {capability_id} must use route={item.get('route')}")
        return item

    def prompt_summary(self) -> str:
        lines = []
        for item in self.capabilities.values():
            route = "总管直办" if item["route"] == "direct" else "必须委派Worker"
            details = [route]
            if item.get("lifecycle"):
                details.append(f"生命周期={item['lifecycle']}")
            if item.get("workerType"):
                details.append(f"Worker={item['workerType']}")
            if item.get("confirmation") not in (None, "", "none"):
                details.append(f"确认={item['confirmation']}")
            lines.append(f"- `{item['id']}`｜{'；'.join(details)}｜{item.get('description', '')}")
        return "\n".join(lines)
