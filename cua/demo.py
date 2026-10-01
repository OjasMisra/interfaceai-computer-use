"""Run every scenario end to end and write the evidence/ folder.

Each scenario states what it expects; the demo checks the real result against
that and records PASS/FAIL. Output values are checked here, in memory, and
only the check result is persisted -- the run logs themselves never contain them.
"""

from __future__ import annotations

import json
import shutil
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .agent.compiler import CAPS_DIR
from .agent.loop import discover
from .agent.planner import MockPlanner
from .catalog import catalog, find
from .cli import POLICY, cmd_approve, ensure_mockbank, inject_fault, reset_mock
from .policy import Policy
from .profile import ROOT, load_profile, load_tenant
from .replay import ReplayEngine

EVIDENCE = ROOT / "evidence"

BALANCE_GOAL = "Look up member 10042 and read their current savings balance"
SHARE_GOAL = "Open a new savings share for member 10042 with an opening deposit of $50.00 and get the confirmation number"
SHARE_IN = {"member_number": "10077", "share_type": "CLUB", "opening_deposit": "25"}

# name, capability, tenant, inputs, faults, operator, preapproved, expectation, story
REPLAYS = [
    ("03-replay-success", "member.lookup_savings_balance", "acme-fcu", {"member_number": "10077"}, [], "none", (),
     {"status": "succeeded", "outputs": {"savings_balance": "18004.22"}},
     "Different member than recorded; savings row is 3rd, not 1st. Row-key targeting reads the right cell."),
    ("04-replay-not-found", "member.lookup_savings_balance", "acme-fcu", {"member_number": "99999"}, [], "none", (),
     {"status": "business_outcome", "outcome": "MEMBER_NOT_FOUND"}, "A legitimate answer, not a failure."),
    ("05-replay-restricted", "member.lookup_savings_balance", "acme-fcu", {"member_number": "10013"}, [], "none", (),
     {"status": "business_outcome", "outcome": "ACCESS_RESTRICTED"}, "Permission denial surfaced as a business outcome."),
    ("06-replay-bad-input", "member.lookup_savings_balance", "acme-fcu", {"member_number": "12AB"}, [], "none", (),
     {"status": "rejected", "failure": "INPUT_INVALID"}, "Rejected by the input contract before a browser starts."),
    ("07-replay-session-timeout", "member.lookup_savings_balance", "acme-fcu", {"member_number": "10042"},
     ["session_expired@/work/member"], "none", (),
     {"status": "succeeded", "outputs": {"savings_balance": "1250.75"}, "recoveries": ["session_timeout"]},
     "Host session expires mid-flow; profile handler re-authenticates, engine repositions."),
    ("08-replay-slow-host", "member.lookup_savings_balance", "acme-fcu", {"member_number": "10042"},
     ["processing@/work/member"], "none", (),
     {"status": "succeeded", "outputs": {"savings_balance": "1250.75"}, "recoveries": ["processing"]},
     "'PROCESSING - PLEASE WAIT' interim page is waited out, not treated as an error."),
    ("09-replay-system-notice", "member.lookup_savings_balance", "acme-fcu", {"member_number": "10042"},
     ["system_notice@/work/member"], "none", (),
     {"status": "succeeded", "outputs": {"savings_balance": "1250.75"}, "recoveries": ["system_notice"]},
     "Broadcast overlay blocking the screen is acknowledged and the flow continues."),
    ("10-replay-app-abend", "member.lookup_savings_balance", "acme-fcu", {"member_number": "10042"},
     ["abend@/work/member"], "none", (),
     {"status": "failed", "failure": "APP_ABEND"}, "Hard failure: step, expected vs observed, masked screenshot."),
    ("11-replay-unknown-state-handoff", "member.lookup_savings_balance", "acme-fcu", {"member_number": "10042"},
     ["security_check@/work/member"], "sim:verify_identity", (),
     {"status": "succeeded", "outputs": {"savings_balance": "1250.75"}, "interventions": ["unexpected_state"]},
     "Unrecognised security screen -> human takes the live session over CDP, clicks through, hands back."),
    ("12-replay-unknown-state-no-operator", "member.lookup_savings_balance", "acme-fcu", {"member_number": "10042"},
     ["security_check@/work/member"], "none", (),
     {"status": "failed", "failure": "UNEXPECTED_STATE"}, "Same situation with nobody on call: fails closed."),
    ("13-replay-open-share-human-approval", "member.open_share", "acme-fcu", SHARE_IN, [], "sim:approve", (),
     {"status": "succeeded", "interventions": ["approval"]}, "Irreversible Post waits for a human approval."),
    ("14-replay-open-share-no-approver", "member.open_share", "acme-fcu", SHARE_IN, [], "none", (),
     {"status": "failed", "failure": "APPROVAL_REQUIRED"}, "No approver and no pre-approval: nothing is posted."),
    ("15-replay-open-share-min-deposit", "member.open_share", "acme-fcu", {**SHARE_IN, "opening_deposit": "1"}, [],
     "none", ("click_post",), {"status": "business_outcome", "outcome": "MINIMUM_DEPOSIT_NOT_MET"},
     "Host-side validation error is a business outcome the caller can act on."),
    ("16-replay-tenant-b-unconfigured", "member.lookup_savings_balance", "harbor-cu-unconfigured",
     {"member_number": "10077"}, [], "none", (),
     {"status": "succeeded", "outputs": {"savings_balance": "18004.22"}, "drift": 2},
     "Same vendor app, relabelled by tenant B. Fallback strategies carry it; drift report says what to override."),
    ("17-replay-tenant-b-overrides", "member.lookup_savings_balance", "harbor-cu", {"member_number": "10077"}, [],
     "none", (), {"status": "succeeded", "outputs": {"savings_balance": "18004.22"}, "drift": 0},
     "Tenant overlay applied: same artifact, no drift."),
]


