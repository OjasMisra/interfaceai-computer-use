"""The surface seam: how we perceive and act on an application.

Everything above this line (planner, compiler, artifact, replay engine) speaks
in terms of frames/windows, roles, names, labels and tables. Only a Surface
knows whether that comes from a browser DOM, a Windows UI Automation tree, or
OCR over pixels. WebSurface (Playwright) is the one implemented here; a
DesktopSurface would map:

    frame path      -> window / pane path
    role, name      -> UIA ControlType, Name
    visual label    -> LabeledBy, or nearest text element to the left
    attribute       -> AutomationId / ClassName
    css_path        -> UIA tree path (same "brittle fallback" status)
    table_cell      -> GridPattern / TablePattern lookups
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..schema import Target


class SurfaceError(Exception):
    code = "SURFACE_ERROR"


class FrameNotFound(SurfaceError):
    code = "FRAME_NOT_FOUND"


class TargetNotFound(SurfaceError):
    code = "TARGET_NOT_FOUND"


class TargetAmbiguous(SurfaceError):
    code = "TARGET_AMBIGUOUS"


class TargetBlocked(SurfaceError):
    """Target exists but something (an overlay, a dialog) is on top of it."""
    code = "TARGET_BLOCKED"


@dataclass
class Resolved:
    target_key: str
    frame: list[str]
    element: dict
    strategy_index: int
    strategy_kind: str
    warnings: list[str] = field(default_factory=list)
    handle: Any = None  # surface-private

    @property
    def drifted(self) -> bool:
        return self.strategy_index > 0


@dataclass
class Observation:
    """What the planner sees. Already redacted."""
    frames: list[dict]
    primary_text: list[str]
    url: str

    def element(self, ref: str) -> dict | None:
        for f in self.frames:
            for e in f["elements"]:
                if e["ref"] == ref:
                    return e
        return None

    def frame_of(self, ref: str) -> dict | None:
        for f in self.frames:
            if any(e["ref"] == ref for e in f["elements"]):
                return f
        return None


class Surface(Protocol):
    endpoint: str | None

    def start(self) -> None: ...
    def close(self) -> None: ...
    def goto(self, path: str) -> None: ...
    def frame_text(self, frame: list[str]) -> list[str] | None: ...
    def observe(self) -> Observation: ...
    def snapshot(self, frame: list[str]) -> dict: ...
    def resolve(self, target: Target, inputs: dict[str, str]) -> Resolved: ...
    def perform(self, action: str, r: Resolved, value: str | None) -> str | None: ...
    def read_value(self, r: Resolved) -> str: ...
    def settle(self) -> None: ...
    def pump(self, ms: int) -> None: ...
    def screenshot(self, path) -> str: ...
