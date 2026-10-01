"""WebSurface: Playwright/Chromium implementation of the Surface seam.

Perception goes through ``snapshot.js`` (roles, names, visual labels, table
context) rather than raw CSS, because the target apps rarely have clean DOMs.
The browser is started with a localhost CDP endpoint so a human operator can
attach to *this* live session during a handoff.
"""

from __future__ import annotations

import socket
from urllib.parse import urlparse
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

from ..policy import PolicyGate
from ..redact import Redactor
from ..schema import Attribute, CssPath, RoleLabel, RoleName, TableCell, Target, render
from . import FrameNotFound, Observation, Resolved, TargetAmbiguous, TargetBlocked, TargetNotFound

SNAPSHOT_JS = (Path(__file__).parent / "snapshot.js").read_text("utf-8")

# Reports what a human does while they hold control. Values typed into
# password fields are never sent; other values are redacted on the Python side.
HUMAN_CAPTURE_JS = r"""
(() => {
  if (window.__cuaHooked) return; window.__cuaHooked = true;
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const label = (el) => {
    const td = el.closest("td");
    for (let p = td && td.previousElementSibling; p; p = p.previousElementSibling) {
      const t = norm(p.innerText); if (t) return t;
    }
    return "";
  };
  const isButton = (el) => el.tagName === "BUTTON" || (el.tagName === "INPUT" && ["submit","button","reset","image"].includes(el.type));
  const describe = (el) => ({
    tag: el.tagName.toLowerCase(), field: el.getAttribute("name"), label: label(el),
    name: isButton(el) ? norm(el.value || el.innerText) : (["INPUT","SELECT","TEXTAREA"].includes(el.tagName) ? "" : norm(el.innerText).slice(0, 60)),
  });
  const send = (d) => { try { window.cuaHumanEvent && window.cuaHumanEvent(d); } catch (e) {} };
  document.addEventListener("click", (e) => {
    const el = e.target.closest("a,button,input,select,td");
    if (el) send({ kind: "click", frame: window.name || "", ...describe(el) });
  }, true);
  document.addEventListener("change", (e) => {
    const el = e.target;
    const value = el.type === "password" ? null : (el.tagName === "SELECT" ? el.options[el.selectedIndex]?.text : el.value);
    send({ kind: "change", frame: window.name || "", ...describe(el), value });
  }, true);
})();
"""


