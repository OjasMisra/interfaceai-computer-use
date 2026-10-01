"""Deterministic replay: the production execution path. No LLM anywhere in here.

Contract
--------
``ReplayEngine(capability, tenant, ...).run(inputs) -> ReplayResult`` where
``status`` is exactly one of:

    succeeded         outputs are populated and the success checkpoint held
    business_outcome  the app gave a legitimate, declared answer the caller must
                      handle (MEMBER_NOT_FOUND, ACCESS_RESTRICTED, ...). Not an error.
    rejected          refused before touching the UI: bad input, unapproved
                      capability, profile mismatch. Nothing happened in the app.
    failed            a hard failure. ``failure`` says which step, what was
                      expected, what was observed, whether a retry could help,
                      and where the evidence is.

Recoverable conditions (session timeout, notices, slow host) never surface as a
status: they are handled inside the run and listed in ``recoveries``.

Execution model
---------------
Each step has a precondition screen and postcondition checkpoints. After every
action the engine classifies the state (see ``classify``) and either proceeds,
recovers, returns a business outcome, fails, or -- if the state is unknown --
escalates to a human. After any recovery or handoff it *repositions*: it finds
where in the flow the current screen belongs instead of assuming, and it will
never reposition to before an irreversible step it already executed.
"""

from __future__ import annotations

import re
import time
from typing import Any

from pydantic import Field

from .classify import Classifier, State
from .control import ControlLost, await_resolution
from .policy import ORDER, Policy
from .profile import AppProfile, Tenant, load_profile
from .redact import Redactor
from .runtime import PolicyDenied, Runtime
from .schema import Capability, FieldEquals, Model, Risk, ScreenIs, Step, TextVisible, render
from .surface import SurfaceError, TargetAmbiguous, TargetBlocked, TargetNotFound

MAX_INTERVENTIONS = 3


class Failure(Model):
    code: str
    message: str
    step_id: str | None = None
    step_index: int | None = None
    step_intent: str | None = None
    expected: Any = None
    observed: Any = None
    retryable: bool = False
    evidence: list[str] = Field(default_factory=list)


class ReplayResult(Model):
    run_id: str | None = None
    capability: str
    tenant: str
    status: str  # succeeded | business_outcome | rejected | failed
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome: dict | None = None
    failure: Failure | None = None
    recoveries: list[dict] = Field(default_factory=list)
    interventions: list[dict] = Field(default_factory=list)
    drift: list[dict] = Field(default_factory=list)
    overrides_applied: list[str] = Field(default_factory=list)
    human_assisted: bool = False
    steps_completed: int = 0
    duration_ms: int = 0
    evidence_dir: str | None = None


class _Stop(Exception):
    """Terminates the run with a final result."""
    def __init__(self, status: str, *, failure: Failure | None = None, outcome: dict | None = None):
        self.status, self.failure, self.outcome = status, failure, outcome


class _Reposition(Exception):
    pass


class _Unexpected(Exception):
    def __init__(self, state: State, expected: Any):
        self.state, self.expected = state, expected


def apply_overrides(cap: Capability, tenant: Tenant) -> tuple[Capability, list[str]]:
    """Specialise a shared capability for one tenant without editing it."""
    if not tenant.target_overrides:
        return cap, []
    data = cap.model_dump(mode="json", by_alias=True)
    applied: list[str] = []

    def patch(t: dict) -> None:
        o = tenant.target_overrides.get(t["key"])
        if o:
            new = [s.model_dump(mode="json") for s in o.strategies]
            t["strategies"] = new if o.mode == "replace" else new + t["strategies"]
            applied.append(f"{t['key']}: {o.reason}")

    for s in data["steps"]:
        patch(s["target"])
        for c in s.get("expect", []):
            if c["kind"] == "field_equals" and c.get("target"):
                patch(c["target"])
    return Capability.model_validate(data), sorted(set(applied))


