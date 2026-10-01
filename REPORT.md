# Design report

The model discovers, the artifact becomes the capability, and deterministic replay is how an
agent uses it in production. This report explains the decisions behind each piece and where each
one stops.

## Architecture

```
 goal ──► Planner (LLM | Mock) ──proposes──┐                    ┌── AppProfile  (per vendor product: screens,
                                           ▼                    │   interstitials, errors, outcomes, sign-on,
        Discovery loop ──trace──► Compiler ──► Capability ◄─────┤   risk rules, sensitive labels/columns)
                                           │   (artifact)        └── Tenant      (base URL, secret slots, overrides)
                                           ▼
 agent ──► catalog / invoke ──► ReplayEngine (no LLM)
                                           │
              both run on ──►  Runtime: PolicyGate · Redactor · RunLog · ControlChannel ◄──► Operator console / CDP
                                           │
                                     Surface seam ──► WebSurface (Playwright + snapshot.js) ──► legacy app
```

It is a single Python process: Playwright, pydantic, and a small Flask console. The seams are
interfaces (`Planner`, `Surface`, `Classifier`, `OperatorRouter`), not services. The brief rewards
correct boundaries, not infrastructure. Each of those interfaces is where a queue or service would
go later.

Key decisions:

- **Perception is semantic, not CSS.** `snapshot.js` reports what an operator sees: role,
  accessible name, the *visual label* (the text in the neighbouring table cell, because legacy
  inputs have no `<label>`), table header and row context, and occlusion. This maps directly onto
  a desktop accessibility tree, and it is the reason the same artifact works on a relabeled
  tenant.
- **The planner proposes; the runtime disposes.** The model returns one structured
  `AgentAction`. The runtime checks the lease, the action allowlist and the risk rules before
  anything reaches the browser. The model never holds a credential: sign-on is a profile
  procedure that runs outside the loop. Caller values are written as `{{inputs.x}}` templates, so
  the trace is parameterized when it is recorded rather than guessed afterwards.
- **Discovery and replay share one Runtime.** The guardrails, redaction, evidence, interstitial
  handling and handoff are therefore identical in both modes. In particular, known interstitials
  are cleared by profile handlers in discovery too. The model spends no turns on them, and they
  never get recorded as steps.
- **Three layers of knowledge**, each versioned separately:
  - The **AppProfile** holds what is true of a vendor product for everyone. It is curated once
    and reused by every capability and tenant.
  - The **Capability** is one task, recorded once.
  - The **Tenant** holds what is local to one institution.

Trade-offs accepted: replay is synchronous, so a handoff blocks the caller (see Cuts). The offline
`MockPlanner` is hand-written heuristics over the real observation contract. It is honest about
being a stand-in, and the seam is the point.

## Artifact schema

Source: `cua/schema.py`. Example: [`evidence/capabilities/member.lookup_savings_balance@1.0.0.json`](evidence/capabilities/).
One step, trimmed:

```json
{ "id": "read_savings_balance", "intent": "Read savings_balance from column 'Balance' of the row where {'Type': 'SAVINGS'}",
  "screen": "member_shares", "action": "extract", "output": "savings_balance", "risk": "safe",
  "target": { "key": "member_shares.savings_balance", "frame": ["work"], "strategies": [
      { "kind": "table_cell", "headers_include": ["Share","Type","Description","Balance","Available"],
        "row_match": {"Type": "SAVINGS"}, "column": "Balance",
        "why": "row addressed by its key column(s), not position: row order varies per record" } ] } }
```

The top level is a **contract** first and a step list second:

- **`inputs`**: typed (`string|integer|decimal|enum`) with `pattern`, `enum` and `minimum`, plus
  a `sensitivity` that drives redaction. Inputs are validated *before* a browser starts.
- **`outputs`**: typed. Money is carried as decimal *strings*, never floats.
- **`outcomes`**: the business answers a caller must handle, such as `MEMBER_NOT_FOUND` and
  `ACCESS_RESTRICTED`. They are part of the contract, so the agent knows "not found" is an answer
  to relay rather than an error to retry. They are copied in from the profile for the screens the
  flow traverses.
