"""Compile a successful discovery trace into a capability artifact.

The trace is what the planner did, step by step, with the (redacted)
observation it acted on. Compilation turns each action into a Step with:

* a Target with several independent strategies, ranked by expected stability
  and each verified to be *unique* on the screen it was recorded on;
* a precondition screen and postcondition checkpoints derived from what the
  app actually showed before and after;
* values as ``{{inputs.*}}`` templates, never literals from the run.

Actions that were not part of the task are left out and noted: steps a human
performed during a handoff, and anything done on an interstitial.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from ..policy import PolicyGate
from ..profile import AppProfile
from ..schema import (AppBinding, Attribute, Capability, CssPath, FieldEquals, OutputSpec, ParamSpec, Provenance,
                      RoleLabel, RoleName, ScreenDef, ScreenIs, Sensitivity, Step, TableCell, Target, TextMatch)
from ..surface.web import WebSurface
from .planner import AgentAction, InputHint

COMPILER = "cua-compiler/1"
CAPS_DIR = Path(__file__).resolve().parents[2] / "capabilities"


@dataclass
class TraceEntry:
    n: int
    action: AgentAction
    element: dict
    frame: list[str]
    frame_elements: list[dict]
    screen_before: str | None
    headline: list[str]
    risk: str
    approved_by: str | None = None
    screen_after: str | None = None


@dataclass
class HumanEntry:
    n: int
    intervention_id: str
    kind: str
    screen: str | None
    actions: list[dict] = field(default_factory=list)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")[:40] or "x"


def _placeholders(inputs: list[InputHint]) -> dict[str, str]:
    """Redacted observations show inputs as «name»; map them back to templates."""
    return {f"«{h.name}»": f"{{{{inputs.{h.name}}}}}" for h in inputs}


def _templatise(s: str, ph: dict[str, str]) -> str:
    for k, v in ph.items():
        s = s.replace(k, v)
    return s


def _unique(strategy, frame_elements: list[dict], redacted_inputs: dict[str, str]) -> bool:
    return sum(WebSurface._matches(strategy, e, redacted_inputs) for e in frame_elements) == 1


def build_target(entry: TraceEntry, inputs: list[InputHint], notes: list[str]) -> Target:
    e, a = entry.element, entry.action
    ph = _placeholders(inputs)
    red_inputs = {h.name: f"«{h.name}»" for h in inputs}
    role, name, label, attrs = e["role"], e.get("name", ""), e.get("label", ""), e.get("attrs", {})
    cands = []
    if role in ("button", "link") and name:
        cands.append(RoleName(role=role, name=_templatise(name, ph),
                              why="accessible name; what an operator reads, stable across layout changes"))
    table = e.get("table") or {}
    if role == "cell" and table.get("headers") and table.get("row_values"):
        headers, row = table["headers"], table["row_values"]
        keys = a.row_key or [h for h in headers if h != table["header"] and row.count(row[headers.index(h)]) == 1][:1]
        cands.append(TableCell(headers_include=headers, column=table["header"],
                               row_match={k: _templatise(row[headers.index(k)], ph) for k in keys},
                               why=f"row addressed by its key column(s) {keys}, not position: row order varies per record"))
    in_grid = bool(table.get("headers") and table.get("row_values"))
    if (role in ("textbox", "combobox", "checkbox", "radio") or (role == "cell" and not in_grid)) and label:
        cands.append(RoleLabel(role=role, label=label,
                               why="no accessible name (table layout); label is the text in the adjacent cell"))
    if attrs.get("name"):
        cands.append(Attribute(role=role, attr="name", value=attrs["name"],
                               why="server-generated field name: opaque but fixed per product version"))
    if attrs.get("href"):
        cands.append(Attribute(role=role, attr="href", value=_templatise(attrs["href"], ph),
                               why="canonical route with record ids parameterised"))
    if a.type != "extract":
        # Never for data reads: if the semantic strategies miss, a positional
        # fallback would silently return some *other* row's value.
        cands.append(CssPath(css=e["css"], why="structural fallback for actions; mainly a drift signal"))

    kept = []
    for s in cands:
        blob = s.model_dump_json()
        if "«" in blob:  # a redacted value that is not an input: would leak or never match
            notes.append(f"step {entry.n}: dropped {s.kind} strategy containing a redacted value")
        elif not _unique(s, entry.frame_elements, red_inputs):
            notes.append(f"step {entry.n}: dropped {s.kind} strategy, not unique on the recorded screen")
        else:
            kept.append(s)
    key_name = (a.output.name if a.output else None) or label or name or e["tag"]
    return Target(key=f"{entry.screen_before or 'screen'}.{slug(key_name)}", frame=entry.frame, strategies=kept)


def _intent(entry: TraceEntry) -> str:
    a, e = entry.action, entry.element
    what = e.get("label") or e.get("name") or e["tag"]
    if a.type == "click":
        return f"Click the '{e.get('name')}' {e['role']}"
    if a.type in ("fill", "select"):
        return f"{'Enter' if a.type == 'fill' else 'Choose'} {a.value} in '{what}'"
    t = e.get("table") or {}
    if t.get("header") and a.row_key:
        row = {k: t["row_values"][t["headers"].index(k)] for k in a.row_key}
        return f"Read {a.output.name} from column '{t['header']}' of the row where {row}"
    return f"Read {a.output.name} from '{what}'"


def _step_id(entry: TraceEntry) -> str:
    a, e = entry.action, entry.element
    if a.type == "extract":
        return f"read_{a.output.name}"
    verb = {"click": "click", "fill": "enter", "select": "choose"}[a.type]
    return f"{verb}_{slug(e.get('label') or e.get('name'))}"


def _input_spec(h: InputHint) -> ParamSpec:
    return ParamSpec(name=h.name, type=h.type, description=h.description, pattern=h.pattern, enum=h.enum,
                     minimum=Decimal(h.minimum) if h.minimum else None,
                     sensitivity=Sensitivity.PII if h.name == "member_number" or h.type == "decimal" else Sensitivity.INTERNAL)


def next_version(cap_id: str, contract: tuple) -> str:
    d = CAPS_DIR / cap_id
    versions = sorted((tuple(map(int, p.stem.split("."))) for p in d.glob("*.json")), reverse=True) if d.exists() else []
    if not versions:
        return "1.0.0"
    latest = versions[0]
    prev = Capability.model_validate_json((d / f"{'.'.join(map(str, latest))}.json").read_text("utf-8"))
    prev_contract = (tuple((p.name, p.type) for p in prev.inputs), tuple((o.name, o.type) for o in prev.outputs))
    if prev_contract != contract:
        return f"{latest[0] + 1}.0.0"  # caller-visible contract changed
    return f"{latest[0]}.{latest[1] + 1}.0"


def compile_capability(trace: list, inputs: list[InputHint], done: AgentAction, final_screen: str | None,
                       final_headline: list[str], profile: AppProfile, gate: PolicyGate, *, run_id: str,
                       planner: str, goal: str, tenant_id: str) -> tuple[Capability, list[str]]:
    notes: list[str] = []
    entries = [t for t in trace if isinstance(t, TraceEntry)]
    for t in trace:
        if isinstance(t, HumanEntry):
            notes.append(f"turn {t.n}: human intervention {t.intervention_id} ({t.kind}) on screen "
                         f"{t.screen or 'unknown'} with {len(t.actions)} action(s) -- not recorded as steps; "
                         f"if this recurs, model it as a profile interstitial")
    for k, t in enumerate(entries):
        t.screen_after = entries[k + 1].screen_before if k + 1 < len(entries) else final_screen

    local_screens: dict[str, ScreenDef] = {}

    def screen_id(sid: str | None, headline: list[str]) -> str:
        if sid:
            return sid
        title = re.sub(r"\s+OPR\s+\S+$", "", headline[0] if headline else "unknown")
        sd = ScreenDef(id=f"auto_{slug(title)}", description="Synthesised at compile time; promote to profile",
                       match=TextMatch(all=[title]))
        local_screens[sd.id] = sd
        notes.append(f"screen '{title}' unknown to profile; synthesised {sd.id}")
        return sd.id

    steps, used_ids = [], set()
    for t in entries:
        t.screen_before = screen_id(t.screen_before, t.headline)
        target = build_target(t, inputs, notes)
        sid = _step_id(t)
        while sid in used_ids:
            sid += "_2"
        used_ids.add(sid)
        expect = []
        if t.action.type in ("fill", "select"):
            expect.append(FieldEquals(value=t.action.value))
        elif t.action.type == "click" and t.screen_after and t.screen_after != t.screen_before:
            expect.append(ScreenIs(screen=t.screen_after))
        steps.append(Step(id=sid, intent=_intent(t), screen=t.screen_before, action=t.action.type, target=target,
                          value=t.action.value if t.action.type != "click" else None,
                          output=t.action.output.name if t.action.output else None, risk=t.risk, expect=expect))
        if t.approved_by:
            notes.append(f"step {sid}: irreversible; approved during discovery by {t.approved_by}")

    outputs = [OutputSpec(name=t.action.output.name, type=t.action.output.type,
                          description=t.action.output.description, sensitivity=t.action.output.sensitivity)
               for t in entries if t.action.output]
    traversed = {s.screen for s in steps} | ({final_screen} if final_screen else set())
    outcomes = [o for o in profile.outcomes if set(o.screens) & traversed]
    final = screen_id(final_screen, final_headline)
    hint = done.capability
    contract = (tuple((h.name, h.type) for h in inputs), tuple((o.name, o.type) for o in outputs))
    cap = Capability(
        id=hint.id, version=next_version(hint.id, contract), title=hint.title, description=hint.description,
        app=AppBinding(profile=profile.id, profile_version=profile.version, surface=profile.surface,
                       start_screen=steps[0].screen),
        inputs=[_input_spec(h) for h in inputs], outputs=outputs, outcomes=outcomes,
        screens=list(local_screens.values()), steps=steps, success=[ScreenIs(screen=final)],
        provenance=Provenance(discovery_run_id=run_id, goal=goal, planner=planner,
                              recorded_at=datetime.now(timezone.utc), recorded_on_tenant=tenant_id,
                              compiler=COMPILER, notes=notes))
    return cap, notes


def save(cap: Capability) -> Path:
    p = CAPS_DIR / cap.id / f"{cap.version}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(cap.to_json() + "\n", encoding="utf-8")
    return p
