"""The capability artifact: a typed, versioned, reviewable description of a flow.

A capability is what discovery produces and what replay executes. It is the
contract between three parties:

* the calling AI agent -- ``inputs`` / ``outputs`` / ``outcomes`` tell it what
  the capability needs, what it returns, and which business answers to expect;
* a human reviewer -- ``steps[].intent``, per-target ``strategies[].why`` and
  ``risk`` make the flow auditable without reading a model transcript;
* the replay engine -- ``steps`` with targets, preconditions and ``expect``
  checkpoints are everything it needs; no LLM is consulted.

What it deliberately does *not* contain: credentials (only named secret slots),
concrete PII (only ``{{inputs.*}}`` templates), tenant base URLs, and app-wide
exception handling (session timeouts, notices), which lives in the shared app
profile so every capability for that vendor product gets it for free.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_ID = "cua.capability/v1"
TEMPLATE_RE = re.compile(r"\{\{\s*(inputs|secrets)\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class Sensitivity(str, Enum):
    PUBLIC = "public"      # safe to log verbatim
    INTERNAL = "internal"  # bank-internal but not about a person (share ids, codes)
    PII = "pii"            # about a member: identifiers, balances, names -> masked in logs
    SECRET = "secret"      # credentials: never logged, never shown to the model


class Risk(str, Enum):
    SAFE = "safe"                  # read-only / navigation
    REVERSIBLE = "reversible"      # changes UI or draft state that can be undone
    IRREVERSIBLE = "irreversible"  # commits a transaction; needs approval, never auto-retried


class Status(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


# --------------------------------------------------------------- contract I/O

class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    ENUM = "enum"


class ParamSpec(Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: ParamType
    description: str
    required: bool = True
    pattern: str | None = Field(None, description="Regex the string form must fully match")
    enum: list[str] | None = None
    minimum: Decimal | None = None
    sensitivity: Sensitivity = Sensitivity.PII

    def coerce(self, raw: Any) -> str:
        """Validate a caller-supplied value; return its canonical string form."""
        s = str(raw).strip()
        if self.type == ParamType.INTEGER and not re.fullmatch(r"-?\d+", s):
            raise ValueError(f"{self.name}: expected integer")
        if self.type == ParamType.DECIMAL:
            try:
                d = Decimal(s.replace(",", "").lstrip("$"))
            except InvalidOperation:
                raise ValueError(f"{self.name}: expected decimal") from None
            if self.minimum is not None and d < self.minimum:
                raise ValueError(f"{self.name}: must be >= {self.minimum}")
            s = f"{d:.2f}"
        if self.type == ParamType.ENUM and s not in (self.enum or []):
            raise ValueError(f"{self.name}: must be one of {self.enum}")
        if self.pattern and not re.fullmatch(self.pattern, s):
            raise ValueError(f"{self.name}: does not match {self.pattern}")
        return s


class OutputSpec(Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: ParamType
    description: str
    required: bool = True
    sensitivity: Sensitivity = Sensitivity.PII

    def parse(self, text: str) -> Any:
        s = text.strip()
        if self.type == ParamType.DECIMAL:
            neg = s.endswith("-") or (s.startswith("(") and s.endswith(")"))
            try:
                d = Decimal(re.sub(r"[^\d.]", "", s))
            except InvalidOperation:
                raise ValueError(f"{self.name}: {s!r} is not a decimal") from None
            return str(-d if neg else d)  # decimals travel as strings: no float rounding on money
        if self.type == ParamType.INTEGER:
            if not re.fullmatch(r"-?\d+", s):
                raise ValueError(f"{self.name}: {s!r} is not an integer")
            return int(s)
        if not s:
            raise ValueError(f"{self.name}: empty")
        return s


class SecretSlot(Model):
    """A named credential the runtime injects. The value lives in the tenant's vault."""
    name: str
    description: str


# ------------------------------------------------------------------- targeting

