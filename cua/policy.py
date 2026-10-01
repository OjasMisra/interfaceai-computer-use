"""Guardrails, enforced where actions happen, not in a prompt.

* URL allowlist: origin must be the tenant's, path must match ``allowed_paths``
  and not ``blocked_paths``. Enforced twice: at the network layer (every
  request from every frame, including redirects and anything a human does in
  the shared session) and before navigation-type actions.
* Action allowlist: only declared action types may be performed.
* Risk: the effective risk of an action is the *maximum* of what the artifact
  declares and what the app profile's rules say about the control. An artifact
  can raise risk but never lower it, so a hand-edited "safe" Post button still
  needs approval.
* Irreversible actions are never performed autonomously. Discovery escalates to
  a human; replay requires an approved capability *and* either an
  invocation-time pre-approval for that step or a live human approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field

from .profile import AppProfile, Tenant
from .schema import Model, Risk

ORDER = [Risk.SAFE, Risk.REVERSIBLE, Risk.IRREVERSIBLE]


class Policy(Model):
    allowed_paths: list[str]
    blocked_paths: list[str] = Field(default_factory=list)
    allowed_actions: list[str]
    max_steps: int = 30
    max_seconds: int = 240
    irreversible: dict[str, str] = Field(default_factory=dict)
    escalation_timeout_seconds: int = 600

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        return cls.model_validate_json(Path(path).read_text("utf-8"))


@dataclass
class Verdict:
    decision: str  # allow | deny | approve
    risk: Risk
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


class PolicyGate:
    def __init__(self, policy: Policy, profile: AppProfile, tenant: Tenant):
        self.policy = policy
        self.profile = profile
        self.origin = urlparse(tenant.base_url)._replace(path="", query="", fragment="").geturl()

    def check_url(self, url: str) -> Verdict:
        u = urlparse(url)
        if u.scheme in ("about", "data") or url == "about:blank":
            return Verdict("allow", Risk.SAFE)
        origin = f"{u.scheme}://{u.netloc}"
        if origin != self.origin:
            return Verdict("deny", Risk.SAFE, f"origin {origin} is not the tenant origin")
        path = u.path or "/"
        if any(fnmatch(path, p) for p in self.policy.blocked_paths):
            return Verdict("deny", Risk.SAFE, f"path {path} is explicitly blocked")
        if not any(fnmatch(path, p) for p in self.policy.allowed_paths):
            return Verdict("deny", Risk.SAFE, f"path {path} is not in the allowlist")
        return Verdict("allow", Risk.SAFE)

    def effective_risk(self, role: str, name: str, declared: Risk | str | None = None) -> Risk:
        classified = self.profile.classify_risk(role, name)
        declared = Risk(declared) if declared else Risk.SAFE
        return max(classified, declared, key=ORDER.index)

    def check_action(self, action: str, role: str, name: str, href: str | None = None,
                     declared: Risk | str | None = None) -> Verdict:
        if action not in self.policy.allowed_actions:
            return Verdict("deny", Risk.SAFE, f"action type '{action}' is not allowed")
        if href:
            v = self.check_url(self.origin + href if href.startswith("/") else href)
            if not v.allowed:
                return Verdict("deny", Risk.SAFE, f"link target denied: {v.reason}")
        risk = self.effective_risk(role, name, declared) if action != "extract" else Risk.SAFE
        if risk == Risk.IRREVERSIBLE:
            return Verdict("approve", risk, f"'{name}' is an irreversible {role} action")
        return Verdict("allow", risk)