def _check(res: dict, exp: dict, outputs: dict) -> list[str]:
    problems = []
    if res["status"] != exp["status"]:
        problems.append(f"status {res['status']} != {exp['status']}")
    if "outcome" in exp and (res.get("outcome") or {}).get("code") != exp["outcome"]:
        problems.append(f"outcome != {exp['outcome']}")
    if "failure" in exp and (res.get("failure") or {}).get("code") != exp["failure"]:
        problems.append(f"failure != {exp['failure']}")
    for k, v in exp.get("outputs", {}).items():
        if outputs.get(k) != v:
            problems.append(f"output {k} mismatch")
    if "recoveries" in exp and [r["interstitial"] for r in res.get("recoveries", [])] != exp["recoveries"]:
        problems.append("recoveries mismatch")
    if "interventions" in exp and [i["kind"] for i in res.get("interventions", [])] != exp["interventions"]:
        problems.append("interventions mismatch")
    if "drift" in exp and len(res.get("drift", [])) != exp["drift"]:
        problems.append("drift count mismatch")
    return problems


def _copy_run(run_dir: str | None, name: str) -> str | None:
    if not run_dir:
        return None
    dest = EVIDENCE / "runs" / name
    shutil.copytree(run_dir, dest)
    return str(dest.relative_to(EVIDENCE)).replace("\\", "/")


