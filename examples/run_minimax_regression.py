"""Run the acceptance matrix with synthetic inputs and temporary databases."""

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic


ROOT = Path(__file__).resolve().parents[1]
SLOT_18 = "2026-11-15T18:00+08:00"
SLOT_19 = "2026-11-15T19:00+08:00"
SLOT_20 = "2026-11-15T20:00+08:00"
NORMAL = [{"material_id": "M01", "requested_slot": SLOT_18}]
MISSING = [{"material_id": "M02", "requested_slot": SLOT_18}]


def expected(status, action, slot=None, evidence=None):
    return {"status": status, "action": action, "slot": slot, "evidence": evidence}


def matrix():
    saved = expected("saved", "save_draft", SLOT_18)
    conflict = expected("needs_confirmation", "alternatives", SLOT_19)
    confirmed = [{"material_id": "M01", "requested_slot": SLOT_18, "confirmed_slot": SLOT_19}]
    batch = NORMAL + [{"material_id": "M04", "requested_slot": SLOT_18}]
    return [
        *[(f"normal-{i}", NORMAL, [], [], [saved], [SLOT_18]) for i in range(1, 4)],
        *[(f"conflict-{i}", NORMAL, [SLOT_18, SLOT_20], [], [conflict], [])
          for i in range(1, 4)],
        ("shift-candidate", NORMAL, [SLOT_18, SLOT_19], [],
         [expected("needs_confirmation", "alternatives", SLOT_20)], []),
        ("no-candidate", NORMAL, [SLOT_18, SLOT_19, SLOT_20], [],
         [expected("slot_conflict", "alternatives")], []),
        ("missing-and-answered", MISSING, [], [],
         [expected("insufficient_info", "ask_user", evidence="rights_cleared"),
          expected("saved", "lookup_evidence", SLOT_18, "E02")], [SLOT_18]),
        ("evidence-conflict-and-resolved", MISSING, [],
         ["--evidence", "examples/conflicting.evidence.json"],
         [expected("evidence_conflict", "ask_user", evidence="rights_cleared"),
          expected("saved", "lookup_evidence", SLOT_18, "E02")], [SLOT_18]),
        ("metadata-evidence-conflict", NORMAL, [],
         ["--evidence", "examples/metadata-conflict.evidence.json"],
         [expected("evidence_conflict", "ask_user", evidence="rights_cleared")], []),
        ("rule-risk", [{"material_id": "M03", "requested_slot": SLOT_18}], [], [],
         [expected("blocked", "check_rules", evidence="R01")], []),
        ("supplement-clears-risk", [{"material_id": "M05", "requested_slot": SLOT_18}],
         [], ["--context-budget-bytes", "7500"],
         [expected("saved", "lookup_evidence", SLOT_18, "E05")], [SLOT_18]),
        ("supplement-reveals-risk", [{"material_id": "M06", "requested_slot": SLOT_18}],
         [], [], [expected("blocked", "lookup_evidence", evidence="E06")], []),
        ("confirmation-ticket", NORMAL, [SLOT_18, SLOT_20], [],
         [conflict, expected("invalid_confirmation", "validate_confirmation"),
          expected("saved", "save_draft", SLOT_19),
          expected("reused", "reuse_existing_draft", SLOT_19)], [SLOT_19]),
        ("confirmed-and-replayed", confirmed, [SLOT_18, SLOT_20], [],
         [expected("saved", "save_draft", SLOT_19),
          expected("reused", "reuse_existing_draft", SLOT_19)], [SLOT_19]),
        ("save-failure", NORMAL, [], ["--inject-save-failure"],
         [expected("save_failed", "save_draft", evidence="injected_save_failure")], []),
        ("batch-slot-contention", batch, [SLOT_20], [],
         [saved, conflict], [SLOT_18]),
        ("step-limit", NORMAL, [], ["--max-steps", "2"],
         [expected("incomplete", "check_rules", evidence="step_limit")], []),
    ]


