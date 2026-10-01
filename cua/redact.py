"""Redaction of regulated data before anything is logged, persisted, or sent to a model.

Three layers, applied in order:

1. Taints: concrete values the run knows are sensitive -- caller inputs marked
   pii/secret, resolved secrets, extracted outputs, and values seen in cells
   whose label the app profile marks sensitive (Name, SSN, ...). Replaced
   everywhere by a typed placeholder such as ``«member_number»``.
2. Patterns: SSNs, phone numbers, dates, e-mails, long digit runs (account
   numbers) -- catches what nobody told us about.
3. Structure: callers redact whole fields by sensitivity (see ``Redactor.value``).

The placeholder keeps logs debuggable ("filled «member_number» into ...")
without leaking the value.
"""

from __future__ import annotations

import re
from typing import Any

BASE_PATTERNS = {
    "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
    "email": r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b",
    "phone": r"\(\d{3}\)\s?\d{3}-\d{4}",
    "date": r"\b\d{2}/\d{2}/\d{4}\b",
    "account_number": r"\b\d{9,17}\b",
}


class Redactor:
    def __init__(self, extra_patterns: list[str] | None = None):
        self._taints: dict[str, str] = {}
        self._patterns = [(k, re.compile(p)) for k, p in BASE_PATTERNS.items()]
        self._patterns += [("sensitive", re.compile(p)) for p in (extra_patterns or [])]

    def taint(self, value: Any, label: str) -> None:
        s = str(value).strip() if value is not None else ""
        if len(s) >= 3:  # very short values ("00", "Y") would shred unrelated text
            self._taints.setdefault(s, f"«{label}»")

    def values(self) -> list[str]:
        return list(self._taints)

    def is_tainted(self, s: str) -> bool:
        return any(t in s for t in self._taints)

    def text(self, s: str) -> str:
        for value in sorted(self._taints, key=len, reverse=True):
            s = s.replace(value, self._taints[value])
        for name, rx in self._patterns:
            s = rx.sub(f"«{name}»", s)
        return s

    def obj(self, o: Any) -> Any:
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {k: self.obj(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v) for v in o]
        return o

    @staticmethod
    def value(v: Any, sensitivity: str, label: str) -> Any:
        """Field-level redaction for structured values with a declared sensitivity."""
        if sensitivity in ("pii", "secret"):
            return f"«{label}»"
        return v
