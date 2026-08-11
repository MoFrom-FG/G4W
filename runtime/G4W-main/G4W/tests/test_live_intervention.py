import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from ga import GenericAgentHandler

from G4W.agents.controller import ConductorSession
from G4W.agents.handlers import ConductorHandler
from G4W.core.config import Config
from G4W.core.service import G4WService
from G4W.core.storage import EventStore, OutboxStore
from G4W.memory.conversation import ConversationStore
from G4W.memory.l4_safe import parse_user_messages


def user_event(store: EventStore, message_id: str, received_at: str, text: str):
    return store.enqueue(
        "wechat.user_message",
        "account:sender",
        {
            "messageId": message_id,
            "receivedAt": received_at,
            "userText": text,
            "text": text,
            "attachments": [],
        },
    )


class LiveInterventionTests(unittest.TestCase):
    def make_session(self, root: Path, events: EventStore):
        session = ConductorSession.__new__(ConductorSession)
        session.controller = types.SimpleNamespace(events=events)
        session.sender_id = "sender"
        session.history_file = root / "history.json"
        session.runtime_dir = root / "runtime"
        session.state_lock = threading.RLock()
        session.active_round_id = "round-live"
        session.active_delivery_kind = "plain_reply"
        session.active_binding = {"bindingKey": "account:sender", "senderId": "sender"}
        session.intervention_lock = threading.RLock()
        session.pending_interventions = []
        session.awaiting_intervention_acks = []
        session.intervention_closing = False
        session.intervene_path = root / "runtime" / "control" / "_intervene"
        session.active_output_path = root / "round" / "output.txt"
        session.active_output_path.parent.mkdir(parents=True)
        session.active_output_path.write_text("", encoding="utf-8")
        session.agent = types.SimpleNamespace(
            is_running=True,
            G4W_input_turn=3,
            G4W_input_context={"roundId": "round-live"},
            handler=types.SimpleNamespace(current_turn=2),
        )
        return session

    def test_interventions_are_sorted_and_consumed_by_next_turn(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            events = EventStore(root / "events.json")
            session = self.make_session(root, events)
            later = user_event(events, "m2", "2026-07-18T12:00:02+08:00", "第二条")
            earlier = user_event(events, "m1", "2026-07-18T12:00:01+08:00", "第一条")

            self.assertTrue(session.try_intervene(later)["accepted"])
            self.assertTrue(session.try_intervene(earlier)["accepted"])
            content = session.intervene_path.read_text(encoding="utf-8")
            self.assertLess(content.index("第一条"), content.index("第二条"))
            self.assertTrue(all(item["status"] == "intervening" for item in events.store.read()["events"]))

            session.intervene_path.unlink()
            session._on_intervention_turn_end({
                "turn": 3,
                "exit_reason": {},
                "self": types.SimpleNamespace(max_turns=100),
            })

            state = events.store.read()["events"]
            self.assertTrue(all(item["status"] == "intervening" for item in state))
            self.assertEqual(session._ack_interventions(4), 2)
            state = events.store.read()["events"]
            self.assertTrue(all(item["status"] == "done" for item in state))
            audit = (session.active_output_path.parent / "interventions.jsonl").read_text(encoding="utf-8")
            self.assertIn('"outcome": "consumed"', audit)
            self.assertIn('"deliveredToTurn": 4', audit)

    def test_exit_boundary_requeues_for_a_new_round(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            events = EventStore(root / "events.json")
            session = self.make_session(root, events)
            event = user_event(events, "m1", "2026-07-18T12:00:01+08:00", "改一下方向")
            self.assertTrue(session.try_intervene(event)["accepted"])

            session.intervene_path.unlink()
            session._on_intervention_turn_end({
                "turn": 3,
                "exit_reason": {"result": "CURRENT_TASK_DONE"},
                "self": types.SimpleNamespace(max_turns=100),
            })

            stored = events.store.read()["events"][0]
            self.assertEqual(stored["status"], "pending")
            self.assertEqual(stored["interventionOutcome"], "requeued")
            self.assertTrue(session.intervention_closing)

    def test_restart_recovers_unfinished_intervention_claim(self):
        with tempfile.TemporaryDirectory() as td:
            events = EventStore(Path(td) / "events.json")
            event = user_event(events, "m1", "2026-07-18T12:00:01+08:00", "别忘了我")
            self.assertIsNotNone(events.claim_intervention(event["id"], "round-live", 4))
            self.assertEqual(events.recover_intervening(), 1)
            self.assertEqual(events.store.read()["events"][0]["status"], "pending")

    def test_handler_promotes_control_file_without_setting_ga_task_dir(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "_intervene"
            path.write_text("新的用户方向", encoding="utf-8")
            parent = types.SimpleNamespace(
                G4W_intervention_lock=threading.RLock(),
                G4W_intervene_path=str(path),
                intervene=None,
            )
            handler = ConductorHandler.__new__(ConductorHandler)
            handler.parent = parent
            captured = []

            def upstream(_self, response, tool_calls, tool_results, turn, next_prompt, exit_reason):
                captured.append(_self.parent.intervene)
                return next_prompt

            with patch.object(GenericAgentHandler, "turn_end_callback", upstream):
                handler.turn_end_callback(None, [], [], 3, "继续", {})

            self.assertEqual(captured, ["新的用户方向"])
            self.assertFalse(path.exists())
            self.assertFalse(hasattr(parent, "task_dir"))

    def test_user_intervention_metadata_is_hidden_but_user_only_keeps_text(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = ConversationStore(root / "conversations", root / "memory")
            first = store.append(
                "sender",
                "User",
                "这是中途补充",
                timestamp="2026-07-18T12:00:01+08:00",
                subtype="user-intervention",
                message_id="message-1",
                parent_round_id="round-live",
            )
            duplicate = store.append(
                "sender",
                "User",
                "这是中途补充",
                timestamp="2026-07-18T12:00:01+08:00",
                subtype="user-intervention",
                message_id="message-1",
                parent_round_id="round-live",
            )
            self.assertTrue(first)
            self.assertFalse(duplicate)
            transcript = store.transcript_path("sender").read_text(encoding="utf-8")
            self.assertEqual(transcript.count("User:"), 1)
            clean = store._clean_history_groups("sender")[0]["messages"][0]["content"][0]["text"]
            self.assertIn("这是中途补充", clean)
            self.assertNotIn("G4W:", clean)
            daily = store.daily_transcript_path("sender", store._local_stamp("2026-07-18T12:00:01+08:00"))
            messages = parse_user_messages(daily, root)
            self.assertEqual([item.body for item in messages], ["这是中途补充"])

    def test_outbox_preserves_active_round_and_defers_older_rounds(self):
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore(Path(td) / "outbox.json")
            active = store.prepare(
                "old:sender", "sender", "old-token", "当前Turn回复", "active",
                round_id="round-live", round_final=False,
            )
            old = store.prepare(
                "old:sender", "sender", "old-token", "更早回复", "old",
                round_id="round-old",
            )
            self.assertEqual(store.retarget_round("sender", "round-live", "new:sender", "new-token"), 1)
            deferred = store.defer_pending_for_sender("sender", exclude_round_id="round-live")
            self.assertEqual([item["id"] for item in deferred], [old["id"]])
            state = {item["id"]: item for item in store.store.read()["messages"]}
            self.assertEqual(state[active["id"]]["status"], "pending")
            self.assertEqual(state[active["id"]]["bindingKey"], "new:sender")
            self.assertEqual(state[active["id"]]["contextToken"], "new-token")

    def test_wechat_poll_path_claims_live_user_message_without_deferring_current_round(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            class Channel:
                pass

            service = G4WService(
                Config(state_dir=root / "state", workspace_root=root),
                channel=Channel(),
            )
            service.conversations.bind("account", "sender", "old-token")

            class LiveSession:
                def __init__(self):
                    self.state_lock = threading.RLock()
                    self.active_round_id = "round-live"
                    self.active_delivery_kind = "plain_reply"
                    self.active_binding = {
                        "bindingKey": "account:sender",
                        "accountId": "account",
                        "senderId": "sender",
                        "contextToken": "old-token",
                    }
                    self.last_delivery_kind = "plain_reply"
                    self.last_user_intervened = False

                def try_intervene(self, event, binding=None):
                    claimed = service.events.claim_intervention(event["id"], "round-live", 4)
                    if not claimed:
                        return {"accepted": False}
                    self.active_binding = dict(binding or self.active_binding)
                    return {"accepted": True, "roundId": "round-live", "targetTurn": 4}

            service.controller.sessions["sender"] = LiveSession()
            active = service.outbox.prepare(
                "account:sender",
                "sender",
                "old-token",
                "Turn 3已经形成的回复",
                "active-round-message",
                round_id="round-live",
                round_final=False,
            )

            service._enqueue_inbound({
                "accountId": "account",
                "senderId": "sender",
                "contextToken": "new-token",
                "messageId": "message-live",
                "text": "请改成新的方向",
                "receivedAt": "2026-07-18T12:00:01+08:00",
                "savedAttachments": [],
                "attachmentFailures": [],
            })

            event = next(item for item in service.events.store.read()["events"] if item.get("payload", {}).get("messageId") == "message-live")
            self.assertEqual(event["status"], "intervening")
            self.assertTrue(event["payload"]["interventionAccepted"])
            queued = next(item for item in service.outbox.store.read()["messages"] if item["id"] == active["id"])
            self.assertEqual(queued["status"], "pending")
            self.assertEqual(queued["contextToken"], "new-token")
            transcript = service.conversations.transcript_path("sender").read_text(encoding="utf-8")
            self.assertEqual(transcript.count("请改成新的方向"), 1)


if __name__ == "__main__":
    unittest.main()
