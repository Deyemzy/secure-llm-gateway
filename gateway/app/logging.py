"""Metadata-only request logs. Fields are allowlisted: anything not named here is dropped,
so prompt or completion text cannot end up in logs by accident."""

from __future__ import annotations

import json
import sys
import time
from typing import Any, TextIO

FIELDS = frozenset({
    "request_id", "method", "path", "status", "code", "outcome",
    "user", "team", "client_id", "model", "model_id",
    "input_tokens", "output_tokens", "reserved_usd", "cost_usd",
    "stop_reason", "input_redactions", "output_redactions", "output_redacted",
    "latency_ms", "upstream_ms", "source_ip", "detail", "ledger_id", "sub", "outcome_unknown",
    "upstream_called", "ledger_outcome",
})
MAX_STR = 128


def _clean(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, dict):  # only redaction counters: {str: int}
        return {str(k)[:32]: int(n) for k, n in v.items() if isinstance(n, int)}
    return str(v)[:MAX_STR]


class AuditLog:
    def __init__(self, stream: TextIO = sys.stdout):
        self.stream = stream

    def request(self, **fields: Any) -> dict:
        return self.event("request", **fields)

    def event(self, name: str, **fields: Any) -> dict:
        record = {"event": name, "ts": round(time.time(), 3)}
        record.update({k: _clean(v) for k, v in fields.items() if k in FIELDS and v is not None})
        self.stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        self.stream.flush()
        return record
