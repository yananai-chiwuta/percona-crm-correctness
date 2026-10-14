"""Allowlisted, append-only evidence from the synthetic integration suite."""

from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path


def json_default(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    raise TypeError(f"Unsupported evidence value: {type(value).__name__}")


class Evidence:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        for name in ("deliveries.jsonl", "transaction-phases.jsonl", "locking.jsonl", "sql-state.txt"):
            (directory / name).touch(exist_ok=True)

    def append(self, name, entry):
        line = json.dumps(entry, default=json_default, sort_keys=True, ensure_ascii=False)
        with self.lock:
            with (self.directory / name).open("a", encoding="utf-8") as output:
                output.write(line + "\n")

    def trace(self, case, entry):
        kind = str(entry.get("kind", ""))
        filename = "transaction-phases.jsonl" if "phase" in kind or "stage" in entry else "deliveries.jsonl"
        self.append(filename, {"case": case, "origin": "handler", "entry": entry})

    def phase(self, case, **entry):
        self.append("transaction-phases.jsonl", {"case": case, "timestamp": dt.datetime.now(dt.timezone.utc), **entry})

    def query(self, case, label, statement, params, rows):
        self.append("sql-state.txt", {"case": case, "label": label, "sql": statement, "params": params, "rows": rows})