def run_case(case, mode):
    name, items, occupied, flags, expectations, expected_slots = case
    calendar = {"occupied": occupied, "alternative_times": ["19:00", "20:00"]}
    with TemporaryDirectory() as temp:
        directory = Path(temp)
        input_file = directory / "input.json"
        (directory / "calendar.json").write_text(json.dumps(calendar), encoding="utf-8")
        database = directory / "drafts.sqlite3"
        command = [sys.executable, "-m", "src.main", "--mode", mode,
                   "--input", str(directory / "input.json"),
                   "--calendar", str(directory / "calendar.json"),
                   "--db", str(database)]
        started = monotonic()
        outputs, errors, phases = [], [], []

        def invoke(phase_items, options=flags):
            input_file.write_text(json.dumps(phase_items), encoding="utf-8")
            result = subprocess.run(command + options, cwd=ROOT, capture_output=True, text=True,
                                    timeout=260, check=True)
            response = json.loads(result.stdout)["results"]
            outputs.extend(response)
            with closing(sqlite3.connect(database)) as db:
                drafts = db.execute("SELECT count(*) FROM drafts").fetchone()[0]
            phases.append({"input": phase_items, "cli_flags": options, "draft_count": drafts})
            return response

        try:
            initial = invoke(items)
            if name == "confirmed-and-replayed":
                invoke(items)
            elif name in {"missing-and-answered", "evidence-conflict-and-resolved"}:
                invoke(items, ["--evidence", "examples/clarification.evidence.json"])
            elif name == "confirmation-ticket":
                confirmation = initial[0].get("confirmation", {})
                if confirmation.get("status") != "pending" or not confirmation.get("confirmation_id"):
                    errors.append("missing pending confirmation")
                else:
                    approved = {**items[0], "confirmation_id": confirmation["confirmation_id"],
                                "confirmed_slot": SLOT_19}
                    invoke([{**approved, "confirmed_slot": SLOT_20}])
                    invoke([approved])
                    invoke([approved])
        except (subprocess.SubprocessError, ValueError, sqlite3.Error) as exc:
            errors.append(type(exc).__name__)
        elapsed = round(monotonic() - started, 2)
        rows, tickets = [], []
        if database.exists():
            with closing(sqlite3.connect(database)) as db:
                db.row_factory = sqlite3.Row
                rows = [dict(row) for row in db.execute(
                    "SELECT material_id, requested_slot, slot, draft_id FROM drafts ORDER BY slot"
                )]
                tickets = [dict(row) for row in db.execute(
                    "SELECT confirmation_id, material_id, requested_slot, slot, draft_id FROM confirmations"
                )]

    if [row["slot"] for row in rows] != expected_slots:
        errors.append("database slots differ")
    if len(outputs) != len(expectations):
        errors.append("result count differs")
    for item, (output, target) in enumerate(zip(outputs, expectations), start=1):
        errors.extend(f"item {item}: {error}" for error in compare(output, target))
    if name in {"confirmed-and-replayed", "confirmation-ticket"} and len(outputs) == len(expectations):
        first_id, second_id = [result["draft"].get("draft_id") for result in outputs[-2:]]
        if not rows or first_id != second_id or first_id != rows[0]["draft_id"]:
            errors.append("draft identity differs")
    if name == "confirmation-ticket":
        if [phase["draft_count"] for phase in phases] != [0, 0, 1, 1]:
            errors.append("unexpected draft count during confirmation")
        if (len(tickets) != 1 or tickets[0]["slot"] != SLOT_19
                or not rows or tickets[0]["draft_id"] != rows[0]["draft_id"]):
            errors.append("confirmation was not persisted exactly once")
    if name in {"missing-and-answered", "evidence-conflict-and-resolved"}:
        if [phase["draft_count"] for phase in phases] != [0, 1]:
            errors.append("unexpected draft count before and after operator follow-up")
        question = outputs[0].get("clarification", {}) if outputs else {}
        questions = question.get("questions", [])
        if not questions or questions[0]["field"] != "rights_cleared":
            errors.append("missing copyright clarification")
        elif name == "evidence-conflict-and-resolved":
            sources = {source["source_id"]: source["value"] for source in questions[0].get("sources", [])}
            if sources != {"E02": True, "E02-REVIEW": False}:
                errors.append("clarification does not identify both conflicting sources")
    return {
        "case": name, "input": items, "calendar": calendar, "cli_flags": flags,
        "passed": not errors, "failures": errors, "elapsed_seconds": elapsed,
        "expected": expectations, "expected_database_slots": expected_slots,
        "actual": outputs, "database_rows": rows, "confirmation_rows": tickets, "phases": phases,
    }


