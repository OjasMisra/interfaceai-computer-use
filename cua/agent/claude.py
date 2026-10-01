"""ClaudePlanner: the real LLM behind the planner seam (Anthropic Messages API, tool use).

Each decision is one stateless request: stable system prompt + tool schema
(cache-friendly prefix), then the goal, the declared inputs, a compact history
and the current redacted observation. Claude answers by calling a single
strict-schema tool, ``next_action``; that JSON is validated into an
AgentAction, and the runtime -- not the model -- executes and policy-checks it.

Not exercised in the committed evidence (no API key was available while
building); the MockPlanner implements the same contract. Requires
``pip install anthropic`` and ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import json
import os

from ..surface import Observation
from .planner import AgentAction, InputHint

MODEL = os.environ.get("CUA_CLAUDE_MODEL", "claude-opus-5-5")

SYSTEM = """You operate a legacy core-banking application on behalf of a bank's back-office team, \
one action at a time. Each turn you get the goal, the inputs, what you did so far, and the current \
screen as frames of elements (ref, role, name, label, value, table context).

Rules:
- Always answer by calling the next_action tool exactly once.
- Refer to elements only by their ref from the CURRENT observation.
- Type caller inputs as templates, e.g. {{inputs.member_number}}, never as literal values.
- For data in a grid, use extract on the cell and set row_key to the column(s) that identify the row \
(e.g. ["Type"]), never rely on row position.
- Values shown as «label» are redacted on purpose. Do not try to recover them.
- If something unexpected covers the screen, the goal seems impossible, or you are unsure an action \
is safe, choose escalate with a clear reason. A human operator will take over the live session.
- Commit actions (Post, Submit, Transfer...) are gated by policy; propose them only when the screen \
shows exactly what the goal asks for.
- When the goal is achieved and all requested data is extracted, choose done and name the reusable \
capability (id like member.lookup_savings_balance, a title, a one-sentence description)."""

NULLABLE_STR = {"type": ["string", "null"]}
ACTION_TOOL = {
    "name": "next_action",
    "description": "The single next UI action to take, or done/escalate.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "ref", "value", "output", "row_key", "capability", "rationale"],
        "properties": {
            "type": {"type": "string", "enum": ["click", "fill", "select", "extract", "done", "escalate"]},
            "ref": NULLABLE_STR,
            "value": NULLABLE_STR,
            "output": {"anyOf": [{"type": "null"}, {
                "type": "object", "additionalProperties": False,
                "required": ["name", "type", "description", "sensitivity"],
                "properties": {"name": {"type": "string"},
                               "type": {"type": "string", "enum": ["string", "integer", "decimal", "enum"]},
                               "description": {"type": "string"},
                               "sensitivity": {"type": "string", "enum": ["public", "internal", "pii"]}}}]},
            "row_key": {"anyOf": [{"type": "null"}, {"type": "array", "items": {"type": "string"}}]},
            "capability": {"anyOf": [{"type": "null"}, {
                "type": "object", "additionalProperties": False, "required": ["id", "title", "description"],
                "properties": {"id": {"type": "string"}, "title": {"type": "string"},
                               "description": {"type": "string"}}}]},
            "rationale": {"type": "string"},
        },
    },
}
INPUTS_TOOL = {
    "name": "declare_inputs",
    "description": "Declare the caller-supplied values contained in the goal.",
    "strict": True,
    "input_schema": {
        "type": "object", "additionalProperties": False, "required": ["inputs"],
        "properties": {"inputs": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "type", "value", "description", "pattern", "enum", "minimum"],
            "properties": {"name": {"type": "string"},
                           "type": {"type": "string", "enum": ["string", "integer", "decimal", "enum"]},
                           "value": {"type": "string"}, "description": {"type": "string"},
                           "pattern": NULLABLE_STR, "minimum": NULLABLE_STR,
                           "enum": {"anyOf": [{"type": "null"}, {"type": "array", "items": {"type": "string"}}]}}}}},
    },
}


def render_observation(obs: Observation) -> str:
    lines = []
    for f in obs.frames:
        lines.append(f"## frame {'/'.join(f['path']) or 'top'} ({f['url']})")
        lines.append("text: " + " | ".join(f["text"][:40]))
        for e in f["elements"]:
            bits = [e["ref"], e["role"], repr(e.get("name", ""))]
            if e.get("label"):
                bits.append(f"label={e['label']!r}")
            if e.get("value"):
                bits.append(f"value={e['value']!r}")
            if e.get("options"):
                bits.append(f"options={e['options']}")
            if e.get("blocked"):
                bits.append("BLOCKED")
            t = e.get("table") or {}
            if t.get("header"):
                bits.append(f"column={t['header']!r} row={t.get('row_values')}")
            lines.append("  " + " ".join(bits))
    return "\n".join(lines)


class ClaudePlanner:
    def __init__(self, model: str = MODEL, effort: str = "medium"):
        import anthropic  # optional dependency
        self._client = anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.name = f"claude/{model}"

    def _call(self, tool: dict, user: str) -> dict:
        resp = self._client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM,
            tools=[tool],
            output_config={"effort": self.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": user + f"\n\nCall the {tool['name']} tool."}],
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"model declined: {getattr(resp, 'stop_details', None)}")
        for block in resp.content:
            if block.type == "tool_use" and block.name == tool["name"]:
                return block.input
        raise RuntimeError(f"model did not call {tool['name']} (stop_reason={resp.stop_reason})")

    def parse_goal(self, goal: str) -> list[InputHint]:
        data = self._call(INPUTS_TOOL, f"Goal: {goal}\n\nList every value in the goal that a caller would "
                                       "supply per invocation (ids, amounts, choices). Use snake_case names.")
        return [InputHint.model_validate(i) for i in data["inputs"]]

    def decide(self, goal: str, inputs: dict[str, str], obs: Observation, history: list[dict]) -> AgentAction:
        user = (f"Goal: {goal}\nInputs (use as templates): {sorted(inputs)}\n"
                f"History: {json.dumps(history[-12:])}\n\nCurrent observation:\n{render_observation(obs)}")
        try:
            return AgentAction.model_validate(self._call(ACTION_TOOL, user))
        except Exception as e:  # a bad decision becomes an escalation, never an unchecked action
            return AgentAction(type="escalate", rationale=f"planner error: {e}")
