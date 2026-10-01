"""Human-in-the-loop: who controls the live session, and how control moves.

Control model
-------------
A run's browser session has exactly one controller at a time:

    AGENT  --raise()-->  PAUSED  --claim()-->  HUMAN  --resolve()-->  AGENT
      ^                                                                 |
      +------------------------- (epoch += 1 on every edge) -----------+

* ``epoch`` is a fencing token. Automation captures it when it (re)acquires
  control and checks it before *every* action; a stale epoch means a human
  took over and the action is not sent. This is what makes an operator's
  "take over now" safe even mid-flow.
* While PAUSED/HUMAN, automation keeps pumping the browser's event loop but
  issues no actions. Everything that happens in the page while a human holds
  the lease is attributed to that human and recorded on the intervention.
* On resolve, automation never assumes what the human did: it re-observes,
  re-classifies and repositions itself in the flow.

Operators reach the *same* session through the browser's localhost CDP
endpoint (attach DevTools, or a co-browsing console in production). The
operator console here is deliberately bare: a JSON API plus one HTML page.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import Field

from .schema import Model

AGENT, PAUSED, HUMAN = "agent", "paused", "human"
DECISIONS = {"resume", "approve", "abort"}


class ControlLost(Exception):
    """The agent's lease is stale: a human holds (or is about to hold) the session."""


class Intervention(Model):
    id: str
    run_id: str
    kind: str  # stuck | approval | unexpected_state | failure | takeover
    reason: str
    context: dict = Field(default_factory=dict)  # goal/capability, step, screen, url (redacted)
    screenshot: str | None = None
    snapshot: str | None = None
    cdp_endpoint: str | None = None
    allowed_decisions: list[str]
    status: str = "open"  # open | claimed | resolved | expired
    created_at: datetime
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    decision: str | None = None
    note: str | None = None
    resolved_at: datetime | None = None
    human_actions: list[dict] = Field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ControlChannel:
    def __init__(self, run_id: str, on_event: Callable[..., None]):
        self.run_id = run_id
        self._on_event = on_event
        self._lock = threading.RLock()
        self.holder = AGENT
        self.epoch = 0
        self.interventions: dict[str, Intervention] = {}
        self._active: str | None = None

    # ---- automation side ----------------------------------------------------
    def acquire(self) -> int:
        with self._lock:
            if self.holder != AGENT:
                raise ControlLost(f"session held by {self.holder}")
            return self.epoch

    def check(self, epoch: int) -> None:
        with self._lock:
            if self.holder != AGENT or self.epoch != epoch:
                raise ControlLost(f"lease epoch {epoch} is stale (holder={self.holder}, epoch={self.epoch})")

    def raise_intervention(self, kind: str, reason: str, allowed: list[str], **fields) -> Intervention:
        with self._lock:
            iv = Intervention(id=f"iv-{secrets.token_hex(3)}", run_id=self.run_id, kind=kind, reason=reason,
                              allowed_decisions=allowed, created_at=_now(), **fields)
            self.interventions[iv.id] = iv
            self._active = iv.id
            self._transition(PAUSED, f"intervention {iv.id} raised: {reason}")
        self._on_event("intervention_raised", intervention=iv.model_dump(mode="json"))
        return iv

    def expire(self, iv_id: str) -> None:
        with self._lock:
            iv = self.interventions[iv_id]
            if iv.status != "resolved":
                iv.status, iv.decision, iv.resolved_at = "expired", "abort", _now()
                self._active = None
                self._transition(AGENT, f"intervention {iv_id} expired; failing closed")
        self._on_event("intervention_expired", intervention_id=iv_id)

    # ---- operator side --------------------------------------------------------
    def claim(self, iv_id: str, operator: str) -> Intervention:
        with self._lock:
            iv = self.interventions[iv_id]
            if iv.status != "open":
                raise ValueError(f"intervention {iv_id} is {iv.status}")
            iv.status, iv.claimed_by, iv.claimed_at = "claimed", operator, _now()
            self._transition(HUMAN, f"{operator} took control")
        self._on_event("control_transferred", to=HUMAN, operator=operator, intervention_id=iv_id, epoch=self.epoch)
        return iv

    def take_over(self, operator: str, reason: str = "operator-initiated takeover") -> Intervention:
        """Unprompted takeover of a running session. The agent stops at its next lease check."""
        iv = self.raise_intervention("takeover", reason, ["resume", "abort"])
        return self.claim(iv.id, operator)

    def resolve(self, iv_id: str, decision: str, operator: str, note: str = "") -> Intervention:
        with self._lock:
            iv = self.interventions[iv_id]
            if iv.status not in ("open", "claimed"):
                raise ValueError(f"intervention {iv_id} is {iv.status}")
            if decision not in iv.allowed_decisions:
                raise ValueError(f"decision {decision!r} not allowed here ({iv.allowed_decisions})")
            iv.status, iv.decision, iv.note, iv.resolved_at = "resolved", decision, note, _now()
            iv.claimed_by = iv.claimed_by or operator
            self._active = None
            self._transition(AGENT, f"{operator} resolved {iv_id}: {decision}")
        self._on_event("control_transferred", to=AGENT, operator=operator, intervention_id=iv_id,
                       decision=decision, note=note, epoch=self.epoch)
        return iv

    def record_page_event(self, evt: dict) -> None:
        """Called for every user-level event in the page; kept only while a human holds control."""
        with self._lock:
            if self.holder != HUMAN or not self._active:
                return
            iv = self.interventions[self._active]
            evt = {"at": _now().isoformat(timespec="milliseconds"), "by": iv.claimed_by, **evt}
            iv.human_actions.append(evt)
        self._on_event("human_action", intervention_id=iv.id, action=evt)

    def active(self) -> Intervention | None:
        with self._lock:
            return self.interventions.get(self._active) if self._active else None

    def _transition(self, to: str, why: str) -> None:
        self.holder = to
        self.epoch += 1


