"""Shared run plumbing for discovery and replay.

One Runtime = one live session against one tenant: browser surface, policy
gate, redactor, evidence log, control channel and operator routing. Both the
LLM-driven discovery loop and the deterministic replay engine sit on top of
it, so they get identical guardrails, evidence and handoff behaviour.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from .classify import Classifier, State
from .control import ControlChannel, Intervention, OperatorConsole, OperatorRouter, await_resolution
from .evidence import RunLog
from .policy import Policy, PolicyGate
from .profile import AppProfile, HandlerAction, Tenant
from .redact import Redactor
from .schema import Risk, render
from .surface import SurfaceError
from .surface.web import WebSurface

SESSION_SECRETS = ["operator_id", "operator_password"]


class PolicyDenied(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Runtime:
    def __init__(self, kind: str, tenant: Tenant, profile: AppProfile, policy: Policy, *,
                 operator: str = "none", headed: bool = False, classifier: Classifier | None = None):
        self.tenant, self.profile, self.policy = tenant, profile, policy
        self.redactor = Redactor(profile.sensitive_patterns)
        self.log = RunLog(kind, self.redactor)
        self.gate = PolicyGate(policy, profile, tenant)
        self.classifier = classifier or Classifier(profile)
        self.channel = ControlChannel(self.log.run_id, self.log.event)
        self.console = OperatorConsole(self.channel) if operator != "none" else None
        self.router = OperatorRouter(operator, self.console, self.log.dir)
        self.secrets = tenant.resolve_secrets(SESSION_SECRETS)
        for k, v in self.secrets.items():
            self.redactor.taint(v, f"secret:{k}")
        self.surface = WebSurface(
            tenant.base_url, self.gate, self.redactor,
            sensitive_labels=profile.sensitive_labels, sensitive_patterns=profile.sensitive_patterns,
            sensitive_columns=profile.sensitive_columns,
            headless=not headed,
            on_blocked_request=lambda url, why: self.log.event("policy_blocked_request", url=url, reason=why),
            on_human_event=lambda e: self.channel.record_page_event(self.redactor.obj(e)),
            on_dialog=lambda kind, msg: self.log.event("native_dialog_dismissed", dialog=kind, message=msg))
        self.epoch = 0
        self._evidence_n = 0

    # ---------------------------------------------------------------- session
    def start(self) -> None:
        self.surface.start()
        self.log.event("session_started", tenant=self.tenant.id, profile=f"{self.profile.id}@{self.profile.version}",
                       cdp_endpoint=self.surface.endpoint,
                       operator_console=self.console.url if self.console else None)
        self.epoch = self.channel.acquire()

    def sign_on(self) -> State:
        """Authenticate with tenant credentials. Runs outside any model loop: the
        planner never sees, chooses, or types a credential."""
        self.surface.goto(self.profile.session.signon_path)
        self.run_handler(self.profile.session.steps, {})
        # An interstitial (e.g. a broadcast notice) right after sign-on still means we are in.
        ok = lambda s: s.screen == self.profile.session.success_screen or s.interstitial is not None
        st = self.wait_for(ok, 10_000)
        if not ok(st):
            raise SurfaceError(f"sign-on did not reach {self.profile.session.success_screen}: {st.summary()}")
        self.log.event("signed_on", screen=st.screen)
        return st

    def close(self) -> None:
        self.router.close()
        self.surface.close()
        if self.console:
            self.console.close()
        self.log.close()

    # ------------------------------------------------------------------ state
    def classify(self) -> State:
        return self.classifier.classify(self.surface)

    def wait_for(self, pred, timeout_ms: int, poll_ms: int = 250) -> State:
        deadline = time.monotonic() + timeout_ms / 1000
        st = self.classify()
        while not pred(st) and time.monotonic() < deadline:
            self.surface.pump(poll_ms)
            st = self.classify()
        return st

    # ---------------------------------------------------------------- actions
    def act(self, action: str, resolved, value: str | None, *, declared: Risk | str | None = None,
            approved: bool = False) -> str | None:
        """The single choke point every automated action goes through: lease
        check, then policy, then the surface."""
        self.channel.check(self.epoch)
        e = resolved.element
        verdict = self.gate.check_action(action, e["role"], e.get("name", ""), e.get("attrs", {}).get("href"),
                                         declared)
        if verdict.decision == "deny" or (verdict.decision == "approve" and not approved):
            self.log.event("policy_denied", action=action, target=resolved.target_key, reason=verdict.reason,
                           risk=verdict.risk.value)
            raise PolicyDenied(verdict.reason)
        return self.surface.perform(action, resolved, value)

    def run_handler(self, actions: list[HandlerAction], inputs: dict[str, str]) -> None:
        for h in actions:
            r = self.surface.resolve(h.target, inputs)
            self.act(h.action, r, render(h.value, inputs, self.secrets))

    def handle_interstitial(self, inter, inputs: dict[str, str]) -> bool:
        """Clear a known interstitial using the profile's handler. True if it cleared."""
        gone = lambda s: not (s.interstitial and s.interstitial.id == inter.id)
        if inter.handler == "wait":
            return gone(self.wait_for(gone, inter.max_wait_ms))
        self.run_handler(inter.steps, inputs)
        return gone(self.wait_for(gone, 5000))

    # --------------------------------------------------------------- evidence
    def capture(self, tag: str) -> tuple[str, str]:
        """Masked screenshot + redacted semantic snapshot of all frames."""
        self._evidence_n += 1
        stem = f"evidence/{self._evidence_n:02d}-{tag}"
        shot = self.surface.screenshot(self.log.path(stem + ".png"))
        snap = self.log.write_json(stem + ".snapshot.json", self.surface.snapshot_all())
        rel = lambda p: os.path.relpath(p, self.log.dir)
        self.log.event("evidence_captured", tag=tag, screenshot=rel(shot), snapshot=rel(snap))
        return str(shot), str(snap)

    # ------------------------------------------------------------- escalation
    def escalate(self, kind: str, reason: str, context: dict, allowed: list[str]) -> Intervention | None:
        """Pause automation and hand the live session to a human. Returns the
        resolved intervention, or None if no operator channel is configured."""
        shot, snap = self.capture(f"escalation-{kind}")
        if not self.router.available:
            self.log.event("escalation_unavailable", kind=kind, reason=reason)
            return None
        iv = self.channel.raise_intervention(
            kind, self.redactor.text(reason), allowed, context=self.redactor.obj(context),
            screenshot=shot, snapshot=snap, cdp_endpoint=self.surface.endpoint)
        self.router.notify(iv)
        iv = await_resolution(self.channel, iv, self.surface.pump, self.policy.escalation_timeout_seconds)
        self.epoch = self.channel.acquire()
        self.log.write_json(f"interventions/{iv.id}.json", iv.model_dump(mode="json"))
        self.log.event("intervention_closed", intervention_id=iv.id, decision=iv.decision,
                       by=iv.claimed_by, human_actions=len(iv.human_actions))
        return iv


def load_env_file(path: str | Path) -> int:
    """Minimal KEY=VALUE loader so the demo needs no extra dependency."""
    p = Path(path)
    if not p.exists():
        return 0
    n = 0
    for line in p.read_text("utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
            n += 1
    return n
