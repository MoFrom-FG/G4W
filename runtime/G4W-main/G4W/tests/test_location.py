import tempfile
import unittest
import urllib.request
import json
from pathlib import Path

from G4W.features.location import LocationService
from G4W.core.storage import EventStore


class LocationTests(unittest.TestCase):
    def test_major_move_emits_same_binding_event(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            events = EventStore(root / "events.json")
            service = LocationService(root / "locations.json", events, major_move_meters=500)
            service.record({"latitude": 34.3416, "longitude": 108.9398}, "account:sender", "sender")
            result = service.record({"latitude": 34.2510, "longitude": 108.9470}, "account:sender", "sender")
            self.assertIsNotNone(result["movement"])
            event = events.next_pending()
            self.assertEqual(event["type"], "location.changed")
            self.assertEqual(event["bindingKey"], "account:sender")

    def test_location_http_server_token_and_record(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            service = LocationService(root / "locations.json", EventStore(root / "events.json"))
            status = service.start_server("127.0.0.1", 0, "secret")
            url = f"http://127.0.0.1:{status['port']}/location"
            request = urllib.request.Request(url, data=json.dumps({"latitude": 34.3, "longitude": 108.9}).encode(), headers={"content-type": "application/json", "x-location-token": "secret"}, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    result = json.loads(response.read().decode())
                self.assertTrue(result["ok"])
                self.assertEqual(service.latest()["latitude"], 34.3)
            finally:
                service.close_server()


if __name__ == "__main__":
    unittest.main()
