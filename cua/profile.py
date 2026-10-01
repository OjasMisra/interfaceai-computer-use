"""App profiles and tenant bindings: the two layers around a capability.

    AppProfile   one per vendor product + major version ("acme-coreone@4").
                 What is true of the software for every institution running it:
                 screen fingerprints, interstitials (timeouts, notices) and how
                 to clear them, error signatures, business-outcome signatures,
                 how to sign on, which labels carry PII, which controls commit.
    Capability   one per task, recorded once against some tenant, refers to
                 screens by profile id.
    Tenant       one per institution app instance: base URL, which vault entry
                 backs each secret slot, and target overrides for local
                 configuration (relabelled fields, renamed tabs).

Replay = capability + profile + tenant. Nothing tenant-specific is baked into
the capability, so one recording serves every tenant on the same product line.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from pydantic import Field

from .schema import Model, OutcomeSpec, Risk, ScreenDef, Strategy, Target, TextMatch

ROOT = Path(__file__).resolve().parent.parent


class HandlerAction(Model):
    action: str  # click | fill
    target: Target
    value: str | None = None


class Interstitial(Model):
    """A known state that can appear in front of any screen and is not part of
    the task: cleared by acting (``act``) or by waiting it out (``wait``)."""
    id: str
    description: str
    match: TextMatch
    handler: str  # "act" | "wait"
    steps: list[HandlerAction] = Field(default_factory=list)
    max_occurrences: int = 3
    max_wait_ms: int = 15_000


class ErrorSignature(Model):
    id: str
    code: str
    description: str
    match: TextMatch
    retryable: bool = False


class RiskRule(Model):
    """Classify a control's risk from its role/name when the capability does not say."""
    role: str | None = None
    name_regex: str
    risk: Risk


class SessionProcedure(Model):
    signon_path: str
    steps: list[HandlerAction]
    success_screen: str


class AppProfile(Model):
    id: str
    version: str
    product: str
    surface: str = "web"
    primary_frame: list[str] = Field(description="Frame whose content defines the current screen")
    entry_path: str
    session: SessionProcedure
    screens: list[ScreenDef]
    interstitials: list[Interstitial] = Field(default_factory=list)
    errors: list[ErrorSignature] = Field(default_factory=list)
    outcomes: list[OutcomeSpec] = Field(default_factory=list)
    risk_rules: list[RiskRule] = Field(default_factory=list)
    sensitive_labels: list[str] = Field(default_factory=list)
    sensitive_patterns: list[str] = Field(default_factory=list)
    sensitive_columns: list[str] = Field(default_factory=list, description="Grid columns whose values are masked")

    def screen(self, sid: str) -> ScreenDef:
        return next(s for s in self.screens if s.id == sid)

    def classify_risk(self, role: str, name: str) -> Risk:
        for r in self.risk_rules:
            if (r.role is None or r.role == role) and re.search(r.name_regex, name or "", re.I):
                return Risk(r.risk)
        return Risk.SAFE if role in ("link", "cell", "textbox", "combobox") else Risk.REVERSIBLE


class TargetOverride(Model):
    strategies: list[Strategy]
    mode: str = "prepend"  # prepend | replace
    reason: str = ""


class Tenant(Model):
    id: str
    name: str
    profile: str
    profile_version: str
    base_url: str
    secrets: dict[str, str] = Field(description="secret slot -> env var (stand-in for a vault path)")
    target_overrides: dict[str, TargetOverride] = Field(default_factory=dict)
    mock_variant: str | None = Field(None, description="Demo only: which mock app variant serves this tenant")

    def resolve_secrets(self, needed: list[str]) -> dict[str, str]:
        out = {}
        for name in needed:
            env = self.secrets.get(name)
            if not env or env not in os.environ:
                raise KeyError(f"secret '{name}' not available for tenant {self.id} (set {env})")
            out[name] = os.environ[env]
        return out


def load_profile(pid: str, version: str) -> AppProfile:
    return AppProfile.model_validate_json((ROOT / "profiles" / f"{pid}@{version}.json").read_text("utf-8"))


def load_tenant(tid: str) -> Tenant:
    return Tenant.model_validate_json((ROOT / "tenants" / f"{tid}.json").read_text("utf-8"))


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text("utf-8"))