class RoleName(Model):
    """Accessibility role + accessible name. Most stable when the app has names."""
    kind: Literal["role_name"] = "role_name"
    role: str
    name: str
    why: str = ""


class RoleLabel(Model):
    """Role + the visual label beside the control (legacy table layouts)."""
    kind: Literal["role_label"] = "role_label"
    role: str
    label: str
    why: str = ""


class Attribute(Model):
    """A stable markup attribute, e.g. generated form-field names or a canonical route.
    ``value`` may contain ``*`` wildcards (``/work/member/*/shares``)."""
    kind: Literal["attribute"] = "attribute"
    role: str | None = None
    attr: str
    value: str
    why: str = ""


class TableCell(Model):
    """A data-grid cell addressed by meaning, not position: the row whose
    ``row_match`` columns hold the given values, in column ``column``."""
    kind: Literal["table_cell"] = "table_cell"
    headers_include: list[str]
    row_match: dict[str, str]
    column: str
    why: str = ""


class CssPath(Model):
    """Structural path. Brittle; kept only as a last-resort fallback and drift signal."""
    kind: Literal["css_path"] = "css_path"
    css: str
    why: str = ""


Strategy = Annotated[Union[RoleName, RoleLabel, Attribute, TableCell, CssPath], Field(discriminator="kind")]


class Target(Model):
    """How to find one control. Strategies are tried in order; each must match
    exactly one element. A later strategy matching *instead of* the first is
    reported as drift, and two strategies matching *different* elements is an
    ambiguity failure rather than a guess."""
    key: str = Field(description="Stable id for this control, used by tenant overrides")
    frame: list[str] = Field(default_factory=list, description="Frame/window path from the top")
    strategies: list[Strategy] = Field(min_length=1)


# ---------------------------------------------------------------- checkpoints

class ScreenIs(Model):
    kind: Literal["screen"] = "screen"
    screen: str


class TextVisible(Model):
    kind: Literal["text"] = "text"
    frame: list[str] = Field(default_factory=list)
    regex: str


class FieldEquals(Model):
    """The control holds ``value`` (read back after a fill/select). ``target``
    defaults to the step's own target."""
    kind: Literal["field_equals"] = "field_equals"
    value: str  # template
    target: Target | None = None


Condition = Annotated[Union[ScreenIs, TextVisible, FieldEquals], Field(discriminator="kind")]


# ---------------------------------------------------------------------- steps

