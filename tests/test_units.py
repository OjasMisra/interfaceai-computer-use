"""Fast unit tests for the pieces that carry the safety and correctness argument."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cua.control import ControlChannel, ControlLost
from cua.policy import Policy, PolicyGate
from cua.profile import load_profile, load_tenant
from cua.redact import Redactor
from cua.schema import Approval, Capability, OutputSpec, ParamSpec, Risk, render

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "evidence" / "capabilities" / "member.lookup_savings_balance@1.0.0.json"


@pytest.fixture
def cap() -> Capability:
    return Capability.model_validate_json(EXAMPLE.read_text("utf-8"))


@pytest.fixture
def gate() -> PolicyGate:
    return PolicyGate(Policy.load(ROOT / "config/policy.json"), load_profile("acme-coreone", "4"),
                      load_tenant("acme-fcu"))


# ---------------------------------------------------------------- schema

def test_artifact_round_trips(cap):
    again = Capability.model_validate_json(cap.to_json())
    assert again == cap and again.content_sha256() == cap.content_sha256()


def test_undeclared_template_is_rejected(cap):
    data = json.loads(cap.to_json())
    data["steps"][1]["value"] = "{{inputs.ssn}}"
    with pytest.raises(ValueError, match="undeclared inputs.ssn"):
        Capability.model_validate(data)


def test_required_output_must_be_extracted(cap):
    data = json.loads(cap.to_json())
    data["steps"] = [s for s in data["steps"] if s["action"] != "extract"]
    with pytest.raises(ValueError, match="never extracted"):
        Capability.model_validate(data)


def test_approval_is_void_after_edit(cap):
    assert cap.is_approved()
    cap.steps[2].timeout_ms = 1  # any behavioural change
    assert not cap.is_approved()


def test_approval_binds_to_hash(cap):
    cap.approvals = [Approval(by="x", at=datetime.now(timezone.utc), content_sha256="0" * 64)]
    assert not cap.is_approved()


def test_param_coercion():
    p = ParamSpec(name="member_number", type="string", description="", pattern=r"\d{5,9}")
    assert p.coerce(" 10042 ") == "10042"
    with pytest.raises(ValueError):
        p.coerce("12AB")
    d = ParamSpec(name="amt", type="decimal", description="", minimum="0.01")
    assert d.coerce("$1,250") == "1250.00"
    with pytest.raises(ValueError):
        d.coerce("0")


def test_money_parses_to_exact_decimal_string():
    o = OutputSpec(name="bal", type="decimal", description="")
    assert o.parse("18,004.22") == "18004.22"
    assert o.parse("(12.50)") == "-12.50"
    with pytest.raises(ValueError):
        o.parse("N/A")


def test_render_templates():
    assert render("/m/{{inputs.id}}/x", {"id": "7"}) == "/m/7/x"
    with pytest.raises(KeyError):
        render("{{secrets.pw}}", {})


# ---------------------------------------------------------------- redaction

def test_redaction_taints_and_patterns():
    r = Redactor()
    r.taint("10042", "member_number")
    out = r.text("member 10042 ssn 900-12-3456 phone (555) 010-4242 jane@example.com")
    assert out == "member «member_number» ssn «ssn» phone «phone» «email»"


def test_redaction_leaves_capability_refs_alone():
    assert Redactor().text("member.open_share@1.0.0") == "member.open_share@1.0.0"


def test_redaction_is_recursive():
    r = Redactor()
    r.taint("demo-pass-123", "secret")
    assert r.obj({"a": ["x demo-pass-123"]}) == {"a": ["x «secret»"]}


# ---------------------------------------------------------------- policy

def test_url_allowlist(gate):
    assert gate.check_url("http://127.0.0.1:5055/work/inquiry").allowed
    assert not gate.check_url("http://127.0.0.1:5055/__admin/fault").allowed
    assert not gate.check_url("https://evil.example.com/work/x").allowed
    assert not gate.check_url("http://127.0.0.1:5055/reports/export").allowed


def test_artifact_cannot_downgrade_risk(gate):
    v = gate.check_action("click", "button", "Post", declared=Risk.SAFE)
    assert v.decision == "approve" and v.risk == Risk.IRREVERSIBLE


def test_unknown_action_denied(gate):
    assert gate.check_action("upload", "button", "x").decision == "deny"


def test_links_to_blocked_paths_denied(gate):
    assert gate.check_action("click", "link", "Admin", href="/__admin/reset").decision == "deny"


# ---------------------------------------------------------------- control

def test_lease_fencing():
    events = []
    ch = ControlChannel("run", lambda t, **k: events.append(t))
    epoch = ch.acquire()
    ch.check(epoch)
    iv = ch.take_over("alice")  # operator grabs the session mid-run
    with pytest.raises(ControlLost):
        ch.check(epoch)  # the agent's next action is refused
    ch.record_page_event({"kind": "click", "name": "OK"})
    ch.resolve(iv.id, "resume", "alice")
    with pytest.raises(ControlLost):
        ch.check(epoch)  # old epoch stays stale even after control returns
    ch.check(ch.acquire())
    assert iv.human_actions[0]["by"] == "alice"


def test_page_events_ignored_while_agent_holds_control():
    ch = ControlChannel("run", lambda t, **k: None)
    ch.record_page_event({"kind": "click"})
    assert not ch.interventions


def test_decision_must_be_allowed():
    ch = ControlChannel("run", lambda t, **k: None)
    iv = ch.raise_intervention("approval", "post?", ["approve", "abort"])
    with pytest.raises(ValueError):
        ch.resolve(iv.id, "resume", "bob")