def run_demo(headed: bool = False) -> int:
    policy = Policy.load(POLICY)
    profile = load_profile("acme-coreone", "4")
    for t in ("acme-fcu", "harbor-cu"):
        ensure_mockbank(load_tenant(t))
    shutil.rmtree(CAPS_DIR, ignore_errors=True)
    shutil.rmtree(EVIDENCE, ignore_errors=True)
    (EVIDENCE / "capabilities").mkdir(parents=True)
    rows = []

    def log(msg):
        print(f"\n=== {msg}", file=sys.stderr)

    # -------- discovery
    acme = load_tenant("acme-fcu")
    for name, goal, faults, operator, story in [
        ("01-discovery-lookup-balance", BALANCE_GOAL, ["system_notice@/work/inquiry"], "none",
         "Mock planner drives the app; a system notice pops up mid-run and is cleared by the profile, not recorded."),
        ("02-discovery-open-share", SHARE_GOAL, [], "sim:approve",
         "Planner proposes the irreversible Post; policy pauses for a human approval; artifact marks the step."),
    ]:
        log(name)
        reset_mock(acme)
        for f in faults:
            inject_fault(acme, f)
        res = discover(goal, acme, profile, policy, MockPlanner(), operator=operator, headed=headed)
        ok = res.status == "succeeded"
        rows.append({"scenario": name, "story": story, "status": res.status, "detail": res.capability.ref if ok else res.reason,
                     "check": "PASS" if ok else f"FAIL: {res.reason}", "run": _copy_run(res.evidence_dir, name),
                     "command": f'python -m cua discover --goal "{goal}"' + "".join(f" --fault {f}" for f in faults)
                                + f" --operator {operator}"})
        if ok:
            cmd_approve(type("A", (), {"capability": res.capability.id, "by": "demo reviewer",
                                       "note": "Reviewed steps, targets, risk and outcomes"})())

    for cid in ("member.lookup_savings_balance", "member.open_share"):
        cap, path = find(cid)
        shutil.copy(path, EVIDENCE / "capabilities" / f"{cap.ref}.json")
    (EVIDENCE / "catalog.json").write_text(json.dumps(catalog(), indent=2) + "\n", encoding="utf-8")

    # -------- replays
    for name, cid, tid, inputs, faults, operator, pre, exp, story in REPLAYS:
        log(name)
        tenant = load_tenant(tid)
        reset_mock(tenant)
        for f in faults:
            inject_fault(tenant, f)
        cap, _ = find(cid)
        res = ReplayEngine(cap, tenant, policy, operator=operator, headed=headed, preapproved=pre).run(inputs)
        outputs = dict(res.outputs)
        persisted = json.loads(res.model_dump_json())
        problems = _check(persisted, exp, outputs)
        run = _copy_run(res.evidence_dir, name)
        if run is None:  # rejected before any session existed: keep the result itself as evidence
            d = EVIDENCE / "runs" / name
            d.mkdir(parents=True)
            (d / "result.json").write_text(res.model_dump_json(indent=2), encoding="utf-8")
            run = f"runs/{name}"
        f = res.failure
        detail = (res.outcome or {}).get("code") or (f.code if f else "") or ", ".join(f"{k}=«ok»" for k in outputs)
        extras = []
        if res.recoveries:
            extras.append("recovered: " + ", ".join(r["interstitial"] for r in res.recoveries))
        if res.interventions:
            extras.append("human: " + ", ".join(f"{i['kind']}->{i['decision']}" for i in res.interventions))
        if res.drift:
            extras.append(f"drift: {len(res.drift)}")
        cmd = f"python -m cua replay {cid} --tenant {tid}" + "".join(f" --input {k}={v}" for k, v in inputs.items()) \
              + "".join(f" --fault {x}" for x in faults) + f" --operator {operator}" + "".join(f" --approve {p}" for p in pre)
        rows.append({"scenario": name, "story": story, "status": res.status, "detail": "; ".join([detail] + extras),
                     "check": "PASS" if not problems else "FAIL: " + "; ".join(problems), "run": run, "command": cmd})

    _write_index(rows)
    failed = [r for r in rows if r["check"] != "PASS"]
    print(f"\n{len(rows) - len(failed)}/{len(rows)} scenarios matched expectations. Evidence: {EVIDENCE}", file=sys.stderr)
    return 1 if failed else 0


def _write_index(rows: list[dict]) -> None:
    (EVIDENCE / "summary.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Evidence",
        "",
        f"Generated by `python -m cua demo` on {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC against the local mock "
        "bank (synthetic data). Planner: `mock-heuristic-planner/1` (see REPORT.md, Cuts).",
        "",
        "Each run folder has `events.jsonl` (redacted structured log), `result.json` (the replay contract as returned, "
        "with output values masked), and on failures, escalations and business outcomes `evidence/*.png` (masked "
        "screenshot) + `*.snapshot.json` (redacted semantic snapshot of every frame). Handoffs add "
        "`interventions/*.json` and `operator-sim-*.log`. Discovery runs add `observations/` (what the planner saw) "
        "and `capability.json`.",
        "",
        "`check` compares the real result with the scenario's expectation, including the actual output values "
        "(compared in memory, never written to disk).",
        "",
        "| # | scenario | status | detail | check |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        n, _, name = r["scenario"].partition("-")
        lines.append(f"| {n} | [{name}]({r['run']}/) — {r['story']} | `{r['status']}` | {r['detail']} | {r['check']} |")
    lines += ["", "## Reproduce a single scenario", "", "```bash"]
    lines += [r["command"] for r in rows]
    lines += ["```", "", "Artifacts: [capabilities/](capabilities/) · Agent tool catalog: [catalog.json](catalog.json)", ""]
    (EVIDENCE / "README.md").write_text("\n".join(lines), encoding="utf-8")
