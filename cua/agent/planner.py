"""The planner seam: the only place a model makes decisions.

A planner sees a redacted Observation and returns one AgentAction. It never
receives credentials (sign-on happens outside the loop), never acts directly
(the runtime executes and policy-checks every action), and refers to caller
inputs by template (``{{inputs.member_number}}``) so the recorded flow is
parameterised by construction rather than by guessing afterwards.

Two implementations:

* ``MockPlanner``      deterministic heuristics over the same observation
                       contract. Used for the offline demo and tests: no key,
                       no network, reproducible evidence.
* ``ClaudePlanner``    Claude via tool use (``cua/agent/claude.py``), enabled
                       when ANTHROPIC_API_KEY is set.
"""

from __future__ import annotations

import re
from typing import Literal, Protocol

from pydantic import Field

from ..schema import Model, ParamType
from ..surface import Observation


class InputHint(Model):
    name: str
    type: ParamType
    value: str
    description: str
    pattern: str | None = None
    enum: list[str] | None = None
    minimum: str | None = None


class OutputHint(Model):
    name: str
    type: ParamType
    description: str
    sensitivity: str = "pii"


class CapabilityHint(Model):
    id: str
    title: str
    description: str


class AgentAction(Model):
    type: Literal["click", "fill", "select", "extract", "done", "escalate"]
    ref: str | None = Field(None, description="Element ref from the observation, e.g. 'work:7'")
    value: str | None = Field(None, description="Literal, or {{inputs.<name>}} for caller-supplied values")
    output: OutputHint | None = None
    row_key: list[str] | None = Field(None, description="For extract from a grid: columns that identify the row")
    capability: CapabilityHint | None = None
    rationale: str


class Planner(Protocol):
    name: str

    def parse_goal(self, goal: str) -> list[InputHint]: ...
    def decide(self, goal: str, inputs: dict[str, str], obs: Observation, history: list[dict]) -> AgentAction: ...


# ------------------------------------------------------------------ mock

def _n(s: str | None) -> str:
    return " ".join((s or "").split()).lower()


