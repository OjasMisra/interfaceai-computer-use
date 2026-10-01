# Computer-use automation for legacy banking apps

An LLM-driven agent learns a task in a legacy back-office UI once. The run is compiled into a
typed, versioned **capability** (inputs, outputs, business outcomes, steps with robust targets
and checkpoints). Replay is **deterministic**, with no model in the loop, and handles runtime
exceptions explicitly. When automation can't proceed safely, it hands the **same live browser
session** to a human and takes it back afterwards.

The target is a local mock "core banking" app (`mockbank/`) built to be hostile in the
legacy-banking way: framesets, table layouts, no ids or test ids, unlabeled inputs. It can
inject the runtime failures the brief lists: session timeout, notices, slow host, ABEND, and an
unexpected security screen. A second "tenant" serves the same product with relabeled fields.
All data is synthetic.

Design write-up: **[REPORT.md](REPORT.md)**. Evidence from every scenario: **[evidence/](evidence/README.md)**.

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
# macOS/Linux: source .venv/bin/activate      Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

**No keys or external services are needed.** Commands start the mock bank in-process when
nothing is listening on its port. The mock bank's demo operator credentials come from
`.env.example`; copy it to `.env` to override them. Discovery uses the offline `MockPlanner` by
default.

Optional: to use a real model for discovery, set `ANTHROPIC_API_KEY` and add `--planner claude`.
This uses Claude Opus 5.5 via tool use (`cua/agent/claude.py`). See REPORT.md → Cuts: this path
is implemented but was not run for the committed evidence.

## Demo path

```bash
# 1. Discover: the agent drives the UI toward a natural-language goal and records a capability
python -m cua discover --goal "Look up member 10042 and read their current savings balance" --operator none
#    -> capabilities/member.lookup_savings_balance/1.0.0.json   (status: draft)

# 2. Review and approve (approval is bound to the artifact's content hash)
python -m cua approve member.lookup_savings_balance --by "Your Name"

# 3. Replay deterministically with new inputs (no LLM)
python -m cua replay member.lookup_savings_balance --input member_number=10077 --operator none
#    -> status: succeeded, outputs: {"savings_balance": "18004.22"}

# 4. Exceptional states
python -m cua replay member.lookup_savings_balance --input member_number=99999 --operator none          # business_outcome MEMBER_NOT_FOUND
python -m cua replay member.lookup_savings_balance --input member_number=12AB  --operator none          # rejected INPUT_INVALID (no browser started)
python -m cua replay member.lookup_savings_balance --input member_number=10042 --operator none \
    --fault session_expired@/work/member                                                            # succeeded, recovered: session_timeout
python -m cua replay member.lookup_savings_balance --input member_number=10042 --operator none \
    --fault abend@/work/member                                                                      # failed APP_ABEND + masked screenshot

# 5. Human handoff on an unknown screen (scripted operator attaches to the live browser over CDP)
python -m cua replay member.lookup_savings_balance --input member_number=10042 \
    --fault security_check@/work/member --operator sim:verify_identity
```

`--fault` is a test hook that only talks to the mock app's `/__admin` endpoint. The agent itself
is blocked from that path by policy.

**Doing the handoff yourself:** run step 5 with `--operator console --headed` instead. The
command prints an operator-console URL. Open it and click **Take control**. In the browser window
that opened (the same session the automation was driving), click **Identity Verified**, then
click **resume** in the console. The run continues from where the app now is. It records what you
clicked, and returns `human_assisted: true`.

**Irreversible flow:**

```bash
python -m cua discover --goal "Open a new savings share for member 10042 with an opening deposit of \$50.00 and get the confirmation number" --operator sim:approve
python -m cua approve member.open_share --by "Your Name"
python -m cua replay member.open_share --input member_number=10077 --input share_type=CLUB --input opening_deposit=25 --operator none     # failed APPROVAL_REQUIRED
python -m cua replay member.open_share --input member_number=10077 --input share_type=CLUB --input opening_deposit=25 --operator sim:approve
```

**Second tenant**, same vendor product with relabeled fields:

```bash
python -m cua replay member.lookup_savings_balance --tenant harbor-cu-unconfigured --input member_number=10077 --operator none  # works via fallbacks, reports drift
python -m cua replay member.lookup_savings_balance --tenant harbor-cu --input member_number=10077 --operator none               # tenant overrides applied, no drift
```

**Everything at once:** `python -m cua demo` runs all 17 scenarios in about 2 minutes. It checks
each one against its expected result and regenerates `evidence/`.

### Agent-facing interface

```bash
python -m cua catalog                     # approved capabilities as tool definitions (JSON Schema inputs)
python -m cua invoke member__lookup_savings_balance --args "{\"member_number\": \"10077\"}"
```

### Operator modes (`--operator`)

| mode | behaviour on an intervention |
|---|---|
| `console` (default) | prints the operator-console URL and the CDP endpoint, then waits up to 10 min for a human |
| `sim:<playbook>[,<playbook>]` | spawns a scripted operator process (`verify_identity`, `approve`, `perform_post`, `abort`) that uses the same console API and the same live browser |
| `none` | nobody on call: the run fails closed (`UNEXPECTED_STATE`, `APPROVAL_REQUIRED`) |

### Exit codes

`0` succeeded or business outcome · `1` failed · `2` rejected before touching the UI.

## Tests

```bash
pytest            # 35 tests, ~70 s: unit tests plus end-to-end runs in headless Chromium
pytest -m "not e2e"
```

## Looking at the mock app

`python -m cua mockbank`, then open http://127.0.0.1:5055 and sign on as `TELLER01` / `demo-pass-123`.
Members `10042` and `10077` exist, `10013` is restricted, and anything else is not found.

## Layout

```
cua/
  schema.py          capability artifact (pydantic): contract, steps, targets, checkpoints, approvals
  profile.py         app profile (per vendor product) + tenant binding/overrides
  surface/           Surface seam; web.py = Playwright; snapshot.js = semantic perception
  agent/             planner seam (mock + Claude), discovery loop, trace -> artifact compiler
  replay.py          deterministic replay engine + result contract
  classify.py        state classifier: error / interstitial / outcome / screen / unknown
  runtime.py         shared session plumbing for discovery and replay
  control.py         control lease, interventions, operator console
  operator_sim.py    scripted operator (CDP attach to the live session)
  policy.py          allowlists and risk gating      redact.py   redaction      evidence.py   run log
  catalog.py         capabilities as agent tools     demo.py     scenario runner -> evidence/
profiles/            acme-coreone@4.json   (screens, interstitials, errors, outcomes, sign-on, risk rules)
tenants/             acme-fcu, harbor-cu, harbor-cu-unconfigured
config/policy.json   allowlist, budgets, irreversible-action policy
mockbank/            the legacy target app
evidence/            committed artifacts, logs, screenshots for every scenario
```

Runtime output goes to `runs/<run_id>/` and compiled artifacts go to
`capabilities/<id>/<version>.json`. Both are git-ignored. The committed copies live in `evidence/`.
