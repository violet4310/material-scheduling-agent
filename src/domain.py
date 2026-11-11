import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import uuid4


SLOT_PREVIEW_LIMIT = 8


@dataclass(frozen=True)
class Request:
    material_id: str
    requested_slot: str
    confirmed_slot: str
    confirmation_id: Optional[str] = None

    @classmethod
    def from_json(cls, item):
        material_id = item.get("material_id")
        if not isinstance(material_id, str) or not material_id.strip():
            raise ValueError("material_id 不能为空")
        requested = parse_slot(item.get("requested_slot"))
        confirmed = parse_slot(item.get("confirmed_slot", requested))
        confirmation_id = item.get("confirmation_id")
        if confirmation_id is not None and (
            not isinstance(confirmation_id, str)
            or not re.fullmatch(r"[0-9a-f-]{36}", confirmation_id)
            or "confirmed_slot" not in item
        ):
            raise ValueError("confirmation_id 需要草稿建议返回的编号及显式 confirmed_slot")
        return cls(material_id.strip(), requested, confirmed, confirmation_id)

    def as_dict(self):
        return {
            "material_id": self.material_id,
            "requested_slot": self.requested_slot,
            "confirmed_slot": self.confirmed_slot,
        }


def parse_slot(slot):
    if not isinstance(slot, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}\+08:00", slot):
        raise ValueError("时段格式为 YYYY-MM-DDTHH:MM+08:00")
    return datetime.fromisoformat(slot).isoformat(timespec="minutes")