def compare(result, expected_result):
    failures = []
    for field, target in (("status", expected_result["status"]),
                          ("suggested_slot", expected_result["slot"])):
        if result[field] != target:
            failures.append(f"{field} was {result[field]!r}, expected {target!r}")

    trace = result["trace"]
    exact = next((event for event in trace if event["arguments"].get("mode") == "exact"), None)
    action = expected_result["action"]
    if action == "alternatives":
        if not exact or exact["next_action"] != "query_slots" or not any(
            event["arguments"].get("mode") == "alternatives" for event in trace
        ):
            failures.append("missing exact-to-alternatives decision")
    elif action == "save_draft":
        if not exact or exact["next_action"] != "save_draft":
            failures.append("missing exact-to-save decision")
    elif action == "lookup_evidence":
        stages = iter(trace)
        missing = next((event for event in stages if event["action"] == "check_rules"
                        and event["result"]["status"] == "insufficient_info"
                        and event["result"].get("supplement_available")), None)
        supplemented = next((event for event in stages if event["action"] == "lookup_evidence"
                             and event["result"]["status"] == "supplemented"), None)
        next_audit_or_save = next((event for event in stages
                                  if event["action"] in {"check_rules", "save_draft", "finish"}), None)
        if not (missing and supplemented and next_audit_or_save
                and next_audit_or_save["action"] == "check_rules"
                and next_audit_or_save["result"]["status"] ==
                ("passed" if result["status"] == "saved" else result["status"])):
            failures.append("missing audit-to-supplement-to-recheck sequence")
    elif action not in [event["action"] for event in trace]:
        failures.append(f"missing action {action}")

    evidence = expected_result["evidence"]
    if evidence and not (
        result["incomplete_reason"] == evidence
        or result["draft"].get("reason") == evidence
        or any(evidence in value for value in result["pending_items"])
        or any(finding["rule_id"] == evidence for finding in result["risk_reasons"])
        or any(source["source_id"] == evidence for source in result.get("audit_evidence", []))
    ):
        failures.append(f"missing evidence {evidence}")
    return failures


def main(default_mode="minimax"):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("mock", "minimax"), default=default_mode)
    parser.add_argument("--output", type=Path, help="Optional JSON report with complete traces")
    args = parser.parse_args()
    if args.mode == "minimax" and not os.environ.get("MINIMAX_API_KEY"):
        parser.error("set MINIMAX_API_KEY before running live tests")
    if args.mode == "minimax":
        os.environ.setdefault("MINIMAX_REQUEST_INTERVAL_SECONDS", "3.2")
    revision = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=ROOT, capture_output=True, text=True,
    ).stdout.strip() if shutil.which("git") and (ROOT / ".git").exists() else None
    cases = []
    for definition in matrix():
        case = run_case(definition, args.mode)
        cases.append(case)
        statuses = " → ".join(item["status"] for item in case["actual"])
        counts = " → ".join(str(phase["draft_count"]) for phase in case["phases"])
        print(f'{"PASS" if case["passed"] else "FAIL"} {case["case"]}: {statuses} | drafts: {counts}',
              flush=True)
        for item in case["actual"]:
            if item["status"] == "reused":
                print(f'  reused draft ID: {item["draft"]["draft_id"]}', flush=True)
        for failure in case["failures"]:
            print(f"  {failure}", flush=True)
    report = {
        "run_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "mode": args.mode,
        "source_revision": revision,
        "request_interval_seconds": (float(os.environ["MINIMAX_REQUEST_INTERVAL_SECONDS"])
                                     if args.mode == "minimax" else None),
        "models": sorted({item["model"] for case in cases for item in case["actual"]
                          if item["model"] is not None}),
        "cases_passed": sum(case["passed"] for case in cases),
        "cases_total": len(cases),
        "cases": cases,
    }
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    print(f'{args.mode}: {report["cases_passed"]}/{report["cases_total"]} cases passed')
    return 0 if report["cases_passed"] == report["cases_total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
