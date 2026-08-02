import json
import math
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..core.storage import JsonStore


def haversine_meters(first: dict, second: dict) -> float:
    lat1, lon1 = math.radians(float(first["latitude"])), math.radians(float(first["longitude"]))
    lat2, lon2 = math.radians(float(second["latitude"])), math.radians(float(second["longitude"]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


class LocationService:
    def __init__(self, path: Path, event_store, history_limit: int = 1000, major_move_meters: int = 1000, known_places: list[dict] | None = None, known_place_radius: int = 150):
        self.store = JsonStore(path, {"version": 1, "latest": {}, "history": [], "movements": [], "batteryHistory": []})
        self.events = event_store
        self.history_limit = max(10, int(history_limit or 1000))
        self.major_move_meters = max(50, int(major_move_meters or 1000))
        self.known_places = known_places or []
        self.known_place_radius = max(10, int(known_place_radius or 150))
        self.server = None
        self.server_thread = None

    def record(self, payload: dict, binding_key: str = "", sender_id: str = "") -> dict:
        latitude = _number(payload.get("latitude", payload.get("lat")))
        longitude = _number(payload.get("longitude", payload.get("lng", payload.get("lon"))))
        if latitude is None or longitude is None or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError("valid latitude and longitude are required")
        point = {
            "id": str(payload.get("id") or f"loc-{uuid.uuid4().hex[:12]}"),
            "latitude": latitude,
            "longitude": longitude,
            "accuracy": _number(payload.get("accuracy")) or 0,
            "altitude": _number(payload.get("altitude")),
            "speed": _number(payload.get("speed")),
            "battery": _number(payload.get("battery", payload.get("batteryLevel"))),
            "charging": payload.get("charging"),
            "source": str(payload.get("source") or "G4W"),
            "senderId": str(sender_id or payload.get("senderId") or ""),
            "bindingKey": str(binding_key or payload.get("bindingKey") or ""),
            "recordedAt": float(payload.get("recordedAt") or payload.get("timestamp") or time.time()),
            "receivedAt": time.time(),
        }
        point["place"] = self._known_place(point)

        def update(state):
            previous = state.get("latest") or None
            movement = None
            if previous and "latitude" in previous:
                distance = haversine_meters(previous, point)
                point["distanceFromPreviousMeters"] = round(distance, 1)
                if distance >= self.major_move_meters:
                    movement = {
                        "id": f"move-{uuid.uuid4().hex[:12]}",
                        "from": {key: previous.get(key) for key in ("latitude", "longitude", "place", "recordedAt")},
                        "to": {key: point.get(key) for key in ("latitude", "longitude", "place", "recordedAt")},
                        "distanceMeters": round(distance, 1),
                        "createdAt": time.time(),
                    }
                    state.setdefault("movements", []).append(movement)
                    state["movements"] = state["movements"][-self.history_limit:]
            state["latest"] = point
            state.setdefault("history", []).append(point)
            state["history"] = state["history"][-self.history_limit:]
            if point.get("battery") is not None:
                state.setdefault("batteryHistory", []).append({"battery": point["battery"], "charging": point.get("charging"), "recordedAt": point["recordedAt"]})
                state["batteryHistory"] = state["batteryHistory"][-self.history_limit:]
            return movement

        movement = self.store.update(update)
        if movement and point.get("bindingKey"):
            self.events.enqueue("location.changed", point["bindingKey"], {"point": point, "movement": movement}, dedupe_key=f"location.changed:{movement['id']}")
        return {"ok": True, "point": point, "movement": movement}

    def latest(self) -> dict:
        return self.store.read().get("latest") or {}

    def history(self, limit: int = 20) -> list[dict]:
        return self.store.read().get("history", [])[-max(1, min(int(limit or 20), 200)):]

    def movements(self, limit: int = 20) -> list[dict]:
        return self.store.read().get("movements", [])[-max(1, min(int(limit or 20), 200)):]

    def start_server(self, host: str, port: int, token: str = "") -> dict:
        if self.server:
            return {"ok": True, "host": host, "port": self.server.server_port, "alreadyRunning": True}
        service = self
        expected_token = str(token or "")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.rstrip("/") == "/health":
                    return self._reply(200, {"ok": True})
                if self.path.rstrip("/") == "/location/latest":
                    return self._reply(200, {"ok": True, "point": service.latest()})
                return self._reply(404, {"ok": False, "error": "not found"})
            def do_POST(self):
                if self.path.rstrip("/") not in ("/location", "/locations"):
                    return self._reply(404, {"ok": False, "error": "not found"})
                if expected_token and self.headers.get("authorization", "").removeprefix("Bearer ").strip() != expected_token and self.headers.get("x-location-token", "") != expected_token:
                    return self._reply(401, {"ok": False, "error": "unauthorized"})
                try:
                    length = min(int(self.headers.get("content-length", "0") or 0), 1024 * 1024)
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    return self._reply(200, service.record(payload))
                except Exception as error:
                    return self._reply(400, {"ok": False, "error": str(error)})
            def _reply(self, status, value):
                data = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(status); self.send_header("content-type", "application/json; charset=utf-8"); self.send_header("content-length", str(len(data))); self.end_headers(); self.wfile.write(data)
            def log_message(self, format, *args):
                return

        self.server = ThreadingHTTPServer((host, int(port)), Handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="G4W-location-server")
        self.server_thread.start()
        return {"ok": True, "host": host, "port": self.server.server_port}

    def close_server(self):
        if self.server:
            self.server.shutdown(); self.server.server_close(); self.server = None

    def _known_place(self, point: dict) -> str:
        nearest = None
        for place in self.known_places:
            try:
                distance = haversine_meters(point, place)
            except Exception:
                continue
            radius = float(place.get("radiusMeters") or self.known_place_radius)
            if distance <= radius and (nearest is None or distance < nearest[0]):
                nearest = (distance, str(place.get("tag") or place.get("name") or "known"))
        return nearest[1] if nearest else ""


def _number(value):
    try:
        return float(value) if value is not None and value != "" else None
    except Exception:
        return None
