"""Classify what the application is showing *right now*.

Replay never assumes a click worked. After every action it asks "what state
am I in?" and the answer falls into exactly one bucket, checked in this order:

    error         a known hard-failure signature (ABEND)          -> stop
    interstitial  a known transient state (timeout, notice, wait)  -> recover
    outcome       a declared business outcome on this screen       -> return it
    screen        a known screen                                   -> compare to checkpoint
    unknown       none of the above                                -> keep polling, then escalate

Errors win over interstitials, which win over outcomes, because an overlay or
crash page can co-exist with a screen's title text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .profile import AppProfile, ErrorSignature, Interstitial
from .schema import OutcomeSpec, ScreenDef


@dataclass
class State:
    screen: str | None = None
    interstitial: Interstitial | None = None
    error: ErrorSignature | None = None
    error_detail: str | None = None
    outcome: OutcomeSpec | None = None
    outcome_message: str | None = None
    text: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        if self.error:
            return "error"
        if self.interstitial:
            return "interstitial"
        if self.outcome:
            return "outcome"
        return "screen" if self.screen else "unknown"

    def summary(self) -> dict:
        return {"kind": self.kind, "screen": self.screen,
                "interstitial": self.interstitial.id if self.interstitial else None,
                "error": self.error.code if self.error else None,
                "outcome": self.outcome.code if self.outcome else None,
                "headline": self.text[:3]}


class Classifier:
    def __init__(self, profile: AppProfile, extra_screens: list[ScreenDef] = (),
                 outcomes: list[OutcomeSpec] | None = None):
        self.profile = profile
        self.screens = list(profile.screens) + list(extra_screens)
        self.outcomes = profile.outcomes if outcomes is None else outcomes

    def classify(self, surface) -> State:
        text = surface.frame_text(self.profile.primary_frame)
        if text is None:  # no frameset (e.g. signed out): fall back to the top document
            text = surface.frame_text([]) or []
        st = State(text=text)
        for e in self.profile.errors:
            m = e.match.test(text)
            if m:
                st.error = e
                st.error_detail = m.group(0) if isinstance(m, re.Match) else None
                return st
        for i in self.profile.interstitials:
            if i.match.test(text):
                st.interstitial = i
                return st
        st.screen = next((s.id for s in self.screens if s.match.test(text)), None)
        for o in self.outcomes:
            if st.screen in o.screens:
                m = re.search(o.regex, "\n".join(text), re.I)
                if m:
                    st.outcome = o
                    line = next((l for l in text if re.search(o.regex, l, re.I)), m.group(0))
                    st.outcome_message = line.strip("* ")
                    break
        return st