def await_resolution(channel: ControlChannel, iv: Intervention, pump: Callable[[int], None],
                     timeout_s: int) -> Intervention:
    """Block the automation (while keeping the browser responsive) until a human resolves."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if channel.interventions[iv.id].status == "resolved":
            return channel.interventions[iv.id]
        pump(250)
    channel.expire(iv.id)
    return channel.interventions[iv.id]


# ------------------------------------------------------------- operator console

CONSOLE_HTML = """<!doctype html><meta charset=utf-8><title>Operator console</title>
<style>body{font:14px system-ui;margin:20px;max-width:1100px}pre{background:#f4f4f4;padding:8px;overflow:auto}
.iv{border:1px solid #ccc;border-radius:6px;padding:12px;margin:12px 0}img{max-width:100%;border:1px solid #999}
button{margin-right:6px;padding:6px 12px}</style>
<h2>Operator console <small id=st></small></h2><div id=list></div>
<script>
async function act(id, verb, decision){
  const op = localStorage.op || (localStorage.op = prompt('Operator name','operator'));
  await fetch(`/api/interventions/${id}/${verb}`, {method:'POST', headers:{'content-type':'application/json'},
    body: JSON.stringify({operator: op, decision, note: decision ? prompt('Note for the record','') : ''})});
  load();
}
async function load(){
  const r = await (await fetch('/api/state')).json();
  document.getElementById('st').textContent = `controller=${r.holder} epoch=${r.epoch}`;
  document.getElementById('list').innerHTML = r.interventions.slice().reverse().map(iv => `
   <div class=iv><b>${iv.id}</b> [${iv.kind}] <i>${iv.status}</i> ${iv.claimed_by ? 'by '+iv.claimed_by : ''}
   <p>${iv.reason}</p><pre>${JSON.stringify(iv.context, null, 1)}</pre>
   <p>Live session: attach to <code>${iv.cdp_endpoint}</code> (chrome://inspect &rarr; Configure) or use the headed window.</p>
   ${iv.status==='open' ? `<button onclick="act('${iv.id}','claim')">Take control</button>` : ''}
   ${['open','claimed'].includes(iv.status) ? iv.allowed_decisions.map(d => `<button onclick="act('${iv.id}','resolve','${d}')">${d}</button>`).join('') : ''}
   ${iv.human_actions.length ? `<pre>${JSON.stringify(iv.human_actions, null, 1)}</pre>` : ''}
   ${iv.screenshot ? `<img src="/api/interventions/${iv.id}/screenshot">` : ''}</div>`).join('') || '<p>No interventions.</p>';
}
load(); setInterval(load, 1500);
</script>"""


class OperatorConsole:
    def __init__(self, channel: ControlChannel, port: int = 0):
        from flask import Flask, abort, jsonify, request, send_file
        from werkzeug.serving import make_server

        app = Flask("operator-console")

        @app.get("/")
        def index():
            return CONSOLE_HTML

        @app.get("/api/state")
        def state():
            return jsonify(holder=channel.holder, epoch=channel.epoch,
                           interventions=[iv.model_dump(mode="json") for iv in channel.interventions.values()])

        @app.get("/api/interventions")
        def list_iv():
            status = request.args.get("status")
            return jsonify([iv.model_dump(mode="json") for iv in channel.interventions.values()
                            if not status or iv.status == status])

        @app.get("/api/interventions/<iv_id>/screenshot")
        def shot(iv_id):
            iv = channel.interventions.get(iv_id)
            if not iv or not iv.screenshot:
                abort(404)
            return send_file(Path(iv.screenshot).resolve())

        @app.post("/api/interventions/<iv_id>/claim")
        def claim(iv_id):
            try:
                return jsonify(channel.claim(iv_id, request.json.get("operator", "operator")).model_dump(mode="json"))
            except (KeyError, ValueError) as e:
                return jsonify(error=str(e)), 409

        @app.post("/api/interventions/<iv_id>/resolve")
        def resolve(iv_id):
            b = request.json
            try:
                iv = channel.resolve(iv_id, b["decision"], b.get("operator", "operator"), b.get("note", ""))
                return jsonify(iv.model_dump(mode="json"))
            except (KeyError, ValueError) as e:
                return jsonify(error=str(e)), 409

        @app.post("/api/takeover")
        def takeover():
            iv = channel.take_over(request.json.get("operator", "operator"))
            return jsonify(iv.model_dump(mode="json"))

        import logging
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        self._server = make_server("127.0.0.1", port, app, threaded=True)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True, name="operator-console").start()

    def close(self) -> None:
        self._server.shutdown()


class OperatorRouter:
    """Where intervention requests go. ``console``: wait for a person at the
    console URL. ``sim:<playbook>``: spawn a scripted operator process that
    uses the same API and attaches to the same browser over CDP (for demos and
    tests). ``none``: no human available -- interventions fail closed at once."""

    def __init__(self, mode: str, console: OperatorConsole | None, log_dir: Path):
        self.mode = mode
        self.console = console
        self.log_dir = log_dir
        self._procs: list[subprocess.Popen] = []

    @property
    def available(self) -> bool:
        return self.mode != "none"

    def notify(self, iv: Intervention) -> None:
        if self.mode == "console":
            print(f"\n>>> HUMAN NEEDED [{iv.kind}] {iv.reason}\n>>> Operator console: {self.console.url}\n"
                  f">>> Live session (CDP): {iv.cdp_endpoint}\n", file=sys.stderr)
        elif self.mode.startswith("sim:"):
            playbooks = self.mode.split(":", 1)[1].split(",")
            n = len(self._procs)
            playbook = playbooks[min(n, len(playbooks) - 1)]
            out = open(self.log_dir / f"operator-sim-{n + 1}.log", "w", encoding="utf-8")
            self._procs.append(subprocess.Popen(
                [sys.executable, "-m", "cua.operator_sim", "--console", self.console.url,
                 "--intervention", iv.id, "--playbook", playbook],
                stdout=out, stderr=subprocess.STDOUT, cwd=Path(__file__).resolve().parent.parent))

    def close(self) -> None:
        for p in self._procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


def dump(iv: Intervention) -> str:
    return json.dumps(iv.model_dump(mode="json"), indent=2)
