"""Run evidence: an append-only, redacted JSONL event log plus files on failure.

Every event passes through the run's Redactor before it touches disk, so a log
line can never contain a value the run knows is sensitive.
"""

from __future__ import annotations

import json
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redact import Redactor

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"


class RunLog:
    def __init__(self, kind: str, redactor: Redactor, root: Path = RUNS_DIR, echo: bool = True):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = f"{kind}-{stamp}-{secrets.token_hex(2)}"
        self.dir = root / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor
        self.echo = echo
        self._seq = 0
        self._fh = (self.dir / "events.jsonl").open("a", encoding="utf-8")

    def event(self, type: str, **data: Any) -> dict:
        self._seq += 1
        rec = {"seq": self._seq, "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
               "type": type, **self.redactor.obj(data)}
        self._fh.write(json.dumps(rec, default=str) + "\n")
        self._fh.flush()
        if self.echo:
            brief = {k: v for k, v in rec.items() if k not in ("seq", "ts", "type", "observation")}
            line = json.dumps(brief, default=str)
            print(f"  [{rec['seq']:>3}] {type:<22} {line[:150]}", file=sys.stderr)
        return rec

    def path(self, name: str) -> Path:
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def write_json(self, name: str, obj: Any) -> Path:
        p = self.path(name)
        p.write_text(json.dumps(self.redactor.obj(obj), indent=2, default=str), encoding="utf-8")
        return p

    def close(self) -> None:
        self._fh.close()