class DraftStore:
    def __init__(self, path):
        self.path = path
        with closing(self.connect()) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS drafts (
                    draft_id TEXT PRIMARY KEY,
                    material_id TEXT NOT NULL,
                    requested_slot TEXT NOT NULL,
                    slot TEXT NOT NULL UNIQUE,
                    UNIQUE (material_id, requested_slot)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS confirmations (
                    confirmation_id TEXT PRIMARY KEY,
                    material_id TEXT NOT NULL,
                    requested_slot TEXT NOT NULL,
                    slot TEXT NOT NULL,
                    draft_id TEXT UNIQUE REFERENCES drafts(draft_id),
                    UNIQUE (material_id, requested_slot, slot)
                )
            """)

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def existing(self, request):
        with closing(self.connect()) as db:
            return self.find(db, request)

    @staticmethod
    def find(db, request):
        row = db.execute(
            "SELECT draft_id, slot FROM drafts WHERE material_id = ? AND requested_slot = ?",
            (request.material_id, request.requested_slot),
        ).fetchone()
        return dict(row) if row else None

    def occupied(self, slots):
        placeholders = ", ".join("?" for _ in slots)
        with closing(self.connect()) as db:
            return {row["slot"] for row in db.execute(
                f"SELECT slot FROM drafts WHERE slot IN ({placeholders})", slots
            )}

    def confirmation_matches(self, request):
        with closing(self.connect()) as db:
            return self.match_confirmation(db, request)

    @staticmethod
    def match_confirmation(db, request):
        return db.execute("""
            SELECT 1 FROM confirmations
            WHERE confirmation_id = ? AND material_id = ? AND requested_slot = ? AND slot = ?
        """, (request.confirmation_id, request.material_id,
              request.requested_slot, request.confirmed_slot)).fetchone() is not None

    @staticmethod
    def taken(db, slot, calendar):
        return slot in calendar or db.execute(
            "SELECT 1 FROM drafts WHERE slot = ?", (slot,)
        ).fetchone() is not None

    def offer(self, request, slot, calendar):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = self.find(db, request)
            if existing:
                return existing_result(existing, request)
            if self.taken(db, slot, calendar):
                return {"status": "slot_conflict", "slot": slot}
            binding = (request.material_id, request.requested_slot, slot)
            row = db.execute("""
                SELECT confirmation_id FROM confirmations
                WHERE material_id = ? AND requested_slot = ? AND slot = ?
            """, binding).fetchone()
            confirmation_id = row["confirmation_id"] if row else str(uuid4())
            if row is None:
                db.execute("""
                    INSERT INTO confirmations (confirmation_id, material_id, requested_slot, slot)
                    VALUES (?, ?, ?, ?)
                """,
                           (confirmation_id, *binding))
            return {"status": "pending", "confirmation_id": confirmation_id, "slot": slot}

    def save(self, request, slot, calendar, inject_failure):
        try:
            with closing(self.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if request.confirmation_id and not self.match_confirmation(db, request):
                    return {"status": "invalid_confirmation"}
                existing = self.find(db, request)
                if existing:
                    return existing_result(existing, request)
                if self.taken(db, slot, calendar):
                    return {"status": "slot_conflict", "slot": slot}

                draft_id = str(uuid4())
                db.execute(
                    "INSERT INTO drafts VALUES (?, ?, ?, ?)",
                    (draft_id, request.material_id, request.requested_slot, slot),
                )
                if request.confirmation_id:
                    db.execute("UPDATE confirmations SET draft_id = ? WHERE confirmation_id = ?",
                               (draft_id, request.confirmation_id))
                if inject_failure:
                    raise sqlite3.OperationalError("injected save failure")
                return {"status": "saved", "draft_id": draft_id, "slot": slot}
        except sqlite3.Error:
            return {
                "status": "save_failed",
                "reason": "injected_save_failure" if inject_failure else "database_write_error",
            }


def existing_result(existing, request):
    if existing["slot"] != request.confirmed_slot:
        return {"status": "draft_exists_at_different_slot", **existing}
    return {"status": "reused", **existing}


class Tools:
    def __init__(self, request, materials, rules, calendar, store, inject_failure=False,
                 evidence=None):
        self.request = request
        self.materials = materials
        self.rules = rules
        self.calendar = calendar
        self.store = store
        self.inject_failure = inject_failure
        self.evidence = (evidence or {}).get(request.material_id, {})
        self.supplemented = {}
        self.read = None
        self.audit = None
        self.available = set()
        self.conflicted = False
        self.save = None

    def read_material(self):
        source = self.materials.get(self.request.material_id)
        self.read = {
            "status": "found" if source is not None else "material_missing",
            "material_id": self.request.material_id,
            "metadata": source,
        }
        if source is None:
            return self.read
        fields = dict.fromkeys(["title", *(rule["field"] for rule in self.rules)])
        preview = {}
        clipped = 0
        for field in list(fields)[:12]:
            if field not in source:
                continue
            value = source[field]
            if isinstance(value, str):
                preview[field] = value[:120]
                clipped += int(len(value) > 120)
            elif value is None or isinstance(value, (bool, int, float)):
                preview[field] = value
        result = {**self.read, "metadata": preview}
        if len(preview) < len(source):
            result["metadata_fields_omitted"] = len(source) - len(preview)
        if clipped:
            result["metadata_fields_clipped"] = clipped
        return result

    def check_rules(self):
        if self.read is None:
            return {"status": "prerequisite_missing", "required_tool": "read_material"}
        metadata = {**(self.read["metadata"] or {}),
                    **{field: proofs[0]["value"] for field, proofs in self.supplemented.items()}}
        findings = []
        for rule in self.rules:
            field = rule["field"]
            value = metadata.get(field)
            records = self.valid_evidence(rule) if self.read["status"] == "found" else []
            if records and type(value) is type(rule["expected"]):
                records = [
                    {"source_id": f"metadata:{self.request.material_id}", "value": value},
                    *records,
                ]
            if records and any(record["value"] != records[0]["value"] for record in records):
                findings.append({
                    "rule_id": rule["id"], "kind": "evidence_conflict", "field": field,
                    "reason": f"{field} 的证据结论冲突，请核实后重新提交",
                    "sources": [{"source_id": record["source_id"], "value": record["value"]}
                                for record in records],
                })
                continue
            if type(value) is not type(rule["expected"]) or value == "":
                findings.append({
                    "rule_id": rule["id"], "kind": "missing_or_invalid",
                    "field": field, "reason": f"请补齐或修正 {field}",
                })
            elif value != rule["expected"]:
                findings.append({
                    "rule_id": rule["id"], "kind": "risk",
                    "field": field, "reason": rule["reason"],
                })
        if any(finding["kind"] == "evidence_conflict" for finding in findings):
            status = "evidence_conflict"
        elif any(finding["kind"] == "risk" for finding in findings):
            status = "blocked"
        elif findings or self.read["status"] == "material_missing":
            status = "insufficient_info"
        else:
            status = "passed"
        self.audit = {"status": status, "findings": findings}
        if status == "insufficient_info":
            self.audit["supplement_available"] = bool(self.supplement_candidates())
        if len(findings) > 6:
            return {**self.audit, "findings": findings[:6], "findings_count": len(findings)}
        return self.audit

    def valid_evidence(self, rule):
        entry = self.evidence.get(rule["field"], [])
        records = entry if isinstance(entry, list) else [entry]
        return [record for record in records
                if type(record["value"]) is type(rule["expected"]) and record["value"] != ""]

    def supplement_candidates(self):
        if not self.audit or self.audit["status"] != "insufficient_info" or self.read["metadata"] is None:
            return {}
        missing = {finding["field"] for finding in self.audit["findings"]}
        candidates = {}
        for rule in self.rules:
            field = rule["field"]
            if field in missing and field not in self.supplemented:
                records = self.valid_evidence(rule)
                if records:
                    candidates[field] = records
        return candidates

    def lookup_evidence(self):
        candidates = self.supplement_candidates()
        if not candidates:
            return {"status": "no_supplement"}
        self.supplemented.update(candidates)
        self.audit = None
        sources = self.evidence_sources()
        return {
            "status": "supplemented",
            "sources": sources[:6],
            "source_count": len(sources),
        }

    def evidence_sources(self):
        return [{"field": field, "source_id": proof["source_id"]}
                for field, proofs in self.supplemented.items() for proof in proofs]

    def query_slots(self, mode):
        if self.audit is None or self.audit["status"] != "passed":
            return {"status": "prerequisite_missing", "required": "passed_audit"}
        target = self.request.confirmed_slot
        if mode == "exact":
            slots = [target]
        elif mode == "alternatives":
            slots = [
                f"{target[:10]}T{hour}+08:00"
                for hour in self.calendar["alternative_times"] if hour != target[11:16]
            ]
        else:
            return {"status": "invalid_arguments", "required": "exact or alternatives"}
        occupied = self.store.occupied(slots) | set(self.calendar["occupied"])
        available = [slot for slot in slots if slot not in occupied]
        occupied_slots = [slot for slot in slots if slot in occupied]
        self.available.difference_update(slots)
        self.available.update(available[:SLOT_PREVIEW_LIMIT])
        if mode == "exact":
            self.conflicted = not available
        result = {
            "status": "ok",
            "mode": mode,
            "available": available[:SLOT_PREVIEW_LIMIT],
            "occupied": occupied_slots[:SLOT_PREVIEW_LIMIT],
        }
        if len(available) > SLOT_PREVIEW_LIMIT or len(occupied_slots) > SLOT_PREVIEW_LIMIT:
            result.update(available_count=len(available), occupied_count=len(occupied_slots))
        return result

    def save_draft(self, slot):
        if self.audit is None or self.audit["status"] != "passed":
            return {"status": "rejected", "reason": "passed_audit_required"}
        if slot != self.request.confirmed_slot:
            return {"status": "rejected", "reason": "slot_not_authorized"}
        if slot not in self.available:
            return {"status": "rejected", "reason": "available_slot_check_required"}
        self.save = self.store.save(
            self.request, slot, self.calendar["occupied"], self.inject_failure
        )
        if self.save["status"] == "slot_conflict":
            self.available.discard(slot)
            self.conflicted = True
        return self.save

    def checkpoint(self):
        return {
            "material_status": self.read["status"] if self.read else "not_read",
            "audit_status": self.audit["status"] if self.audit else "not_reviewed",
            "supplement_available": bool(self.audit and self.audit.get("supplement_available")),
            "supplemented_count": len(self.supplemented),
            "available_slots": sorted(self.available),
            "slot_conflict": self.conflicted,
            "draft": self.save or {"status": "not_attempted"},
        }

    def finish(self, proposed_slot=None):
        result = self.result(proposed_slot)
        if result["status"] != "needs_confirmation":
            return result
        try:
            offer = self.store.offer(self.request, proposed_slot, self.calendar["occupied"])
        except sqlite3.Error:
            return self.result(incomplete_reason="confirmation_write_error")
        if offer["status"] == "pending":
            return {**result, "confirmation": offer}
        if offer["status"] != "slot_conflict":
            self.save = offer
        return self.result()

    def ask_user(self):
        if not self.audit or self.audit["status"] not in {"insufficient_info", "evidence_conflict"}:
            return {"status": "rejected", "reason": "insufficient_info_audit_required"}
        if self.audit.get("supplement_available"):
            return {"status": "rejected", "reason": "lookup_evidence_available"}
        if self.audit["status"] == "evidence_conflict":
            questions = [{
                "field": finding["field"],
                "rule_id": finding["rule_id"],
                "sources": finding["sources"],
                "question": "这些证据的审核结论不同，请核实哪一条是有效记录。",
            } for finding in self.audit["findings"] if finding["kind"] == "evidence_conflict"]
            resume_option = "--evidence"
        elif self.read["status"] == "material_missing":
            questions = [{
                "field": "material_metadata",
                "question": f"请提供素材 {self.request.material_id} 的元数据。",
            }]
            resume_option = "--materials"
        else:
            rules = {rule["id"]: rule for rule in self.rules}
            questions = [{
                "field": finding["field"],
                "rule_id": finding["rule_id"],
                "question": rules[finding["rule_id"]].get(
                    "question", f'请提供 {finding["field"]} 的审核结论和证据编号。'
                ),
            } for finding in self.audit["findings"]]
            resume_option = "--evidence"
        return {
            **self.result(),
            "clarification": {
                "questions": questions,
                "resume_option": resume_option,
                "resume_instruction": "补齐资料后，用相同请求和数据库重新运行审核。",
            },
        }

    def result(self, proposed_slot=None, incomplete_reason=None):
        audit = self.audit or {"status": "not_reviewed", "findings": []}
        pending = [
            finding["reason"] for finding in audit["findings"]
            if finding["kind"] in {"missing_or_invalid", "evidence_conflict"}
        ]
        if self.read and self.read["status"] == "material_missing":
            pending.insert(0, f"请提供素材 {self.request.material_id} 的元数据")
        save = self.save or {"status": "not_attempted"}
        status = "incomplete"
        suggestion = None
        if incomplete_reason:
            status = "incomplete"
        elif save["status"] in {"saved", "reused", "save_failed", "draft_exists_at_different_slot",
                                "invalid_confirmation"}:
            status = save["status"]
        elif audit["status"] in {"blocked", "insufficient_info", "evidence_conflict"}:
            status = audit["status"]
        elif self.read and self.read["status"] == "material_missing":
            status = "insufficient_info"
        elif self.conflicted and proposed_slot in self.available:
            status = "needs_confirmation"
            suggestion = proposed_slot
            pending.append(f"请确认改期至 {suggestion}")
        elif self.conflicted:
            status = "slot_conflict"
            pending.append("请提供可接受的其他发布时间")
        else:
            incomplete_reason = "model_stopped_before_resolution"
        return {
            **self.request.as_dict(),
            **({"submitted_confirmation_id": self.request.confirmation_id}
               if self.request.confirmation_id else {}),
            "status": status,
            "audit_status": "passed" if save["status"] in {"saved", "reused"} else audit["status"],
            "risk_reasons": [f for f in audit["findings"] if f["kind"] == "risk"],
            "audit_evidence": self.evidence_sources(),
            "evidence_conflicts": [f for f in audit["findings"]
                                   if f["kind"] == "evidence_conflict"],
            "suggested_slot": suggestion or save.get("slot"),
            "pending_items": pending,
            "draft": save,
            "incomplete_reason": incomplete_reason,
        }
