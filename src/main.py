import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .agent import MAX_STEPS, run
from .context import DEFAULT_CONTEXT_BYTES
from .domain import DraftStore, Request, Tools, parse_slot
from .models import MiniMax, Mock


DATA = Path(__file__).resolve().parent.parent / "data"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_material_map(path):
    data = read_json(path)
    if not isinstance(data, dict) or not all(isinstance(entry, dict) for entry in data.values()):
        raise ValueError("资料文件须按素材 ID 提供 JSON 对象")
    return data


def load_evidence(path):
    data = read_material_map(path)
    for fields in data.values():
        for entry in fields.values():
            records = entry if isinstance(entry, list) else [entry]
            if not records or not all(
                isinstance(record, dict) and isinstance(record.get("source_id"), str)
                and record["source_id"].strip() and "value" in record
                for record in records
            ):
                raise ValueError("证据字段须提供一条记录或非空记录列表，每条包含 source_id 和 value")
            if len({record["source_id"] for record in records}) != len(records):
                raise ValueError("同一字段的证据来源编号不能重复")
    return data


def load_calendar(path):
    calendar = read_json(path)
    if not isinstance(calendar, dict) or not all(
        isinstance(calendar.get(field), list) for field in ("occupied", "alternative_times")
    ):
        raise ValueError("排期文件需要 occupied 和 alternative_times 数组")
    calendar["occupied"] = [parse_slot(slot) for slot in calendar["occupied"]]
    calendar["alternative_times"] = list(dict.fromkeys(
        parse_slot(f"2026-01-01T{time}+08:00")[11:16] for time in calendar["alternative_times"]
    ))
    return calendar


def main(argv=None):
    parser = argparse.ArgumentParser(description="素材审核与发布排期 Agent")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, default=DATA / "calendar.json")
    parser.add_argument("--materials", type=Path, default=DATA / "materials.json",
                        help="按素材 ID 提供的元数据文件")
    parser.add_argument("--evidence", type=Path, default=DATA / "evidence.json",
                        help="按素材和审核字段提供的证据文件")
    parser.add_argument("--db", type=Path, default=Path("drafts.sqlite3"))
    parser.add_argument("--mode", choices=("mock", "minimax"), default="mock")
    parser.add_argument("--max-steps", type=int, choices=range(1, MAX_STEPS + 1), default=MAX_STEPS)
    parser.add_argument("--context-budget-bytes", type=int, default=DEFAULT_CONTEXT_BYTES,
                        help="系统提示词、工具定义和消息的 JSON 字节预算")
    parser.add_argument("--inject-save-failure", action="store_true")
    args = parser.parse_args(argv)
    if args.context_budget_bytes < 1:
        parser.error("--context-budget-bytes 必须为正整数")

    try:
        items = read_json(args.input)
        if not isinstance(items, list) or not items or not all(isinstance(item, dict) for item in items):
            raise ValueError("输入须为非空 JSON 对象数组")
        requests = [Request.from_json(item) for item in items]
        calendar = load_calendar(args.calendar)
        materials = read_material_map(args.materials)
        rules = read_json(DATA / "rules.json")
        evidence = load_evidence(args.evidence)
        model = MiniMax() if args.mode == "minimax" else Mock()
        store = DraftStore(args.db)
        results = [
            run(model, Tools(request, materials, rules, calendar, store, args.inject_save_failure,
                             evidence=evidence),
                args.max_steps, args.context_budget_bytes)
            for request in requests
        ]
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "execution_error", "message": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    print(json.dumps({"mode": args.mode, "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
