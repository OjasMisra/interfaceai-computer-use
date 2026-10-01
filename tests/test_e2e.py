"""End-to-end against the mock bank in a real (headless) browser.

Discovery runs once per session; every replay test reuses the compiled
capability in memory, so nothing is written to ./capabilities.
"""

import json
import urllib.request
from datetime import datetime, timezone

import pytest

from cua.agent.loop import discover
from cua.agent.planner import MockPlanner
from cua.cli import POLICY, _setup_env, ensure_mockbank
from cua.policy import Policy
from cua.profile import load_profile, load_tenant
from cua.replay import ReplayEngine
from cua.schema import Approval, Capability

pytestmark = pytest.mark.e2e


def _post(tenant, path, body):
    req = urllib.request.Request(tenant.base_url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()


@pytest.fixture(scope="session")
def env():
    _setup_env()
    for t in ("acme-fcu", "harbor-cu"):
        ensure_mockbank(load_tenant(t))
    return Policy.load(POLICY)


@pytest.fixture(autouse=True)
def clean_mock(env):
    for t in ("acme-fcu", "harbor-cu"):
        _post(load_tenant(t), "/__admin/reset", {})


def approve(cap: Capability) -> Capability:
    cap.status = "approved"
    cap.approvals = [Approval(by="test", at=datetime.now(timezone.utc), content_sha256=cap.content_sha256())]
    return cap


@pytest.fixture(scope="session")
def balance_cap(env) -> Capability:
    tenant = load_tenant("acme-fcu")
    res = discover("Look up member 10042 and read their current savings balance", tenant,
                   load_profile("acme-coreone", "4"), env, MockPlanner(), save_artifact=False)
    assert res.status == "succeeded", res.reason
    return approve(res.capability)


def replay(cap, env, tenant="acme-fcu", fault=None, **kw):
    t = load_tenant(tenant)
    if fault:
        name, _, route = fault.partition("@")
        _post(t, "/__admin/fault", {"fault": name, "route": route or "/work/", "count": 1})
    return ReplayEngine(cap, t, env, **kw).run


def test_discovery_compiles_parameterised_flow(balance_cap):
    c = balance_cap
    assert [s.id for s in c.steps] == ["click_member_inquiry", "enter_member_number", "click_inquire",
                                       "click_shares", "read_savings_balance"]
    assert c.steps[1].value == "{{inputs.member_number}}"
    assert "10042" not in c.to_json()  # no recorded literal leaks into the artifact
    grid = c.steps[-1].target.strategies[0]
    assert grid.kind == "table_cell" and grid.row_match == {"Type": "SAVINGS"}
    assert all(s.kind != "css_path" for s in c.steps[-1].target.strategies)


def test_replay_other_member_reads_correct_row(balance_cap, env):
    r = replay(balance_cap, env)({"member_number": "10077"})
    assert r.status == "succeeded" and r.outputs == {"savings_balance": "18004.22"}


@pytest.mark.parametrize("member,code", [("99999", "MEMBER_NOT_FOUND"), ("10013", "ACCESS_RESTRICTED")])
def test_business_outcomes(balance_cap, env, member, code):
    r = replay(balance_cap, env)({"member_number": member})
    assert r.status == "business_outcome" and r.outcome["code"] == code and r.failure is None


def test_bad_input_rejected_before_ui(balance_cap, env):
    r = replay(balance_cap, env)({"member_number": "12AB"})
    assert r.status == "rejected" and r.failure.code == "INPUT_INVALID" and r.run_id is None


@pytest.mark.parametrize("fault,interstitial", [("session_expired@/work/member", "session_timeout"),
                                                ("system_notice@/work/inquiry", "system_notice"),
                                                ("processing@/work/member", "processing")])
def test_recoverable_conditions(balance_cap, env, fault, interstitial):
    r = replay(balance_cap, env, fault=fault)({"member_number": "10042"})
    assert r.status == "succeeded" and r.outputs["savings_balance"] == "1250.75"
    assert [x["interstitial"] for x in r.recoveries] == [interstitial]


def test_hard_failure_is_debuggable(balance_cap, env):
    r = replay(balance_cap, env, fault="abend@/work/member")({"member_number": "10042"})
    assert r.status == "failed" and r.failure.code == "APP_ABEND" and not r.failure.retryable
    assert r.failure.step_id == "click_inquire" and r.failure.expected == {"screen": "member_detail"}
    assert any(p.endswith(".png") for p in r.failure.evidence)


def test_unknown_state_without_operator_fails_closed(balance_cap, env):
    r = replay(balance_cap, env, fault="security_check@/work/member", operator="none")({"member_number": "10042"})
    assert r.status == "failed" and r.failure.code == "UNEXPECTED_STATE"


def test_unknown_state_handed_to_human_and_resumed(balance_cap, env):
    r = replay(balance_cap, env, fault="security_check@/work/member",
               operator="sim:verify_identity")({"member_number": "10042"})
    assert r.status == "succeeded" and r.human_assisted
    assert r.interventions[0]["human_actions"][0]["name"] == "Identity Verified"


def test_new_tenant_degrades_gracefully_and_reports_drift(balance_cap, env):
    r = replay(balance_cap, env, tenant="harbor-cu-unconfigured")({"member_number": "10077"})
    assert r.status == "succeeded" and r.outputs["savings_balance"] == "18004.22"
    assert {d["target"] for d in r.drift} == {"member_inquiry.member_number", "member_detail.shares"}


def test_tenant_overrides_remove_drift(balance_cap, env):
    r = replay(balance_cap, env, tenant="harbor-cu")({"member_number": "10077"})
    assert r.status == "succeeded" and not r.drift and len(r.overrides_applied) == 2


def test_unapproved_capability_rejected(balance_cap, env):
    draft = balance_cap.model_copy(deep=True)
    draft.status = "draft"
    r = replay(draft, env)({"member_number": "10042"})
    assert r.status == "rejected" and r.failure.code == "NOT_APPROVED"


@pytest.fixture
def share_cap():
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "evidence/capabilities/member.open_share@1.0.0.json"
    return Capability.model_validate_json(p.read_text("utf-8"))


SHARE_INPUTS = {"member_number": "10077", "share_type": "CLUB", "opening_deposit": "25"}


def test_irreversible_step_needs_approval(share_cap, env):
    r = replay(share_cap, env, operator="none")(SHARE_INPUTS)
    assert r.status == "failed" and r.failure.code == "APPROVAL_REQUIRED" and r.failure.step_id == "click_post"


def test_irreversible_step_with_preapproval(share_cap, env):
    r = replay(share_cap, env, preapproved=("click_post",))(SHARE_INPUTS)
    assert r.status == "succeeded" and r.outputs["confirmation_number"].startswith("CF-")


def test_host_validation_is_a_business_outcome(share_cap, env):
    r = replay(share_cap, env, preapproved=("click_post",))({**SHARE_INPUTS, "opening_deposit": "1"})
    assert r.status == "business_outcome" and r.outcome["code"] == "MINIMUM_DEPOSIT_NOT_MET"