- **`steps[]`**: each step has an `intent` (for the reviewer), a precondition `screen`, an
  `action`, a `target`, a `value` template, a `risk`, and postcondition `expect` checkpoints:
  `screen` transitions, plus field read-back after fills.
- **`success`**: the final checkpoint. All required outputs must also be present and type-valid.
- **`app`**: the profile id, profile version and start screen. **`provenance`**: discovery run,
  redacted goal, planner, compiler, and notes such as dropped strategies or human interventions.
- **`status`**: `draft → approved → deprecated`. **`approvals[]`** are bound to a SHA-256 of the
  behavioural content, so any edit voids them. Versioning is semver. When a flow is re-recorded, the compiler bumps the
  **major** version if the input/output contract changed and the **minor** version otherwise.

**Targets carry several independent strategies, ranked by expected stability.**

| strategy | why it ranks there |
|---|---|
| `role_name` | Role plus accessible name. It is what the operator reads, and it survives layout changes. |
| `role_label` | Role plus visual label. It covers legacy table layouts, where inputs have no accessible name. |
| `table_cell` | The data-grid cell is addressed by *row key + column header*, never by position. |
| `attribute` | Generated field names (`f_0012`) and canonical routes with parameterized ids (`/work/member/{{inputs.member_number}}/shares`). Opaque to humans, but stable within a product version. |
| `css_path` | Structural path. Last resort for *actions* only, and mostly a drift signal. |

The compiler keeps a strategy only if it matches **exactly one** element on the screen it was
recorded on. It drops any strategy containing a redacted non-input value, and writes a note
explaining why. One deliberate rule, found by testing: positional fallbacks are **never** used for
data reads. A member without a savings row would otherwise silently return some other row's
balance.

Left out of the artifact on purpose: credentials (only named secret slots), concrete PII, base
URLs, and app-wide exception handling. Those live in the tenant binding and the profile, so a fix
to "session timeout" handling reaches every capability at once.

## Determinism & error handling

There is no model at replay. Determinism comes from never assuming that an action worked:

1. **Before each step**, the primary frame must be on the step's `screen`.
2. **Target resolution:** each strategy must match exactly one element.
   - The first strategy that does wins.
   - If a *different* non-structural strategy matches a *different* element, that is
     `TARGET_AMBIGUOUS`. The engine refuses to guess.
   - Winning with a fallback means the target has drifted. The result reports which strategy
     failed and which one carried it.
3. **After each action**, the engine polls a **state classifier** until the step's checkpoint
   holds or the step times out. It uses bounded polling, never fixed sleeps. The classifier
   assigns exactly one class, checked in this order:

| state | example | handling |
|---|---|---|
| `error` (profile signature) | `TRANSACTION ABENDED (CODE S0C7)` | stop → `failed APP_ABEND`, `retryable` taken from the signature |
| `interstitial` | session timeout · system-notice overlay · "PROCESSING - PLEASE WAIT" | run the profile handler (re-auth / acknowledge / wait), then **reposition** |
| `outcome` (declared) | `NO RECORD FOUND` · `ACCESS DENIED` · `MINIMUM OPENING DEPOSIT` | stop → `business_outcome` with the code and message |
| `screen` | matches the checkpoint | next step |
| `unknown` past the timeout | unrecognised security screen | escalate to a human; with no operator → `failed UNEXPECTED_STATE` |

**Result contract.** There are four statuses: `succeeded` (outputs), `business_outcome` (code),
`rejected` (bad input, unapproved capability or profile mismatch; nothing touched the app), and
`failed` (code, step, intent, expected vs observed, `retryable`, evidence paths). Recoverable
conditions are deliberately *not* a status. They are absorbed and listed in `recoveries[]`. That
keeps the three-way split the brief asks for (expected outcome / recoverable / hard failure) in
the type rather than in the caller's head.

**Repositioning** is the key to resuming correctly after a recovery or a handoff. The engine
re-classifies the screen and resumes at the start of the latest block of steps for that screen,
at or before `i+1`. If a session timeout dropped a POSTed search, it refills the form. If a human
already moved past the step, it moves forward with them. It never repositions to before an
executed **irreversible** step. That case becomes `INDETERMINATE_AFTER_IRREVERSIBLE` and goes to
a human, because a blind retry after "Post" could open two accounts.

