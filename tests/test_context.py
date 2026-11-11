import json
import unittest

from src.context import ContextWindow, OBSERVATION_BYTES, bounded_observation, json_bytes


class ContextTest(unittest.TestCase):
    def test_compaction_keeps_latest_complete_provider_exchange(self):
        window = ContextWindow({"material_id": "M01", "confirmed_slot": "18:00"}, "", [], 2200)
        for call_id in ("first", "latest"):
            blocks = [
                {"type": "thinking", "thinking": "x" * 600, "signature": call_id},
                {"type": "tool_use", "id": call_id, "name": "query_slots", "input": {}},
            ]
            observation = [{"type": "tool_result", "tool_use_id": call_id, "content": "occupied"}]
            window.append(blocks, observation)

        checkpoint = {"audit_status": "passed", "slot_conflict": True}
        messages, sizes = window.prepare(checkpoint)
        anchor = json.loads(messages[0]["content"])
        self.assertEqual(anchor["runtime_checkpoint"], checkpoint)
        self.assertEqual(anchor["confirmed_slot"], "18:00")
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[1]["content"], blocks)
        self.assertEqual(messages[2]["content"], observation)
        self.assertEqual(sizes["exchanges_removed"], 1)
        self.assertLess(sizes["bytes_after"], sizes["bytes_before"])
        self.assertFalse(sizes["over_budget"])

    def test_observation_limit_preserves_write_status_and_receipt(self):
        response = {
            "status": "saved", "draft_id": "draft-1", "slot": "18:00",
            "details": "字" * 8000, "reason": "\x00" * 1200,
        }
        limited = bounded_observation(response)
        self.assertTrue(limited["details_omitted"])
        self.assertEqual(limited["status"], "saved")
        self.assertEqual(limited["draft_id"], "draft-1")
        self.assertEqual(limited["slot"], "18:00")
        self.assertLessEqual(json_bytes(limited), OBSERVATION_BYTES)
