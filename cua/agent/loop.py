"""Discovery: the LLM-driven observe -> decide -> act loop.

    sign on (runtime, no model) -> [observe -> planner.decide -> policy -> act]* -> compile

The planner proposes; the runtime disposes. Every proposed action is checked
against the lease, the action allowlist and the risk rules before it reaches
the browser. Known interstitials are cleared by the profile handlers, exactly
as in replay, so the model spends no turns on them and they never end up as
recorded steps. Stopping conditions: done, max steps, wall-clock budget,
repeated invalid decisions, or a human aborting an escalation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..classify import Classifier
from ..policy import Policy
from ..profile import AppProfile, Tenant
from ..runtime import PolicyDenied, Runtime
from ..schema import Capability, render
from .compiler import HumanEntry, TraceEntry, compile_capability, save
from .planner import AgentAction, Planner

MAX_BAD_DECISIONS = 3


@dataclass
class DiscoveryResult:
    run_id: str
    status: str  # succeeded | failed
    reason: str = ""
    capability: Capability | None = None
    artifact_path: str | None = None
    notes: list[str] = field(default_factory=list)
    turns: int = 0
    evidence_dir: str = ""


def discover(goal: str, tenant: Tenant, profile: AppProfile, policy: Policy, planner: Planner, *,
             operator: str = "none", headed: bool = False, save_artifact: bool = True) -> DiscoveryResult:
    rt = Runtime("discovery", tenant, profile, policy, operator=operator, headed=headed, classifier=Classifier(profile))
    hints = planner.parse_goal(goal)
    inputs = {h.name: h.value for h in hints}
    for h in hints:
        rt.redactor.taint(h.value, h.name)
    redacted_goal = rt.redactor.text(goal)
    res = DiscoveryResult(run_id=rt.log.run_id, status="failed", evidence_dir=str(rt.log.dir))
    rt.log.event("discovery_started", goal=redacted_goal, planner=planner.name, tenant=tenant.id,
                 inputs=[h.model_dump(exclude={"value"}) for h in hints])
    trace: list = []
    history: list[dict] = []
    bad = 0
    t0 = time.monotonic()
    try:
        rt.start()
        rt.sign_on()
        rt.surface.goto(profile.entry_path)
        for turn in range(1, policy.max_steps + 1):
            res.turns = turn
            if time.monotonic() - t0 > policy.max_seconds:
                res.reason = f"time budget of {policy.max_seconds}s exhausted"
                break
            st = rt.classify()
            if st.interstitial:
                ok = rt.handle_interstitial(st.interstitial, inputs)
                rt.log.event("recovery_succeeded" if ok else "recovery_failed", interstitial=st.interstitial.id,
                             during="discovery")
                if not ok:
                    res.reason = f"could not clear {st.interstitial.id}"
                    break
                continue
            if st.error:
                rt.capture("failure-app-error")
                res.reason = f"application error {st.error.code}: {st.error_detail}"
                break

            obs = rt.surface.observe()
            obs_file = rt.log.write_json(f"observations/{turn:02d}.json", obs.__dict__)
            rt.log.event("observation", turn=turn, state=st.summary(), elements=sum(len(f["elements"]) for f in obs.frames),
                         file=obs_file.name)
            action = planner.decide(goal, inputs, obs, history)
            rt.log.event("decision", turn=turn, action=action.type, ref=action.ref, value=action.value,
                         output=action.output.name if action.output else None, rationale=action.rationale)

            if action.type == "done":
                final = rt.classify()
                cap, notes = compile_capability(trace, hints, action, final.screen, final.text, profile, rt.gate,
                                                run_id=rt.log.run_id, planner=planner.name, goal=redacted_goal,
                                                tenant_id=tenant.id)
                res.status, res.capability, res.notes = "succeeded", cap, notes
                if save_artifact:
                    res.artifact_path = str(save(cap))
                rt.log.event("capability_compiled", capability=cap.ref, steps=len(cap.steps),
                             path=res.artifact_path, notes=notes)
                break

            if action.type == "escalate":
                iv = rt.escalate("stuck", action.rationale, {"goal": redacted_goal, "turn": turn,
                                                             "observed": st.summary()}, ["resume", "abort"])
                if iv is None or iv.decision != "resume":
                    res.reason = f"planner stuck ({action.rationale}); " + ("no operator" if iv is None else f"operator {iv.decision}")
                    break
                trace.append(HumanEntry(turn, iv.id, "stuck", st.screen, iv.human_actions))
                history.append({"type": "human", "note": iv.note, "actions": len(iv.human_actions)})
                continue

            el = obs.element(action.ref or "")
            if el is None:
                bad += 1
                history.append({"type": "error", "error": f"unknown ref {action.ref}"})
                if bad >= MAX_BAD_DECISIONS:
                    res.reason = "planner kept choosing invalid elements"
                    break
                continue

            frame = obs.frame_of(action.ref)["path"]
            r = rt.surface.resolve_ref(action.ref, el)
            verb = action.type
            verdict = rt.gate.check_action(verb, el["role"], el.get("name", ""), el.get("attrs", {}).get("href"))
            approved_by = None
            if verdict.decision == "deny":
                rt.log.event("policy_denied", turn=turn, action=verb, reason=verdict.reason)
                history.append({"type": "denied", "ref": action.ref, "reason": verdict.reason})
                bad += 1
                if bad >= MAX_BAD_DECISIONS:
                    res.reason = "planner kept proposing denied actions"
                    break
                continue
            if verdict.decision == "approve":
                iv = rt.escalate("approval", f"planner wants to {verb} '{el.get('name')}': {verdict.reason}",
                                 {"goal": redacted_goal, "turn": turn, "rationale": action.rationale,
                                  "observed": st.summary()}, ["approve", "resume", "abort"])
                if iv is None or iv.decision == "abort":
                    res.reason = "irreversible action not approved"
                    break
                if iv.decision == "resume":  # the human did it themselves
                    trace.append(TraceEntry(turn, action, el, frame, obs.frame_of(action.ref)["elements"],
                                            st.screen, st.text[:2], verdict.risk.value, approved_by=f"{iv.claimed_by} (performed)"))
                    history.append({"type": action.type, "ref": action.ref, "by": "human"})
                    continue
                approved_by = iv.claimed_by
            try:
                out = rt.act(verb, r, render(action.value, inputs), approved=approved_by is not None)
            except PolicyDenied as e:
                res.reason = f"policy denied: {e.reason}"
                break
            if action.type == "extract":
                rt.redactor.taint((out or "").strip(), action.output.name)
                rt.log.event("extracted", output=action.output.name, value=(out or "").strip())
            trace.append(TraceEntry(turn, action, el, frame, obs.frame_of(action.ref)["elements"], st.screen,
                                    st.text[:2], verdict.risk.value, approved_by=approved_by))
            history.append({"type": action.type, "ref": action.ref, "output": action.output.name if action.output else None})
        else:
            res.reason = f"max steps ({policy.max_steps}) reached"
    except Exception as e:
        res.reason = f"{type(e).__name__}: {e}"
        try:
            rt.capture("failure-exception")
        except Exception:
            pass
    finally:
        rt.log.event("discovery_finished", status=res.status, reason=res.reason, turns=res.turns,
                     capability=res.capability.ref if res.capability else None)
        if res.capability:
            rt.log.write_json("capability.json", res.capability.model_dump(mode="json", by_alias=True))
        rt.close()
    return res