class MockPlanner:
    """Heuristic stand-in for an LLM. It genuinely reads the observation each
    turn (it finds controls by role/name/label, notices blocked elements and
    unknown screens) but its task knowledge is hand-written for the two demo
    goal families. When it does not recognise the situation it escalates,
    exactly as the real planner is instructed to."""

    name = "mock-heuristic-planner/1"

    def parse_goal(self, goal: str) -> list[InputHint]:
        hints = []
        if m := re.search(r"member\s+(?:number\s+|no\.?\s+|#)?(\d{3,12})", goal, re.I):
            hints.append(InputHint(name="member_number", type=ParamType.STRING, value=m.group(1),
                                   pattern=r"\d{5,9}", description="Member (account holder) number"))
        if m := re.search(r"\$\s?([\d,]+(?:\.\d{1,2})?)", goal):
            hints.append(InputHint(name="opening_deposit", type=ParamType.DECIMAL, value=m.group(1).replace(",", ""),
                                   minimum="0.01", description="Opening deposit amount in USD"))
        if m := re.search(r"\b(savings|club|certificate)\b\s+(?:sub-account|share|account)", goal, re.I):
            hints.append(InputHint(name="share_type", type=ParamType.ENUM, value=m.group(1).upper(),
                                   enum=["SAVINGS", "CLUB", "CERTIFICATE"], description="Type of share to open"))
        return hints

    def decide(self, goal: str, inputs: dict[str, str], obs: Observation, history: list[dict]) -> AgentAction:
        g = goal.lower()
        task = "open_share" if re.search(r"\bopen\b", g) else "balance" if "balance" in g else None
        if task is None:
            return AgentAction(type="escalate", rationale="I don't know how to accomplish this goal in this app.")

        work = next((f for f in obs.frames if f["path"] == ["work"]), None)
        menu = next((f for f in obs.frames if f["path"] == ["menu"]), None)
        text = "\n".join(obs.primary_text).upper()
        els = work["elements"] if work else []

        def find(role, name=None, label=None, frame=els):
            for e in frame:
                if e["role"] == role and (name is None or _n(e["name"]) == _n(name)) \
                        and (label is None or _n(e.get("label")) == _n(label)):
                    return e
            return None

        extracted = {h["output"] for h in history if h.get("type") == "extract"}

        # Something covering the screen we don't know about: don't click through it blindly.
        if any(e.get("blocked") for e in els):
            return AgentAction(type="escalate", rationale="An unrecognised element is covering the screen.")

        if task == "balance":
            if "savings_balance" in extracted:
                return AgentAction(type="done", rationale="Savings balance has been read.",
                                   capability=CapabilityHint(
                                       id="member.lookup_savings_balance", title="Look up a member's savings balance",
                                       description="Finds a member by member number and returns the current balance "
                                                   "of their primary savings share."))
            if "MEMBER SHARES" in text:
                cell = next((e for e in els if e["role"] == "cell" and (e.get("table") or {}).get("header") == "Balance"
                             and "SAVINGS" in (e["table"].get("row_values") or [])), None)
                if cell:
                    return AgentAction(type="extract", ref=cell["ref"], row_key=["Type"],
                                       output=OutputHint(name="savings_balance", type=ParamType.DECIMAL,
                                                         description="Current balance of the SAVINGS share, USD"),
                                       rationale="The shares grid row with Type=SAVINGS holds the savings balance.")
            if "MEMBER DETAIL" in text and (link := find("link", "Shares")):
                return AgentAction(type="click", ref=link["ref"], rationale="Balances are on the Shares tab.")
        else:
            if "share_id" in extracted and "confirmation_number" in extracted:
                return AgentAction(type="done", rationale="Share opened and confirmation captured.",
                                   capability=CapabilityHint(
                                       id="member.open_share", title="Open a new share (sub-account) for a member",
                                       description="Opens a share of the given type with an opening deposit and "
                                                   "returns the confirmation number. Posts a transaction."))
            if "SHARE OPENED" in text:
                for out, label, desc in (("confirmation_number", "Confirmation No", "Host confirmation number"),
                                         ("share_id", "Share", "Id of the newly opened share")):
                    if out not in extracted and (cell := find("cell", label=label)):
                        return AgentAction(type="extract", ref=cell["ref"], rationale=f"Read the {label}.",
                                           output=OutputHint(name=out, type=ParamType.STRING, description=desc,
                                                             sensitivity="internal"))
            if "VERIFY NEW SHARE" in text and (b := find("button", "Post")):
                return AgentAction(type="click", ref=b["ref"], rationale="Details match the goal; post the new share.")
            if "OPEN NEW SHARE" in text:
                if "MINIMUM OPENING DEPOSIT" in text:
                    return AgentAction(type="escalate", rationale="The host rejected the deposit amount.")
                sel = find("combobox", label="Share Type")
                dep = find("textbox", label="Opening Deposit")
                if sel and sel.get("value") in ("", "--"):
                    return AgentAction(type="select", ref=sel["ref"], value="{{inputs.share_type}}",
                                       rationale="Choose the requested share type.")
                if dep and not dep.get("value"):
                    return AgentAction(type="fill", ref=dep["ref"], value="{{inputs.opening_deposit}}",
                                       rationale="Enter the opening deposit.")
                if b := find("button", "Continue"):
                    return AgentAction(type="click", ref=b["ref"], rationale="Form complete; continue to verification.")
            if ("MEMBER DETAIL" in text or "MEMBER SHARES" in text) and (link := find("link", "Open Share")):
                return AgentAction(type="click", ref=link["ref"], rationale="Open Share is a tab on the member record.")

        # Shared prefix: find the member.
        if "MEMBER INQUIRY" in text:
            box = find("textbox", label="Member Number")
            if box and not box.get("value"):
                return AgentAction(type="fill", ref=box["ref"], value="{{inputs.member_number}}",
                                   rationale="Search for the member by number.")
            if box and (b := find("button", "Inquire")):
                return AgentAction(type="click", ref=b["ref"], rationale="Submit the member search.")
        if "TELLER HOME" in text and menu and (link := find("link", "Member Inquiry", frame=menu["elements"])):
            return AgentAction(type="click", ref=link["ref"], rationale="Member Inquiry is the entry point for member work.")
        headline = obs.primary_text[0] if obs.primary_text else "(blank)"
        return AgentAction(type="escalate", rationale=f"I don't recognise this screen ('{headline}') and can't "
                                                      "tell how to proceed safely.")
