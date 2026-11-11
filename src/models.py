import json
import math
import os
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .context import compact_json


DEFAULT_MODEL = "MiniMax-M3.1-Flash"
ENDPOINTS = {
    "cn": "https://api.minimax.cn/anthropic/v1/messages",
    "global": "https://api.minimax.io/anthropic/v1/messages",
}


class ModelError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MiniMax:
    def __init__(self):
        self.key = os.environ.get("MINIMAX_API_KEY")
        self.model = os.environ.get("MINIMAX_MODEL") or DEFAULT_MODEL
        self.endpoint = ENDPOINTS.get(os.environ.get("MINIMAX_REGION") or "cn")
        if not self.key:
            raise ValueError("请设置环境变量 MINIMAX_API_KEY")
        if self.endpoint is None:
            raise ValueError("MINIMAX_REGION 只接受 cn 或 global")
        self.request_interval = float(os.environ.get("MINIMAX_REQUEST_INTERVAL_SECONDS") or "0")
        if not math.isfinite(self.request_interval) or not 0 <= self.request_interval <= 60:
            raise ValueError("MINIMAX_REQUEST_INTERVAL_SECONDS 须在 0 到 60 之间")
        self.last_request = monotonic()
        self.opener = build_opener(NoRedirect())

    def next_turn(self, system, messages, tools):
        payload = {
            "model": self.model,
            "max_tokens": 2048,
            "system": system,
            "messages": messages,
            "tools": tools,
            "tool_choice": {"type": "auto"},
        }
        if self.model == "MiniMax-M3.1-Flash":
            payload["output_config"] = {"effort": "low"}
        request = Request(self.endpoint, data=compact_json(payload).encode("utf-8"), headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.key}",
            "anthropic-version": "2023-06-01",
        })
        data = self.send(request)
        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            raise ModelError("minimax_invalid_response")
        if not all(isinstance(block, dict) for block in data["content"]):
            raise ModelError("minimax_invalid_content_block")
        return data

    def send(self, request):
        remaining_wait = 60
        for fallback in (15, 45, None):
            delay = self.last_request + self.request_interval - monotonic()
            if delay > 0:
                sleep(delay)
            self.last_request = monotonic()
            try:
                with self.opener.open(request, timeout=30) as response:
                    return json.load(response)
            except HTTPError as exc:
                if exc.code != 429 or fallback is None:
                    raise ModelError(f"minimax_http_{exc.code}") from None
                retry_after = (exc.headers or {}).get("Retry-After", "")
                delay = max(1, int(retry_after)) if retry_after.isascii() and retry_after.isdecimal() else fallback
                if delay > remaining_wait:
                    raise ModelError("minimax_http_429") from None
                remaining_wait -= delay
                sleep(delay)
            except (URLError, TimeoutError, OSError, ValueError):
                raise ModelError("minimax_connection_or_response_error") from None


class Mock:
    model = "offline-mock"

    def next_turn(self, system, messages, tools):
        if len(messages) == 1:
            name, arguments = "read_material", {"reason": "读取审核所需的素材字段"}
        else:
            name = next(
                b["name"] for b in messages[-2]["content"] if b["type"] == "tool_use"
            )
            result = json.loads(messages[-1]["content"][0]["content"])
            name, arguments = self.after_observation(name, result)
        return {"model": self.model, "content": [{
            "type": "tool_use", "id": f"mock-{len(messages)}", "name": name, "input": arguments,
        }]}

    @staticmethod
    def after_observation(name, result):
        if name == "read_material":
            return "check_rules", {"reason": "根据素材数据执行审核规则"}
        if name == "check_rules" and result["status"] == "passed":
            return "query_slots", {"mode": "exact", "reason": "审核通过，查询用户指定的时段"}
        if name == "check_rules" and result.get("supplement_available"):
            return "lookup_evidence", {"reason": "审核缺项有可用的本地证据"}
        if name == "check_rules" and result["status"] in {"insufficient_info", "evidence_conflict"}:
            return "ask_user", {"reason": "审核资料有缺项或冲突，请操作者核实"}
        if name == "lookup_evidence" and result["status"] == "supplemented":
            return "check_rules", {"reason": "补查完成，重新审核素材"}
        if name == "query_slots" and result["mode"] == "exact":
            if result["available"]:
                return "save_draft", {"slot": result["available"][0], "reason": "授权时段空闲"}
            return "query_slots", {"mode": "alternatives", "reason": "指定时段被占用，查询同日替代"}
        if name == "save_draft" and result["status"] == "slot_conflict":
            return "query_slots", {"mode": "alternatives", "reason": "写入前时段被占用，查询替代"}
        if name == "query_slots" and result["available"]:
            return "finish", {
                "proposed_slot": result["available"][0],
                "reason": "候选空闲，改期需用户确认",
            }
        return "finish", {"reason": {
            "blocked": "审核发现阻断风险",
            "insufficient_info": "缺少审核字段，需用户补充",
            "save_failed": "草稿写入失败",
            "saved": "工具返回了已保存的草稿 ID",
            "reused": "工具返回了已有草稿",
        }.get(result["status"], "当前工具结果不足以继续保存")}