**Evidence.** Every run writes a redacted `events.jsonl`: decisions with rationale, step actions
with the strategy used, recoveries, policy events, control transfers and human actions. Failures,
outcomes and escalations add a **masked screenshot** and a **redacted semantic snapshot** of every
frame. `evidence/` holds 17 scenarios. `python -m cua demo` regenerates them and checks each one
against its expected result.

## Heterogeneity & multi-tenant

**Surfaces.** The artifact never mentions the DOM. It speaks in frame or window paths, roles,
names, labels and table cells, and only a `Surface` knows how to produce them. The proxy target
already *is* the legacy-web case (framesets, table layout, no ids), so `role_label` and
`table_cell` are the strategies that matter there. A `DesktopSurface` would implement the same
interface over Windows UI Automation:

| artifact concept | Windows UI Automation equivalent |
|---|---|
| frame path | window/pane path |
| role, name | `ControlType`, `Name` |
| visual label | `LabeledBy`, or the nearest text to the left |
| `attribute` | `AutomationId` |
| `table_cell` | `GridPattern` |
| `css_path` | tree path |

For surfaces with no usable accessibility tree, such as Citrix or mainframe emulators, the plan is
to add one strategy kind, `visual` (an OCR text anchor plus a relative offset and an image hash),
ranked last. The step, checkpoint and outcome model doesn't change, because screens are already
identified by visible text.

**Reuse across tenants.** Many institutions run the same vendor product, configured differently.
The capability binds to `profile@major` (`acme-coreone@4`), not to a tenant. A tenant supplies a
base URL, the vault paths behind secret slots, and **target overrides** keyed by the capability's
stable target keys. Overrides are prepended to, or replace, the strategies for one control, and
the shared artifact is never forked. The demo replays the artifact recorded on Acme against Harbor
CU, which relabels "Member Number" as "Account No." and renames "Shares" to "Accounts":

- **Without overrides**, it still succeeds: the `attribute` fallbacks (`f_0012`, canonical href)
  carry it, and the result reports exactly which two targets drifted.
- **With Harbor's overrides**, it runs clean.

The drift report doubles as the to-do list for onboarding a new tenant.

**Detecting and managing drift** at scale needs these signals, aggregated per tenant, per target
and per profile version:

- the strategy index that resolved each target, which shows a primary strategy decaying;
- the rate of unknown states and outcomes per screen, which shows a new interstitial or a
  changed message;
- extraction type failures.

A vendor upgrade becomes a new profile version, validated by canary replays with synthetic test
members before the tenant's version pin moves. When drift is structural rather than cosmetic,
re-run discovery on that tenant and diff the new artifact against the shared one. The compiler
emits the same target keys, so the diff resolves to either an override or a new minor version.

## Escalation & handoff

**What counts as stuck:**

- the planner chooses `escalate`;
- the step budget, time budget or bad-decision budget runs out;
- a state stays `unknown` past the step timeout;
- an unknown element covers the target;
- an irreversible action needs approval;
- the result after an irreversible step can't be confirmed.

**Routing.** An `Intervention` carries:

- the kind of intervention and the reason;
- the capability or goal, the step and its intent;
- expected vs. observed state;
- a masked screenshot and a redacted snapshot;
- the CDP endpoint of the live browser;
- the allowed decisions (`resume | approve | abort`).

It goes to the operator console's queue.

**The control model** (`cua/control.py`). One controller holds the session at a time:

`AGENT → PAUSED → HUMAN → AGENT`

- Every transition increments an **epoch**, which acts as a fencing token. Automation re-checks
  its epoch before *every* action. An operator can therefore take over unprompted, mid-run
  (`/api/takeover`), and the agent's next action is refused instead of racing the human.
- While control is away, automation pumps the browser's event loop but issues nothing.
- The human works in the *same* session: the headed window, or anything attached to the
  localhost CDP endpoint. The demo's scripted operator is a separate process that does exactly
  that.
- Page-level listeners report clicks and changes. The lease decides attribution, so anything that
  happens while a human holds control is recorded on the intervention as theirs. Values are
  redacted, and password values are never sent.
- **Resolve hands control back.** The engine re-observes and repositions instead of trusting a
  description of what the human did. If nobody answers within the SLA, the run expires and fails
  closed.