class Step(Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    intent: str = Field(description="What a human would say this step does")
    screen: str = Field(description="Precondition: screen the primary frame must show")
    action: Literal["click", "fill", "select", "press", "extract"]
    target: Target
    value: str | None = Field(None, description="Literal or {{inputs.x}} / {{secrets.y}} template")
    output: str | None = Field(None, description="For extract: which declared output this fills")
    risk: Risk = Risk.SAFE
    expect: list[Condition] = Field(default_factory=list, description="Postconditions, all must hold")
    timeout_ms: int = 10_000

    @model_validator(mode="after")
    def _shape(self):
        if self.action in ("fill", "select", "press") and self.value is None:
            raise ValueError(f"step {self.id}: {self.action} needs a value")
        if self.action == "extract" and not self.output:
            raise ValueError(f"step {self.id}: extract needs an output")
        return self

    @property
    def retry_safe(self) -> bool:
        return self.risk != Risk.IRREVERSIBLE


class TextMatch(Model):
    """Case-insensitive test against a frame's visible text."""
    all: list[str] = Field(default_factory=list)
    any: list[str] = Field(default_factory=list)
    regex: str | None = None

    def test(self, lines: list[str]) -> re.Match | bool:
        blob = "\n".join(lines).upper()
        if any(s.upper() not in blob for s in self.all):
            return False
        if self.any and not any(s.upper() in blob for s in self.any):
            return False
        if self.regex:
            return re.search(self.regex, "\n".join(lines), re.I | re.M) or False
        return bool(self.all or self.any)


class ScreenDef(Model):
    """Fingerprint of a screen. Normally lives in the app profile; a capability
    carries its own only for screens the profile does not know yet (promoted to
    the profile on review)."""
    id: str
    description: str = ""
    match: TextMatch


class OutcomeSpec(Model):
    """A business outcome the caller must handle: a legitimate answer, not a crash."""
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    description: str
    screens: list[str]
    regex: str = Field(description="Matched against the primary frame's visible text")


# ------------------------------------------------------------ lifecycle/meta

class AppBinding(Model):
    profile: str = Field(description="App profile id, e.g. acme-coreone")
    profile_version: str
    surface: Literal["web", "desktop"] = "web"
    start_screen: str = Field(description="Screen the session must be on before step 1")


class Provenance(Model):
    discovery_run_id: str
    goal: str = Field(description="Original goal with input values replaced by placeholders")
    planner: str
    recorded_at: datetime
    recorded_on_tenant: str
    compiler: str
    notes: list[str] = Field(default_factory=list)


class Approval(Model):
    by: str
    at: datetime
    content_sha256: str = Field(description="Approval is void if the reviewed content changes")
    note: str = ""


class Capability(Model):
    schema_: Literal["cua.capability/v1"] = Field(SCHEMA_ID, alias="schema")
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    title: str
    description: str
    status: Status = Status.DRAFT
    app: AppBinding
    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    secrets: list[SecretSlot] = Field(default_factory=list)
    outcomes: list[OutcomeSpec] = Field(default_factory=list)
    screens: list[ScreenDef] = Field(default_factory=list, description="Screens unknown to the profile")
    steps: list[Step] = Field(min_length=1)
    success: list[Condition] = Field(min_length=1)
    provenance: Provenance
    approvals: list[Approval] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)

    @field_validator("steps")
    @classmethod
    def _unique_ids(cls, steps):
        ids = [s.id for s in steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        return steps

    @model_validator(mode="after")
    def _references_resolve(self):
        """Every template and output reference must point at something declared."""
        names = {"inputs": {p.name for p in self.inputs}, "secrets": {s.name for s in self.secrets}}
        for blob in [self.model_dump_json(include={"steps", "success"})]:
            for ns, var in TEMPLATE_RE.findall(blob):
                if var not in names[ns]:
                    raise ValueError(f"template references undeclared {ns}.{var}")
        outs = {o.name for o in self.outputs}
        for s in self.steps:
            if s.output and s.output not in outs:
                raise ValueError(f"step {s.id} writes undeclared output {s.output}")
        missing = {o.name for o in self.outputs if o.required} - {s.output for s in self.steps if s.output}
        if missing:
            raise ValueError(f"required outputs never extracted: {sorted(missing)}")
        return self

    # -- derived views ---------------------------------------------------------
    @property
    def risk(self) -> Risk:
        order = [Risk.SAFE, Risk.REVERSIBLE, Risk.IRREVERSIBLE]
        return max((Risk(s.risk) for s in self.steps), key=order.index)

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def content_sha256(self) -> str:
        """Hash of everything that affects behaviour (not status/approvals)."""
        body = self.model_dump(mode="json", by_alias=True, exclude={"status", "approvals"})
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    def is_approved(self) -> bool:
        h = self.content_sha256()
        return self.status == Status.APPROVED and any(a.content_sha256 == h for a in self.approvals)

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True, indent=2, exclude_none=True)


def render(template: str | None, inputs: dict[str, str], secrets: dict[str, str] | None = None) -> str | None:
    """Fill ``{{inputs.x}}`` / ``{{secrets.y}}`` placeholders."""
    if template is None:
        return None
    pools = {"inputs": inputs, "secrets": secrets or {}}

    def sub(m: re.Match) -> str:
        ns, var = m.group(1), m.group(2)
        if var not in pools[ns]:
            raise KeyError(f"{ns}.{var} not provided")
        return pools[ns][var]

    return TEMPLATE_RE.sub(sub, template)