# Tag the innermost element around any text node containing a known-sensitive
# value, so free text ("NO RECORD FOUND FOR MEMBER 12345") is masked too.
MASK_TEXT_JS = r"""
(values) => {
  const w = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) {
    if (values.some((v) => n.textContent.includes(v)) && n.parentElement) n.parentElement.setAttribute("data-cua-sensitive", "1");
  }
}
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _norm(s: str | None) -> str:
    return " ".join((s or "").split()).lower()


class WebSurface:
    def __init__(self, base_url: str, gate: PolicyGate, redactor: Redactor, *,
                 sensitive_labels: list[str], sensitive_patterns: list[str], sensitive_columns: list[str] = (),
                 headless: bool = True,
                 on_blocked_request: Callable[[str, str], None] | None = None,
                 on_human_event: Callable[[dict], None] | None = None,
                 on_dialog: Callable[[str, str], None] | None = None):
        self.base_url = base_url.rstrip("/")
        self.gate = gate
        self.redactor = redactor
        self.headless = headless
        self.snapshot_opts = {"sensitiveLabels": sensitive_labels, "sensitivePatterns": sensitive_patterns,
                              "sensitiveColumns": list(sensitive_columns)}
        self.on_blocked_request = on_blocked_request or (lambda url, reason: None)
        self.on_human_event = on_human_event or (lambda e: None)
        self.on_dialog = on_dialog or (lambda kind, msg: None)
        self.endpoint: str | None = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        port = _free_port()
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless, args=[f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1"])
        self.endpoint = f"http://127.0.0.1:{port}"
        self.context = self._browser.new_context(viewport={"width": 1100, "height": 720})
        self.context.route("**/*", self._guard)
        self.context.expose_binding("cuaHumanEvent", lambda source, payload: self.on_human_event(payload))
        self.context.add_init_script(HUMAN_CAPTURE_JS)
        self.page = self.context.new_page()
        self.page.on("dialog", self._dialog)

    def close(self) -> None:
        for fn in (self.context.close, self._browser.close, self._pw.stop):
            try:
                fn()
            except Exception:
                pass

    def _guard(self, route, request) -> None:
        v = self.gate.check_url(request.url)
        if v.allowed:
            route.continue_()
        else:
            self.on_blocked_request(request.url, v.reason)
            route.abort("blockedbyclient")

    def _dialog(self, dialog) -> None:
        # Native dialogs are never accepted blindly: dismiss (the safe answer) and report.
        self.on_dialog(dialog.type, dialog.message)
        dialog.dismiss()

    # ---------------------------------------------------------------- frames
    def _frame(self, path: list[str]):
        f = self.page.main_frame
        for name in path:
            f = next((c for c in f.child_frames if c.name == name and not c.is_detached()), None)
            if f is None:
                raise FrameNotFound(f"frame {'/'.join(path)} not present")
        return f

    def _frames(self):
        out, stack = [], [([], self.page.main_frame)]
        while stack:
            path, f = stack.pop(0)
            out.append((path, f))
            stack.extend((path + [c.name], c) for c in f.child_frames if not c.is_detached())
        return out

    def goto(self, path: str) -> None:
        if urlparse(self.page.url).path == path:
            return  # already there; reloading a frameset would detach frames under us
        self.page.goto(self.base_url + path, wait_until="load")
        self.settle()

    def frame_text(self, frame: list[str]) -> list[str] | None:
        try:
            f = self._frame(frame)
            raw = f.evaluate("() => document.body ? document.body.innerText : ''")
        except (FrameNotFound, PWError):
            return None
        return [" ".join(l.split()) for l in raw.splitlines() if l.strip()]

    def current_url(self) -> str:
        return self.page.url

    # ------------------------------------------------------------ perception
    def snapshot(self, frame: list[str]) -> dict:
        return self._frame(frame).evaluate(SNAPSHOT_JS, self.snapshot_opts)

    def observe(self) -> Observation:
        """All frames, with element refs made global and sensitive values redacted."""
        frames = []
        for path, f in self._frames():
            try:
                snap = f.evaluate(SNAPSHOT_JS, self.snapshot_opts)
            except PWError:
                continue
            if not snap["elements"] and not snap["text"]:
                continue
            prefix = "/".join(path) or "top"
            for e in snap["elements"]:
                e["ref"] = f"{prefix}:{e['ref']}"
                if e.get("sensitive"):
                    self.redactor.taint(e["name"], e.get("label") or "sensitive")
            frames.append({"path": path, "url": snap["url"], "title": snap["title"],
                           "text": snap["text"], "elements": snap["elements"]})
        frames = self.redactor.obj(frames)
        primary = next((f["text"] for f in frames if f["path"] == ["work"]), frames[0]["text"] if frames else [])
        return Observation(frames=frames, primary_text=primary, url=self.redactor.text(self.page.url))

    # --------------------------------------------------------------- targets
    @staticmethod
    def _matches(s, e: dict, inputs: dict[str, str]) -> bool:
        if isinstance(s, RoleName):
            return e["role"] == s.role and _norm(e["name"]) == _norm(render(s.name, inputs))
        if isinstance(s, RoleLabel):
            return e["role"] == s.role and _norm(e.get("label")) == _norm(render(s.label, inputs))
        if isinstance(s, Attribute):
            if s.role and e["role"] != s.role:
                return False
            v = e.get("attrs", {}).get(s.attr)
            return v is not None and fnmatch(v, render(s.value, inputs))
        if isinstance(s, TableCell):
            t = e.get("table") or {}
            headers = t.get("headers") or []
            if e["role"] != "cell" or not t.get("row_values") or t.get("header") != s.column:
                return False
            if not set(s.headers_include) <= set(headers):
                return False
            for col, want in s.row_match.items():
                if col not in headers or _norm(t["row_values"][headers.index(col)]) != _norm(render(want, inputs)):
                    return False
            return True
        if isinstance(s, CssPath):
            return e["css"] == s.css
        return False

    def resolve(self, target: Target, inputs: dict[str, str]) -> Resolved:
        frame = self._frame(target.frame)
        snap = frame.evaluate(SNAPSHOT_JS, self.snapshot_opts)
        per = [[e for e in snap["elements"] if self._matches(s, e, inputs)] for s in target.strategies]
        first = next((i for i, m in enumerate(per) if len(m) == 1), None)
        if first is None:
            if any(len(m) > 1 for m in per):
                raise TargetAmbiguous(f"{target.key}: no strategy matched exactly one element "
                                      f"(counts {[len(m) for m in per]})")
            raise TargetNotFound(f"{target.key}: no strategy matched (tried {[s.kind for s in target.strategies]})")
        chosen = per[first][0]
        warnings = []
        for i, m in enumerate(per):
            if len(m) == 1 and m[0]["ref"] != chosen["ref"]:
                kind = target.strategies[i].kind
                if kind == "css_path":  # structural paths drift harmlessly; report, don't fail
                    warnings.append(f"css_path fallback now points elsewhere for {target.key}")
                else:
                    raise TargetAmbiguous(f"{target.key}: strategies {target.strategies[first].kind} and "
                                          f"{kind} matched different elements")
        if first > 0:
            warnings.append(f"{target.key}: primary strategy {target.strategies[0].kind} failed; "
                            f"matched by fallback #{first} {target.strategies[first].kind}")
        return Resolved(target_key=target.key, frame=target.frame, element=chosen, strategy_index=first,
                        strategy_kind=target.strategies[first].kind, warnings=warnings,
                        handle=frame.locator(f'[data-cua-ref="{chosen["ref"]}"]'))

    def resolve_ref(self, ref: str, element: dict) -> Resolved:
        """Discovery-time handle for an element from the latest observation."""
        prefix, local = ref.rsplit(":", 1)
        path = [] if prefix == "top" else prefix.split("/")
        frame = self._frame(path)
        return Resolved(target_key=ref, frame=path, element=element, strategy_index=0, strategy_kind="ref",
                        handle=frame.locator(f'[data-cua-ref="{local}"]'))

    # --------------------------------------------------------------- actions
    def perform(self, action: str, r: Resolved, value: str | None) -> str | None:
        if r.element.get("blocked"):
            raise TargetBlocked(f"{r.target_key} is covered by another element")
        loc = r.handle
        if action == "click":
            loc.click(timeout=5000)
        elif action == "fill":
            loc.fill(value or "", timeout=5000)
        elif action == "select":
            loc.select_option(label=value, timeout=5000)
        elif action == "press":
            loc.press(value or "Enter", timeout=5000)
        elif action == "extract":
            return loc.inner_text(timeout=5000)
        else:
            raise ValueError(f"unsupported action {action}")
        if action in ("click", "press"):
            self.settle()
        return None

    def read_value(self, r: Resolved) -> str:
        if r.element["role"] == "combobox":
            return r.handle.evaluate("el => el.options[el.selectedIndex]?.text || ''")
        return r.handle.input_value(timeout=3000)

    def settle(self) -> None:
        self.page.wait_for_timeout(150)
        for f in self.page.frames:
            try:
                f.wait_for_load_state("load", timeout=10_000)
            except PWError:
                pass

    def pump(self, ms: int) -> None:
        """Let Playwright dispatch events (route guard, human capture) while we wait."""
        self.page.wait_for_timeout(ms)

    # -------------------------------------------------------------- evidence
    def screenshot(self, path) -> str:
        """Screenshot with sensitive cells, tainted values and password fields masked."""
        masks = []
        for fpath, f in self._frames():
            try:
                snap = f.evaluate(SNAPSHOT_JS, self.snapshot_opts)
            except PWError:
                continue
            try:
                f.evaluate(MASK_TEXT_JS, self.redactor.values())
            except PWError:
                pass
            refs = [e["ref"] for e in snap["elements"]
                    if e.get("sensitive") or self.redactor.is_tainted(e.get("name", ""))]
            sel = ", ".join([f'[data-cua-ref="{r}"]' for r in refs] + ["[data-cua-sensitive]", "input[type=password]"])
            masks.append(f.locator(sel))
        self.page.screenshot(path=str(path), mask=masks, mask_color="#222222")
        return str(path)

    def snapshot_all(self) -> list[dict]:
        """Redacted semantic snapshot of every frame (our 'DOM snapshot' evidence)."""
        return self.observe().frames
