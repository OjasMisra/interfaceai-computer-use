"""Agent-facing catalog: approved capabilities as callable tools.

Each capability becomes a tool definition (name, description, JSON-Schema
input) in the shape LLM tool-use APIs expect, so the agent product can
discover and invoke "lookup savings balance" by name with typed arguments.
The description carries the business outcomes, so the calling model knows
MEMBER_NOT_FOUND is an answer to relay, not an error to retry.
"""

from __future__ import annotations

from pathlib import Path

from .schema import Capability, ParamSpec

CAPS_DIR = Path(__file__).resolve().parent.parent / "capabilities"


def load_all() -> list[Capability]:
    caps = [Capability.model_validate_json(p.read_text("utf-8")) for p in sorted(CAPS_DIR.glob("*/*.json"))]
    return sorted(caps, key=lambda c: (c.id, tuple(map(int, c.version.split(".")))))


def find(ref: str) -> tuple[Capability, Path]:
    """Accepts a path, ``id@version``, or ``id`` (latest version)."""
    p = Path(ref)
    if p.suffix == ".json" and p.exists():
        return Capability.model_validate_json(p.read_text("utf-8")), p
    cid, _, ver = ref.partition("@")
    d = CAPS_DIR / cid
    if not d.exists():
        raise FileNotFoundError(f"no capability {cid}")
    if not ver:
        ver = max((p.stem for p in d.glob("*.json")), key=lambda v: tuple(map(int, v.split("."))))
    p = d / f"{ver}.json"
    return Capability.model_validate_json(p.read_text("utf-8")), p


def tool_name(cap: Capability) -> str:
    return cap.id.replace(".", "__")


def _schema(p: ParamSpec) -> dict:
    s: dict = {"description": p.description}
    if p.type == "enum":
        s.update(type="string", enum=p.enum)
    elif p.type == "decimal":
        s.update(type="string", pattern=r"^\d+(\.\d{1,2})?$", description=p.description + " (decimal as string)")
    elif p.type == "integer":
        s.update(type="integer")
    else:
        s.update(type="string")
        if p.pattern:
            s["pattern"] = f"^{p.pattern}$"
    return s


def tool_definition(cap: Capability) -> dict:
    outcomes = "; ".join(f"{o.code} ({o.description})" for o in cap.outcomes) or "none"
    outputs = ", ".join(f"{o.name}: {o.type}" for o in cap.outputs)
    return {
        "name": tool_name(cap),
        "description": (f"{cap.title}. {cap.description} Returns {{{outputs}}}. "
                        f"May instead return a business outcome: {outcomes}. Risk: {cap.risk.value}."),
        "input_schema": {"type": "object", "properties": {p.name: _schema(p) for p in cap.inputs},
                         "required": [p.name for p in cap.inputs if p.required], "additionalProperties": False},
        "x-capability": cap.ref,
        "x-status": cap.status,
        "x-approved": cap.is_approved(),
    }


def catalog(approved_only: bool = True) -> list[dict]:
    latest: dict[str, Capability] = {}
    for c in load_all():
        if c.is_approved() or not approved_only:
            latest[c.id] = c
    return [tool_definition(c) for c in latest.values()]
