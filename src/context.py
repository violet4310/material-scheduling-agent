import json


DEFAULT_CONTEXT_BYTES = 24 * 1024
OBSERVATION_BYTES = 2 * 1024


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_bytes(value):
    return len(compact_json(value).encode("utf-8"))


def bounded_observation(observation):
    if json_bytes(observation) <= OBSERVATION_BYTES:
        return observation
    # The status and write receipt survive even when diagnostic details are large.
    essential = ("status", "reason", "mode", "slot", "draft_id")
    return {
        **{key: observation[key][:48] for key in essential
           if isinstance(observation.get(key), str)},
        "details_omitted": True,
    }


class ContextWindow:
    def __init__(self, request, system, tools, budget):
        self.request = request
        self.system = system
        self.tools = tools
        self.budget = budget
        self.compact_at = budget // 2
        self.exchanges = []
        self.removed = 0

    def append(self, assistant_blocks, observations):
        self.exchanges.append((
            {"role": "assistant", "content": assistant_blocks},
            {"role": "user", "content": observations},
        ))

    def messages(self, checkpoint):
        anchor = self.request
        if self.removed:
            anchor = {**self.request, "runtime_checkpoint": checkpoint}
        return [
            {"role": "user", "content": compact_json(anchor)},
            *(message for exchange in self.exchanges for message in exchange),
        ]

    def size(self, messages):
        return json_bytes({"system": self.system, "tools": self.tools, "messages": messages})

    def prepare(self, checkpoint):
        messages = self.messages(checkpoint)
        before = after = self.size(messages)
        removed_now = 0
        # Keep the latest provider reply and its matching tool result together.
        while after > self.compact_at and len(self.exchanges) > 1:
            self.exchanges.pop(0)
            self.removed += 1
            removed_now += 1
            messages = self.messages(checkpoint)
            after = self.size(messages)
        return messages, {
            "bytes_before": before,
            "bytes_after": after,
            "exchanges_removed": removed_now,
            "exchanges_retained": len(self.exchanges),
            "over_budget": after > self.budget,
        }
