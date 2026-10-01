"""Command line: discover, replay, approve, catalog, invoke, mockbank.

    python -m cua discover --goal "Look up member 10042 and read their current savings balance"
    python -m cua approve member.lookup_savings_balance --by "Jane Reviewer"
    python -m cua replay member.lookup_savings_balance --input member_number=10077
    python -m cua demo                       # every scenario, evidence into ./evidence
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .profile import ROOT, load_profile, load_tenant
from .policy import Policy
from .runtime import load_env_file
from .schema import Approval, Status

POLICY = ROOT / "config" / "policy.json"


def _setup_env() -> None:
    if not load_env_file(ROOT / ".env"):
        load_env_file(ROOT / ".env.example")
        print("note: no .env found; using demo credentials from .env.example (mock bank only)", file=sys.stderr)


def _listening(url: str) -> bool:
    u = urlparse(url)
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((u.hostname, u.port or 80)) == 0


_servers = []


def ensure_mockbank(tenant) -> None:
    """Demo convenience: serve the mock app for a local tenant if nothing is there."""
    if _listening(tenant.base_url) or not tenant.mock_variant:
        return
    from mockbank.app import serve
    port = urlparse(tenant.base_url).port
    _servers.append(serve(tenant.mock_variant, port=port))
    print(f"note: started mock bank '{tenant.mock_variant}' at {tenant.base_url}", file=sys.stderr)


def inject_fault(tenant, spec: str) -> None:
    """Test hook for the *mock* app: name[@route][:count]."""
    name, _, count = spec.partition(":")
    name, _, route = name.partition("@")
    body = {"fault": name, "count": int(count or 1), "route": route or "/work/"}
    req = urllib.request.Request(tenant.base_url + "/__admin/fault", data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=5).read()
    print(f"note: injected fault {body}", file=sys.stderr)


def reset_mock(tenant) -> None:
    req = urllib.request.Request(tenant.base_url + "/__admin/reset", data=b"{}", method="POST",
                                 headers={"content-type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()


def make_planner(name: str):
    if name == "claude":
        from .agent.claude import ClaudePlanner
        return ClaudePlanner()
    from .agent.planner import MockPlanner
    return MockPlanner()


def cmd_discover(a) -> int:
    from .agent.loop import discover
    tenant = load_tenant(a.tenant)
    ensure_mockbank(tenant)
    for f in a.fault or []:
        inject_fault(tenant, f)
    res = discover(a.goal, tenant, load_profile(tenant.profile, tenant.profile_version), Policy.load(POLICY),
                   make_planner(a.planner), operator=a.operator, headed=a.headed)
    print(json.dumps({"run_id": res.run_id, "status": res.status, "reason": res.reason, "turns": res.turns,
                      "capability": res.capability.ref if res.capability else None, "artifact": res.artifact_path,
                      "notes": res.notes, "evidence": res.evidence_dir}, indent=2))
    return 0 if res.status == "succeeded" else 1


def run_replay(ref: str, tenant_id: str, inputs: dict, *, operator="none", headed=False, approve=(),
               allow_draft=False, faults=()) -> "ReplayResult":
    from .catalog import find
    from .replay import ReplayEngine
    cap, _ = find(ref)
    tenant = load_tenant(tenant_id)
    ensure_mockbank(tenant)
    for f in faults:
        inject_fault(tenant, f)
    eng = ReplayEngine(cap, tenant, Policy.load(POLICY), operator=operator, headed=headed,
                       preapproved=tuple(approve), allow_draft=allow_draft)
    return eng.run(inputs)


def cmd_replay(a) -> int:
    inputs = dict(kv.split("=", 1) for kv in a.input or [])
    res = run_replay(a.capability, a.tenant, inputs, operator=a.operator, headed=a.headed, approve=a.approve or [],
                     allow_draft=a.allow_draft, faults=a.fault or [])
    print(res.model_dump_json(indent=2, exclude_none=True))
    return {"succeeded": 0, "business_outcome": 0, "rejected": 2}.get(res.status, 1)


def cmd_approve(a) -> int:
    from .catalog import find
    cap, path = find(a.capability)
    cap.status = Status.APPROVED.value
    cap.approvals.append(Approval(by=a.by, at=datetime.now(timezone.utc), content_sha256=cap.content_sha256(),
                                  note=a.note or ""))
    path.write_text(cap.to_json() + "\n", encoding="utf-8")
    print(f"approved {cap.ref} (sha256 {cap.content_sha256()[:12]}) -> {path}")
    return 0


def cmd_catalog(a) -> int:
    from .catalog import catalog
    print(json.dumps(catalog(approved_only=not a.all), indent=2))
    return 0


def cmd_invoke(a) -> int:
    """What the agent product does: call a catalog tool by name with typed args."""
    from .catalog import catalog
    tools = {t["name"]: t for t in catalog()}
    if a.tool not in tools:
        print(f"no approved tool {a.tool}; available: {sorted(tools)}", file=sys.stderr)
        return 2
    res = run_replay(tools[a.tool]["x-capability"], a.tenant, json.loads(a.args), operator=a.operator)
    print(res.model_dump_json(indent=2, exclude_none=True))
    return 0 if res.status in ("succeeded", "business_outcome") else 1


def cmd_mockbank(a) -> int:
    from mockbank.app import create_app
    create_app(a.variant).run(host="127.0.0.1", port=a.port, threaded=True)
    return 0


def cmd_demo(a) -> int:
    from .demo import run_demo
    return run_demo(headed=a.headed)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    _setup_env()
    ap = argparse.ArgumentParser(prog="cua", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--tenant", default="acme-fcu")
        p.add_argument("--operator", default="console",
                       help="console | sim:<playbook>[,<playbook>...] | none")
        p.add_argument("--headed", action="store_true", help="show the browser (lets a human operate it directly)")
        p.add_argument("--fault", action="append", help="mock-app test hook: name[@route][:count]")

    p = sub.add_parser("discover", help="LLM-driven run that records a capability")
    p.add_argument("--goal", required=True)
    p.add_argument("--planner", default="mock", choices=["mock", "claude"])
    common(p)
    p.set_defaults(fn=cmd_discover)

    p = sub.add_parser("replay", help="deterministic replay of a capability")
    p.add_argument("capability", help="path, id@version, or id (latest)")
    p.add_argument("--input", action="append", metavar="NAME=VALUE")
    p.add_argument("--approve", action="append", metavar="STEP_ID", help="pre-approve an irreversible step")
    p.add_argument("--allow-draft", action="store_true")
    common(p)
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("approve", help="review sign-off: draft -> approved (bound to content hash)")
    p.add_argument("capability")
    p.add_argument("--by", required=True)
    p.add_argument("--note")
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("catalog", help="approved capabilities as tool definitions")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_catalog)

    p = sub.add_parser("invoke", help="invoke a catalog tool by name (what the AI agent does)")
    p.add_argument("tool")
    p.add_argument("--args", required=True, help="JSON object")
    p.add_argument("--tenant", default="acme-fcu")
    p.add_argument("--operator", default="none")
    p.set_defaults(fn=cmd_invoke)

    p = sub.add_parser("mockbank", help="serve the mock core-banking app")
    p.add_argument("--variant", default="acme")
    p.add_argument("--port", type=int, default=5055)
    p.set_defaults(fn=cmd_mockbank)

    p = sub.add_parser("demo", help="run every scenario and write evidence/")
    p.add_argument("--headed", action="store_true")
    p.set_defaults(fn=cmd_demo)

    a = ap.parse_args(argv)
    return a.fn(a)