- In discovery, human steps are excluded from the compiled flow, and a note suggests modeling the
  situation as a profile interstitial.

**Real vs. mocked:**

- *Real:* the lease and fencing, same-session control over CDP, action capture, resume through
  repositioning, approval gating, and the expiry path.
- *Mocked:* the console is one HTML page. The production version would stream the CDP screencast
  (co-browsing), route requests by skill and SLA, and let replay return `pending_human` with a
  resumable run handle instead of blocking the caller.

## Safety

**Guardrail model** (`cua/policy.py`):

- **URL allowlist.** The tenant origin plus path globs, with `/__admin/*` and `/signoff`
  explicitly blocked. It is enforced at the **network layer** for every request from every frame,
  including redirects and anything the human operator does, and again on link targets before a
  click.
- **Action allowlist** by action type.
- **Risk.** Effective risk is the **maximum** of the risk the artifact declares and the profile's
  control rules. An edited artifact can raise risk but never lower it.

**Irreversible actions** are handled by blocking plus human approval:

- **In discovery**, a model never commits a transaction on its own. Exploring with real money is
  the wrong place for autonomy.
- **In replay**, a capability with irreversible steps must be *approved*, and that approval is
  hash-bound. The step itself also needs either a per-invocation pre-approval (`--approve
  click_post`, for an agent product that has its own confirmation UX) or a live human approval.
  Draft capabilities with irreversible steps are refused outright. A human may also perform the
  step themselves, and automation then never repeats it.
- **Steps are never auto-retried after an irreversible action.**

**Data handling:**

- Credentials never reach the model, artifacts or logs. They are injected only at the moment a
  sign-on or re-auth handler runs.
- Redaction has three layers:
  - **taints:** caller inputs, secrets, extracted outputs, and values seen in profile-marked
    sensitive labels or columns;
  - **patterns:** SSN, phone, date, e-mail and long digit runs;
  - **field-level masking** of outputs by sensitivity.
- Sensitive grid columns such as `Balance` are masked *in what the planner sees*. The model picks
  the cell by row key and column without ever seeing the number; the runtime reads it
  deterministically. That is data minimization, not only log hygiene.
- Screenshots mask sensitive cells, tainted free text and password fields.
- Persisted `result.json` masks PII outputs. Only the in-process return to the caller carries
  them.
- The demo checks outputs in memory, and a grep of `evidence/` for the synthetic names, SSNs,
  balances and credentials comes back empty.

**Limits:**

- Risk rules based on control names are a heuristic backstop. Production needs explicit,
  reviewed risk annotations per control in the profile.
- Redaction is best-effort. A name printed in free text is caught only if the run has already
  seen it in a labeled cell. The evidence store must therefore still be treated as regulated data
  (encryption, retention, access control), and discovery should run against sandbox tenants and
  test members.
- The CDP endpoint is unauthenticated on localhost. Production needs an authenticated broker.
- Free-text screenshot masking is coarse: it blanks the whole line.
- Confirmation numbers are classified `internal` and are logged.

## Cuts

- **No live LLM run in the evidence.** No API key was available. `ClaudePlanner`
  (`claude-opus-5-5`, strict-schema tool, stateless per turn, refusal fallback) is written against
  the current SDK and import-checked, but has not been run. The committed discovery runs use
  `MockPlanner`, which reads the real observations but has hand-written task knowledge for the two
  goal families. It is the first thing to swap in.
- **Synchronous replay.** Handoffs block the caller. Next: durable run state, a `pending_human`
  status, and resume after a process restart.
- **No desktop or visual surface.** The design is above; the interface is not implemented.
- **Bare operator console.** No co-browsing stream and no queue or SLA routing.
- **Small gaps:**
  - native JS dialogs are only dismissed and logged;
  - "member has no savings share" should be a declared business outcome, but surfaces as
    `TARGET_NOT_FOUND`;
  - sign-on is a profile procedure rather than a composable sub-capability.
- **Done from the stretch list:**
  - an agent-facing catalog (tool definitions + `invoke`);
  - a draft → approved gate bound to the content hash;
  - canonicalized routes plus a second tenant with per-tenant overrides.

  Next would be multi-run stability scoring to gate approval, and a bounded, policy-checked
  single-step LLM recovery that is recorded as evidence.
