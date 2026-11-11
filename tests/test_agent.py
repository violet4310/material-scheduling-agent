import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from src.agent import run
from src.context import OBSERVATION_BYTES, json_bytes
from src.domain import DraftStore, Request, Tools
from src.main import DATA, load_calendar, read_json
from src.models import Mock, ModelError


ROOT = DATA.parent
SLOT_18 = "2026-11-15T18:00+08:00"
SLOT_19 = "2026-11-15T19:00+08:00"
SLOT_20 = "2026-11-15T20:00+08:00"


class AgentTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "drafts.sqlite3"
        self.store = DraftStore(self.db)
        self.materials = read_json(DATA / "materials.json")
        self.rules = read_json(DATA / "rules.json")
        self.evidence = read_json(DATA / "evidence.json")
        self.calendar = load_calendar(DATA / "calendar.json")

    def tools(self, material="M01", confirmed=SLOT_18, failure=False, confirmation_id=None):
        return Tools(Request(material, SLOT_18, confirmed, confirmation_id), self.materials,
                     self.rules, self.calendar, self.store, failure, evidence=self.evidence)

    def rows(self):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute("SELECT draft_id, material_id, slot FROM drafts").fetchall()

    def tickets(self):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute(
                "SELECT confirmation_id, slot, draft_id FROM confirmations ORDER BY slot"
            ).fetchall()

    def test_normal_and_duplicate_from_cli(self):
        cmd = [
            sys.executable, "-m", "src.main", "--input", "examples/normal.input.json",
            "--mode", "mock", "--db", str(self.db),
        ]
        first, second = [
            json.loads(subprocess.check_output(cmd, cwd=ROOT, text=True))["results"][0]
            for _ in range(2)
        ]
        self.assertEqual(first["status"], "saved")
        self.assertEqual(second["status"], "reused")
        self.assertEqual(first["draft"]["draft_id"], second["draft"]["draft_id"])
        self.assertEqual(self.rows(), [(first["draft"]["draft_id"], "M01", SLOT_18)])

    def test_missing_metadata_and_rule_risk_are_distinct(self):
        missing = run(Mock(), self.tools(material="M02"))
        self.assertEqual(missing["status"], "insufficient_info")
        self.assertTrue(any("rights_cleared" in item for item in missing["pending_items"]))
        self.assertEqual(missing["trace"][-1]["action"], "ask_user")
        self.assertEqual(missing["clarification"]["questions"][0]["field"], "rights_cleared")
        self.assertEqual(missing["clarification"]["resume_option"], "--evidence")
        risk = run(Mock(), self.tools(material="M03"))
        self.assertEqual(risk["status"], "blocked")
        self.assertEqual(risk["risk_reasons"][0]["rule_id"], "R01")
        self.assertNotIn("clarification", risk)
        self.assertEqual(self.rows(), [])

    def test_unknown_material_requests_metadata(self):
        result = run(Mock(), self.tools(material="UNKNOWN"))
        self.assertEqual(result["status"], "insufficient_info")
        self.assertTrue(any("UNKNOWN" in item for item in result["pending_items"]))
        self.assertEqual(result["clarification"]["resume_option"], "--materials")
        self.assertEqual(self.rows(), [])

    def test_user_can_answer_clarification_with_evidence_on_new_cli_run(self):
        command = [sys.executable, "-m", "src.main", "--input", "examples/missing.input.json",
                   "--db", str(self.db)]
        initial = json.loads(subprocess.check_output(command, cwd=ROOT, text=True))["results"][0]
        self.assertEqual(initial["trace"][-1]["action"], "ask_user")
        self.assertEqual(self.rows(), [])
        resumed = json.loads(subprocess.check_output(
            [*command, "--evidence", "examples/clarification.evidence.json"], cwd=ROOT, text=True,
        ))["results"][0]
        self.assertEqual(resumed["status"], "saved")
        self.assertEqual(resumed["audit_evidence"], [{"field": "rights_cleared", "source_id": "E02"}])
        self.assertEqual([action["action"] for action in resumed["trace"][:4]],
                         ["read_material", "check_rules", "lookup_evidence", "check_rules"])
        self.assertEqual(len(self.rows()), 1)

    def test_asking_is_rejected_when_local_evidence_can_fill_gap(self):
        tools = self.tools(material="M05")
        tools.read_material()
        tools.check_rules()
        self.assertEqual(tools.ask_user(), {"status": "rejected", "reason": "lookup_evidence_available"})
        self.assertEqual(self.rows(), [])

    def test_conflicting_evidence_blocks_even_with_complete_metadata(self):
        self.evidence["M01"] = read_json(ROOT / "examples/conflicting.evidence.json")["M02"]
        tools = self.tools()
        tools.read_material()
        self.assertEqual(tools.check_rules()["status"], "evidence_conflict")
        self.assertEqual(tools.save_draft(SLOT_18)["status"], "rejected")
        result = run(Mock(), self.tools())
        self.assertEqual(result["status"], "evidence_conflict")
        self.assertEqual(result["trace"][-1]["action"], "ask_user")
        sources = result["clarification"]["questions"][0]["sources"]
        self.assertEqual({source["source_id"]: source["value"] for source in sources},
                         {"metadata:M01": True, "E02": True, "E02-REVIEW": False})
        self.assertEqual(self.rows(), [])

    def test_metadata_disagreeing_with_single_record_requires_review(self):
        for metadata_value, evidence_value in ((True, False), (False, True)):
            with self.subTest(metadata=metadata_value, evidence=evidence_value):
                self.materials["M01"]["rights_cleared"] = metadata_value
                self.evidence["M01"] = {
                    "rights_cleared": {"source_id": "E01", "value": evidence_value},
                }
                result = run(Mock(), self.tools())
                self.assertEqual(result["status"], "evidence_conflict")
                self.assertEqual(result["trace"][-1]["action"], "ask_user")
                sources = result["clarification"]["questions"][0]["sources"]
                self.assertEqual({source["source_id"]: source["value"] for source in sources},
                                 {"metadata:M01": metadata_value, "E01": evidence_value})
                self.assertEqual(self.rows(), [])

    def test_agreeing_evidence_can_fill_gap_and_retains_both_source_ids(self):
        self.evidence["M02"] = {"rights_cleared": [
            {"source_id": "E02", "value": True}, {"source_id": "E02-COPY", "value": True},
        ]}
        result = run(Mock(), self.tools(material="M02"))
        self.assertEqual(result["status"], "saved")
        self.assertEqual({source["source_id"] for source in result["audit_evidence"]},
                         {"E02", "E02-COPY"})
        self.assertEqual(len(self.rows()), 1)

    def test_conflict_needs_confirmation_then_saves_confirmed_slot(self):
        self.calendar = load_calendar(ROOT / "examples/conflict.calendar.json")
        result = run(Mock(), self.tools())
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertEqual(result["suggested_slot"], SLOT_19)
        self.assertEqual(result["draft"]["status"], "not_attempted")
        self.assertEqual(self.rows(), [])

        result = run(Mock(), self.tools(confirmed=SLOT_19))
        self.assertEqual(result["status"], "saved")
        self.assertEqual(self.rows()[0][2], SLOT_19)
        self.assertEqual(run(Mock(), self.tools(confirmed=SLOT_19))["status"], "reused")
        self.assertEqual(run(Mock(), self.tools())["status"], "draft_exists_at_different_slot")
        self.assertEqual(len(self.rows()), 1)

    def test_save_guard_rejects_unauthorized_slot_even_if_queried(self):
        tools = self.tools()
        tools.read_material()
        tools.check_rules()
        self.assertIn(SLOT_19, tools.query_slots("alternatives")["available"])
        self.assertEqual(tools.save_draft(SLOT_19)["reason"], "slot_not_authorized")
        self.assertEqual(self.rows(), [])

    def test_approval_does_not_authorize_another_fallback(self):
        self.calendar["occupied"] = [SLOT_18, SLOT_19]
        result = run(Mock(), self.tools(confirmed=SLOT_19))
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertEqual(result["suggested_slot"], SLOT_20)
        self.assertEqual(result["draft"]["status"], "not_attempted")
        self.assertEqual(self.rows(), [])

    def test_save_guard_rejects_risk_and_missing_slot_check(self):
        risky = self.tools(material="M03")
        risky.read_material()
        risky.check_rules()
        self.assertEqual(risky.save_draft(SLOT_18)["reason"], "passed_audit_required")
        clean = self.tools()
        clean.read_material()
        clean.check_rules()
        self.assertEqual(clean.save_draft(SLOT_18)["reason"], "available_slot_check_required")
        self.assertEqual(self.rows(), [])

    def test_failed_save_rolls_back_and_can_be_retried(self):
        result = run(Mock(), self.tools(failure=True))
        self.assertEqual(result["status"], "save_failed")
        self.assertEqual(result["audit_status"], "passed")
        self.assertEqual(result["draft"]["reason"], "injected_save_failure")
        self.assertEqual(self.rows(), [])
        self.assertEqual(run(Mock(), self.tools())["status"], "saved")

    def test_step_limit_preserves_draft_already_saved(self):
        result = run(Mock(), self.tools(), max_steps=4)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["incomplete_reason"], "step_limit")
        self.assertEqual(len(result["trace"]), 4)
        self.assertEqual(result["draft"]["status"], "saved")
        self.assertEqual(len(self.rows()), 1)

    def test_model_failure_stays_visible(self):
        class FailingModel:
            model = "fake-provider"

            def next_turn(self, system, messages, tools):
                raise ModelError("minimax_http_503")

        result = run(FailingModel(), self.tools())
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["incomplete_reason"], "minimax_http_503")
        self.assertEqual(self.rows(), [])

    def test_two_writers_cannot_reserve_same_slot(self):
        prepared = [self.tools(material=material) for material in ("M01", "M04")]
        for tools in prepared:
            tools.read_material()
            tools.check_rules()
            self.assertEqual(tools.query_slots("exact")["available"], [SLOT_18])
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda tools: tools.save_draft(SLOT_18), prepared))
        self.assertCountEqual([outcome["status"] for outcome in outcomes], ["saved", "slot_conflict"])
        self.assertEqual(len(self.rows()), 1)

    def test_two_writers_reuse_same_draft_for_same_request(self):
        prepared = [self.tools(), self.tools()]
        for tools in prepared:
            tools.read_material()
            tools.check_rules()
            self.assertEqual(tools.query_slots("exact")["available"], [SLOT_18])
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda tools: tools.save_draft(SLOT_18), prepared))
        self.assertCountEqual([outcome["status"] for outcome in outcomes], ["saved", "reused"])
        self.assertEqual(outcomes[0]["draft_id"], outcomes[1]["draft_id"])
        self.assertEqual(len(self.rows()), 1)

    def test_parallel_actions_from_model_cannot_trigger_a_write(self):
        class MultiActionMock(Mock):
            def next_turn(self, system, messages, tools):
                turn = super().next_turn(system, messages, tools)
                action = turn["content"][0]
                if action["name"] == "save_draft":
                    turn["content"].append({**action, "id": "second-save"})
                return turn

        result = run(MultiActionMock(), self.tools(), max_steps=4)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["draft"]["status"], "not_attempted")
        self.assertEqual([event["result"]["status"] for event in result["trace"][-2:]],
                         ["invalid_action", "invalid_action"])
        self.assertEqual(self.rows(), [])

    def test_large_metadata_and_compacted_history_still_require_confirmation(self):
        self.calendar = load_calendar(ROOT / "examples/conflict.calendar.json")
        self.materials["M01"]["transcript"] = "字" * 80_000
        self.materials["M01"]["title"] = "长" * 10_000
        result = run(Mock(), self.tools(), context_budget=5200)
        observation = result["trace"][0]["result"]
        self.assertNotIn("transcript", observation["metadata"])
        self.assertEqual(len(observation["metadata"]["title"]), 120)
        self.assertEqual(observation["metadata_fields_omitted"], 1)
        self.assertEqual(observation["metadata_fields_clipped"], 1)
        self.assertLessEqual(json_bytes(observation), OBSERVATION_BYTES)
        self.assertTrue(any(event["exchanges_removed"] for event in result["context"]["rounds"]))
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertEqual(result["suggested_slot"], SLOT_19)
        self.assertEqual(self.rows(), [])

    def test_alternative_preview_only_accepts_candidates_exposed_to_model(self):
        self.calendar["occupied"] = [SLOT_18]
        self.calendar["alternative_times"] = [f"19:{i:02d}" for i in range(12)]
        tools = self.tools()
        tools.read_material()
        tools.check_rules()
        tools.query_slots("exact")
        observation = tools.query_slots("alternatives")
        self.assertEqual(observation["available_count"], 12)
        self.assertEqual(len(observation["available"]), 8)
        self.assertEqual(tools.result(proposed_slot="2026-11-15T19:11+08:00")["status"],
                         "slot_conflict")
        self.assertEqual(tools.result(proposed_slot=SLOT_19)["status"], "needs_confirmation")

    def test_initial_context_over_budget_skips_model_call(self):
        class UnusedModel:
            model = "unused"

            def next_turn(self, system, messages, tools):
                raise AssertionError("oversized context reached model")

        result = run(UnusedModel(), self.tools(), context_budget=1024)
        self.assertEqual(result["incomplete_reason"], "context_budget_exceeded")
        self.assertEqual(result["trace"], [])
        self.assertTrue(result["context"]["rounds"][0]["over_budget"])
        self.assertEqual(self.rows(), [])

    def test_context_limit_preserves_already_saved_draft(self):
        class LargeReplyMock(Mock):
            def next_turn(self, system, messages, tools):
                turn = super().next_turn(system, messages, tools)
                if turn["content"][0]["name"] == "save_draft":
                    turn["content"].insert(0, {"type": "thinking", "thinking": "x" * 9000})
                return turn

        result = run(LargeReplyMock(), self.tools(), context_budget=6800)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["incomplete_reason"], "context_budget_exceeded")
        self.assertEqual(result["draft"]["status"], "saved")
        self.assertEqual(result["draft"]["draft_id"], self.rows()[0][0])
        self.assertNotIn("x" * 9000, json.dumps(result))

    def test_supplement_requires_reaudit_and_preserves_original_metadata(self):
        tools = self.tools(material="M05")
        tools.read_material()
        self.assertTrue(tools.check_rules()["supplement_available"])
        self.assertEqual(tools.lookup_evidence()["sources"],
                         [{"field": "rights_cleared", "source_id": "E05"}])
        self.assertEqual(tools.query_slots("exact")["status"], "prerequisite_missing")
        self.assertEqual(tools.check_rules()["status"], "passed")
        self.assertNotIn("rights_cleared", self.materials["M05"])

        result = run(Mock(), self.tools(material="M05"), context_budget=5200)
        self.assertEqual(result["status"], "saved")
        self.assertEqual([event["action"] for event in result["trace"][:4]],
                         ["read_material", "check_rules", "lookup_evidence", "check_rules"])
        self.assertEqual(result["audit_evidence"][0]["source_id"], "E05")

    def test_supplement_can_reveal_risk_and_cannot_override_known_risk(self):
        result = run(Mock(), self.tools(material="M06"))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["audit_evidence"][0]["source_id"], "E06")
        self.assertEqual(result["risk_reasons"][0]["rule_id"], "R01")
        self.evidence["M03"] = self.evidence["M05"]
        tools = self.tools(material="M03")
        tools.read_material()
        self.assertEqual(tools.check_rules()["status"], "evidence_conflict")
        self.assertEqual(tools.lookup_evidence()["status"], "no_supplement")
        self.assertEqual(self.rows(), [])

    def test_ticket_binds_material_and_slot_and_replay_reuses_draft(self):
        self.calendar = load_calendar(ROOT / "examples/conflict.calendar.json")
        offer = run(Mock(), self.tools())["confirmation"]
        ticket = offer["confirmation_id"]
        self.assertEqual(run(Mock(), self.tools())["confirmation"], offer)
        self.assertEqual(self.tickets(), [(ticket, SLOT_19, None)])
        self.assertEqual(self.rows(), [])

        self.store = DraftStore(self.db)
        for args in ({"material": "M04", "confirmed": SLOT_19}, {"confirmed": SLOT_20}):
            result = run(Mock(), self.tools(confirmation_id=ticket, **args))
            self.assertEqual(result["status"], "invalid_confirmation")
            self.assertIsNone(result["model"])
        self.assertEqual(self.rows(), [])

        result = run(Mock(), self.tools(confirmed=SLOT_19, confirmation_id=ticket))
        draft_id = result["draft"]["draft_id"]
        self.assertEqual(result["status"], "saved")
        self.assertEqual(self.tickets(), [(ticket, SLOT_19, draft_id)])
        replay = run(Mock(), self.tools(confirmed=SLOT_19, confirmation_id=ticket))
        self.assertEqual(replay["status"], "reused")
        self.assertEqual(replay["draft"]["draft_id"], draft_id)

    def test_ticket_rechecks_busy_slot_and_offers_new_confirmation(self):
        self.calendar = load_calendar(ROOT / "examples/conflict.calendar.json")
        first = run(Mock(), self.tools())["confirmation"]
        self.calendar["occupied"] = [SLOT_18, SLOT_19]
        next_result = run(Mock(), self.tools(confirmed=SLOT_19,
                                             confirmation_id=first["confirmation_id"]))
        self.assertEqual(next_result["status"], "needs_confirmation")
        self.assertEqual(next_result["suggested_slot"], SLOT_20)
        second = next_result["confirmation"]
        self.assertNotEqual(first["confirmation_id"], second["confirmation_id"])
        self.assertEqual(len(self.tickets()), 2)
        self.assertEqual(self.rows(), [])

    def test_save_failure_rolls_back_ticket_binding(self):
        self.calendar = load_calendar(ROOT / "examples/conflict.calendar.json")
        ticket = run(Mock(), self.tools())["confirmation"]["confirmation_id"]
        failure = run(Mock(), self.tools(confirmed=SLOT_19, confirmation_id=ticket, failure=True))
        self.assertEqual(failure["status"], "save_failed")
        self.assertEqual(self.tickets(), [(ticket, SLOT_19, None)])
        self.assertEqual(self.rows(), [])
        self.assertEqual(run(Mock(), self.tools(confirmed=SLOT_19, confirmation_id=ticket))["status"],
                         "saved")

    def test_concurrent_offers_share_one_confirmation(self):
        self.calendar = load_calendar(ROOT / "examples/conflict.calendar.json")
        sessions = [self.tools(), self.tools()]
        with ThreadPoolExecutor(max_workers=2) as pool:
            offers = list(pool.map(lambda tools: run(Mock(), tools)["confirmation"], sessions))
        self.assertEqual(offers[0], offers[1])
        self.assertEqual(len(self.tickets()), 1)
        self.assertEqual(self.rows(), [])

    def test_confirmation_does_not_bypass_changed_audit_result(self):
        self.calendar = load_calendar(ROOT / "examples/conflict.calendar.json")
        ticket = run(Mock(), self.tools())["confirmation"]["confirmation_id"]
        self.materials["M01"]["rights_cleared"] = False
        result = run(Mock(), self.tools(confirmed=SLOT_19, confirmation_id=ticket))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["risk_reasons"][0]["rule_id"], "R01")
        self.assertEqual(self.tickets(), [(ticket, SLOT_19, None)])
        self.assertEqual(self.rows(), [])

    def test_candidate_taken_after_query_does_not_create_offer(self):
        self.calendar = load_calendar(ROOT / "examples/conflict.calendar.json")
        tools = self.tools()
        tools.read_material()
        tools.check_rules()
        tools.query_slots("exact")
        self.assertEqual(tools.query_slots("alternatives")["available"], [SLOT_19])
        competing = Tools(Request("M04", SLOT_19, SLOT_19), self.materials, self.rules,
                          self.calendar, self.store)
        self.assertEqual(run(Mock(), competing)["status"], "saved")
        result = tools.finish(proposed_slot=SLOT_19)
        self.assertEqual(result["status"], "slot_conflict")
        self.assertNotIn("confirmation", result)
        self.assertEqual(self.tickets(), [])
        self.assertEqual(len(self.rows()), 1)


if __name__ == "__main__":
    unittest.main()