class ReplayEngine:
    def __init__(self, cap: Capability, tenant: Tenant, policy: Policy, *, profile: AppProfile | None = None,
                 operator: str = "none", headed: bool = False, preapproved: tuple[str, ...] = (),
                 allow_draft: bool = False):
        self.base = cap
        self.tenant = tenant
        self.policy = policy
        self.profile = profile or load_profile(cap.app.profile, cap.app.profile_version)
        self.operator = operator
        self.headed = headed
        self.preapproved = set(preapproved)
        self.allow_draft = allow_draft

    # ------------------------------------------------------------- pre-flight
    def _preflight(self, raw: dict[str, Any]) -> dict[str, str]:
        cap = self.base
        errors = []
        inputs: dict[str, str] = {}
        for p in cap.inputs:
            if p.name not in raw:
                if p.required:
                    errors.append(f"{p.name}: required")
                continue
            try:
                inputs[p.name] = p.coerce(raw[p.name])
            except ValueError as e:
                errors.append(str(e))
        unknown = set(raw) - {p.name for p in cap.inputs}
        errors += [f"{u}: not a declared input" for u in sorted(unknown)]
        if errors:
            raise _Stop("rejected", failure=Failure(code="INPUT_INVALID", message="; ".join(errors),
                                                    expected={p.name: p.model_dump(exclude_none=True) for p in cap.inputs}))
        if (self.tenant.profile, self.tenant.profile_version) != (cap.app.profile, cap.app.profile_version):
            raise _Stop("rejected", failure=Failure(
                code="PROFILE_MISMATCH",
                message=f"tenant runs {self.tenant.profile}@{self.tenant.profile_version}, capability needs "
                        f"{cap.app.profile}@{cap.app.profile_version}"))
        if not cap.is_approved():
            if cap.risk == Risk.IRREVERSIBLE:
                raise _Stop("rejected", failure=Failure(
                    code="NOT_APPROVED", message="capability has irreversible steps and is not approved "
                                                 "(or was modified after approval)"))
            if not self.allow_draft:
                raise _Stop("rejected", failure=Failure(
                    code="NOT_APPROVED", message=f"capability status is {cap.status}; approve it or pass --allow-draft"))
        return inputs

    # -------------------------------------------------------------------- run
    def run(self, raw_inputs: dict[str, Any]) -> ReplayResult:
        t0 = time.monotonic()
        res = ReplayResult(capability=self.base.ref, tenant=self.tenant.id, status="failed")
        try:
            inputs = self._preflight(raw_inputs)
        except _Stop as s:
            res.status, res.failure = s.status, s.failure
            return res

        cap, res.overrides_applied = apply_overrides(self.base, self.tenant)
        self.cap = cap
        rt = Runtime("replay", self.tenant, self.profile, self.policy, operator=self.operator, headed=self.headed,
                     classifier=Classifier(self.profile, cap.screens, cap.outcomes))
        self.rt, self.res, self.inputs = rt, res, inputs
        res.run_id, res.evidence_dir = rt.log.run_id, str(rt.log.dir)
        for p in cap.inputs:
            if p.sensitivity in ("pii", "secret") and p.name in inputs:
                rt.redactor.taint(inputs[p.name], p.name)
        rt.log.event("replay_started", capability=cap.ref, status=self.base.status, tenant=self.tenant.id,
                     inputs={k: f"«{k}»" for k in inputs}, overrides=res.overrides_applied,
                     preapproved=sorted(self.preapproved))
        try:
            rt.start()
            rt.sign_on()
            rt.surface.goto(self.profile.entry_path)
            self._execute()
            res.status = "succeeded"
        except _Stop as s:
            res.status, res.failure, res.outcome = s.status, s.failure, s.outcome
        except SurfaceError as e:
            res.status, res.failure = "failed", self._failure(e.code, str(e))
        except Exception as e:  # anything unforeseen is a hard failure with evidence, never a hang
            res.status, res.failure = "failed", self._failure("INTERNAL_ERROR", f"{type(e).__name__}: {e}")
        finally:
            res.duration_ms = int((time.monotonic() - t0) * 1000)
            res.interventions = [
                {"id": iv.id, "kind": iv.kind, "decision": iv.decision, "by": iv.claimed_by, "note": iv.note,
                 "human_actions": iv.human_actions} for iv in rt.channel.interventions.values()]
            res.human_assisted = any(iv.decision in ("resume", "approve") for iv in rt.channel.interventions.values())
            rt.log.event("replay_finished", status=res.status, outcome=res.outcome,
                         failure=res.failure.model_dump() if res.failure else None,
                         outputs={o.name: Redactor.value(res.outputs.get(o.name), o.sensitivity, o.name)
                                  for o in cap.outputs if o.name in res.outputs},
                         recoveries=len(res.recoveries), drift=len(res.drift), duration_ms=res.duration_ms)
            persisted = res.model_dump(mode="json")
            persisted["outputs"] = {o.name: Redactor.value(res.outputs[o.name], o.sensitivity, o.name)
                                    for o in cap.outputs if o.name in res.outputs}
            rt.log.write_json("result.json", persisted)
            rt.close()
        return res

    # --------------------------------------------------------------- the loop
    def _execute(self) -> None:
        cap, rt = self.cap, self.rt
        self.last_irreversible = -1
        self.seen: dict[str, int] = {}
        i, n = 0, len(cap.steps)
        while i < n:
            step = cap.steps[i]
            self.i = i
            try:
                self._run_step(step, i)
                rt.log.event("step_completed", index=i, step=step.id)
                self.res.steps_completed = i + 1
                i += 1
            except _Reposition:
                i = self._reposition(i, "recovery")
            except TargetBlocked:
                st = rt.classify()
                if st.interstitial:
                    self._recover(st.interstitial, step)
                    i = self._reposition(i, "recovery")
                else:
                    i = self._handoff(i, "unexpected_state", f"step {step.id}: target is covered by an unknown element",
                                      st, {"target": step.target.key})
            except _Unexpected as u:
                after_irrev = self.last_irreversible == i
                i = self._handoff(i, "failure" if after_irrev else "unexpected_state",
                                  ("result of irreversible step is unconfirmed; do NOT retry blindly. "
                                   if after_irrev else "") + f"step {step.id}: unrecognised state",
                                  u.state, u.expected)
            except ControlLost:
                iv = rt.channel.active()
                rt.log.event("automation_paused", reason="operator took control", intervention_id=iv.id if iv else None)
                if iv:
                    iv = await_resolution(rt.channel, iv, rt.surface.pump, self.policy.escalation_timeout_seconds)
                    rt.epoch = rt.channel.acquire()
                    if iv.decision == "abort":
                        raise _Stop("failed", failure=self._failure("ABORTED_BY_OPERATOR", iv.note or "operator aborted"))
                else:
                    rt.epoch = rt.channel.acquire()
                i = self._reposition(i, "takeover")
            except PolicyDenied as e:
                raise _Stop("failed", failure=self._failure("POLICY_DENIED", e.reason, step=step))
        self._verify_success()

    def _run_step(self, step: Step, i: int) -> None:
        rt = self.rt
        self._reach(step, lambda s: s.screen == step.screen, {"screen": step.screen})
        r = self._resolve(step)
        for w in r.warnings:
            self.res.drift.append({"step": step.id, "target": step.target.key, "warning": w})
            rt.log.event("locator_drift", step=step.id, warning=w)
        effective = rt.gate.effective_risk(r.element["role"], r.element.get("name", ""), step.risk)
        approved = effective != Risk.IRREVERSIBLE or self._approve(step, i)
        value = render(step.value, self.inputs, rt.secrets)
        rt.log.event("step_action", index=i, step=step.id, action=step.action, target=step.target.key,
                     strategy=r.strategy_kind, value_template=step.value, risk=effective.value)
        if effective == Risk.IRREVERSIBLE:
            self.last_irreversible = i
        out = rt.act(step.action, r, value, declared=step.risk, approved=approved)
        if step.action == "extract":
            spec = next(o for o in self.cap.outputs if o.name == step.output)
            try:
                self.res.outputs[spec.name] = spec.parse(out or "")
            except ValueError as e:
                raise _Stop("failed", failure=self._failure("EXTRACTION_FAILED", str(e), step=step,
                                                            expected={"type": spec.type}))
            rt.redactor.taint(out.strip(), spec.name)
            rt.redactor.taint(self.res.outputs[spec.name], spec.name)
        for cond in step.expect:
            self._check(step, cond)

    # ---------------------------------------------------- state & checkpoints
    def _dispatch(self, st: State, step: Step, expected: Any) -> None:
        """Act on any state that is not a plain screen. Raises; returns only for screens/unknown."""
        rt = self.rt
        if st.error:
            raise _Stop("failed", failure=self._failure(
                st.error.code, f"{st.error.description}: {st.error_detail or ''}".strip(), step=step,
                expected=expected, observed=st.summary(), retryable=st.error.retryable))
        if st.interstitial:
            self._recover(st.interstitial, step)
            raise _Reposition()
        if st.outcome:
            rt.log.event("business_outcome", code=st.outcome.code, message=st.outcome_message, step=step.id)
            rt.capture(f"outcome-{st.outcome.code.lower()}")
            raise _Stop("business_outcome", outcome={"code": st.outcome.code, "description": st.outcome.description,
                                                     "message": st.outcome_message})

    def _reach(self, step: Step, want, expected: Any) -> State:
        """Poll until ``want(state)``; deal with everything else on the way. A
        state that stays unrecognised past the step timeout is ``_Unexpected``."""
        deadline = time.monotonic() + step.timeout_ms / 1000
        while True:
            st = self.rt.classify()
            self._dispatch(st, step, expected)
            if want(st):
                return st
            if time.monotonic() > deadline:
                raise _Unexpected(st, expected)
            self.rt.surface.pump(200)

    def _check(self, step: Step, cond) -> None:
        if isinstance(cond, ScreenIs):
            self._reach(step, lambda s: s.screen == cond.screen, {"screen": cond.screen})
        elif isinstance(cond, TextVisible):
            frame_text = lambda: "\n".join(self.rt.surface.frame_text(cond.frame) or [])
            self._reach(step, lambda s: bool(re.search(cond.regex, frame_text(), re.I)), {"text": cond.regex})
        elif isinstance(cond, FieldEquals):
            want = render(cond.value, self.inputs, self.rt.secrets)
            target = cond.target or step.target
            got = self.rt.surface.read_value(self.rt.surface.resolve(target, self.inputs))
            if got.strip() != (want or "").strip():
                raise _Stop("failed", failure=self._failure("CHECKPOINT_FAILED", f"{target.key} did not take the value",
                                                            step=step, expected={"field": target.key, "value": cond.value},
                                                            observed={"value": got}, retryable=True))

    def _resolve(self, step: Step):
        """Resolve a target, tolerating late rendering up to the step timeout."""
        deadline = time.monotonic() + min(step.timeout_ms, 5000) / 1000
        while True:
            try:
                return self.rt.surface.resolve(step.target, self.inputs)
            except TargetAmbiguous as e:
                raise _Stop("failed", failure=self._failure(e.code, str(e), step=step,
                                                            expected=step.target.model_dump(), observed=self.rt.classify().summary()))
            except TargetNotFound as e:
                if time.monotonic() > deadline:
                    st = self.rt.classify()
                    self._dispatch(st, step, {"screen": step.screen})
                    if st.screen != step.screen:
                        raise _Unexpected(st, {"screen": step.screen})
                    raise _Stop("failed", failure=self._failure(e.code, str(e), step=step,
                                                                expected=step.target.model_dump(), observed=st.summary()))
                self.rt.surface.pump(250)

    def _verify_success(self) -> None:
        last = self.cap.steps[-1]
        for cond in self.cap.success:
            self._check(last, cond)
        missing = [o.name for o in self.cap.outputs if o.required and o.name not in self.res.outputs]
        if missing:
            raise _Stop("failed", failure=self._failure("CHECKPOINT_FAILED", f"required outputs missing: {missing}"))
        self.rt.log.event("success_checkpoint_passed", conditions=[c.model_dump() for c in self.cap.success])

    # --------------------------------------------------------------- recovery
    def _recover(self, inter, step: Step) -> None:
        rt = self.rt
        n = self.seen[inter.id] = self.seen.get(inter.id, 0) + 1
        if n > inter.max_occurrences:
            raise _Stop("failed", failure=self._failure("RECOVERY_EXHAUSTED",
                                                        f"{inter.id} occurred {n} times", step=step, retryable=True))
        rec = {"step": step.id, "interstitial": inter.id, "handler": inter.handler, "attempt": n}
        rt.log.event("recovery_started", **rec)
        if not rt.handle_interstitial(inter, self.inputs):
            raise _Stop("failed", failure=self._failure(
                "TIMEOUT" if inter.handler == "wait" else "RECOVERY_FAILED", f"{inter.id} did not clear",
                step=step, observed=rt.classify().summary(), retryable=True))
        rec["ok"] = True
        self.res.recoveries.append(rec)
        rt.log.event("recovery_succeeded", **rec)

    def _reposition(self, i: int, why: str) -> int:
        """Find where the current screen sits in the flow. Steps on one screen form
        a block (fills then a submit); resume at the start of the latest block, at
        or before i+1, that matches -- never before an executed irreversible step."""
        rt, steps = self.rt, self.cap.steps
        st = rt.wait_for(lambda s: s.kind in ("screen", "outcome", "error"), 5000)
        if st.kind != "screen":
            return i  # the next _reach on step i will deal with the outcome/error
        last = min(i + 1, len(steps) - 1)
        starts = [j for j in range(last + 1)
                  if steps[j].screen == st.screen and (j == 0 or steps[j - 1].screen != st.screen)]
        if not starts:
            raise _Unexpected(st, {"screen": steps[i].screen})
        j = max(starts)
        if j <= self.last_irreversible:
            raise _Stop("failed", failure=self._failure(
                "INDETERMINATE_AFTER_IRREVERSIBLE",
                f"resuming would repeat irreversible step {steps[self.last_irreversible].id}", step=steps[i],
                observed=st.summary()))
        rt.log.event("repositioned", why=why, from_step=steps[i].id, to_step=steps[j].id, screen=st.screen)
        return j

    # --------------------------------------------------------------- humans
    def _approve(self, step: Step, i: int) -> bool:
        rt = self.rt
        if step.id in self.preapproved:
            rt.log.event("approval_granted", step=step.id, by="invocation pre-approval")
            return True
        iv = rt.escalate("approval", f"step '{step.id}' is irreversible: {step.intent}",
                         self._context(step, rt.classify()), ["approve", "resume", "abort"])
        if iv is None:
            raise _Stop("failed", failure=self._failure(
                "APPROVAL_REQUIRED", "irreversible step needs a human approval or --approve", step=step))
        if iv.decision == "resume":
            # The human performed (or deliberately skipped) the step in the live session.
            # Treat it as executed: re-observe, and never let automation repeat it.
            self.last_irreversible = i
            raise _Reposition()
        if iv.decision != "approve":
            raise _Stop("failed", failure=self._failure(
                "ESCALATION_TIMEOUT" if iv.status == "expired" else "APPROVAL_DECLINED", iv.note or "declined", step=step))
        return True

    def _handoff(self, i: int, kind: str, reason: str, st: State, expected: Any) -> int:
        rt, step = self.rt, self.cap.steps[i]
        if len(rt.channel.interventions) >= MAX_INTERVENTIONS:
            raise _Stop("failed", failure=self._failure("UNEXPECTED_STATE", reason + " (intervention budget spent)",
                                                        step=step, expected=expected, observed=st.summary()))
        iv = rt.escalate(kind, reason, self._context(step, st, expected), ["resume", "abort"])
        if iv is None:
            code = "INDETERMINATE_AFTER_IRREVERSIBLE" if kind == "failure" else "UNEXPECTED_STATE"
        elif iv.status == "expired":
            code = "ESCALATION_TIMEOUT"
        elif iv.decision == "abort":
            code = "ABORTED_BY_OPERATOR"
        else:
            return self._reposition(i, "handoff")
        raise _Stop("failed", failure=self._failure(code, reason, step=step, expected=expected,
                                                    observed=st.summary()))

    def _context(self, step: Step, st: State, expected: Any = None) -> dict:
        return {"capability": self.cap.ref, "tenant": self.tenant.id, "step_index": self.i, "step": step.id,
                "intent": step.intent, "expected": expected, "observed": st.summary()}

    def _failure(self, code: str, msg: str, *, step: Step | None = None, expected: Any = None,
                 observed: Any = None, retryable: bool = False) -> Failure:
        f = Failure(code=code, message=msg, step_id=step.id if step else None,
                    step_index=self.cap.steps.index(step) if step and hasattr(self, "cap") else None,
                    step_intent=step.intent if step else None, expected=expected, observed=observed,
                    retryable=retryable)
        if hasattr(self, "rt"):
            try:
                shot, snap = self.rt.capture(f"failure-{code.lower()}")
                f.evidence = [shot, snap]
            except Exception as e:  # evidence is best-effort; the failure itself must still be reported
                f.evidence = [f"evidence capture failed: {e}"]
        return f
