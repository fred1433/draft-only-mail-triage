"""Content-free JSONL journal.

Only whitelisted field names, and string values limited to short machine tokens
(reason codes, effect names, hashes). A sentence, an address, a subject line or a token
cannot pass the value check, so they cannot reach the journal by accident."""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
from typing import IO

FIELDS = {"event", "run", "key", "provider", "effect", "reason", "count", "status",
          "lang", "attempt", "ms", "n", "rule", "at"}
_TOKEN = re.compile(r"^[a-z0-9_:.\-]{0,64}$")


class LogFieldRefused(ValueError):
    pass


class JsonlLog:
    def __init__(self, stream: IO[str]):
        self.stream = stream
        self._lock = threading.Lock()
        self.lines: list[dict] = []

    def event(self, event: str, **fields) -> None:
        fields = {"at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "event": event, **fields}
        for name, value in fields.items():
            if name not in FIELDS:
                raise LogFieldRefused(f"field {name!r} is not loggable")
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise LogFieldRefused(f"field {name!r} must be a short token or an integer")
            if isinstance(value, str) and name != "at" and not _TOKEN.fullmatch(value):
                raise LogFieldRefused(f"field {name!r} carries text, refused")
        if "key" in fields:
            fields["key"] = fields["key"][:16]          # short prefix is enough to correlate
        with self._lock:
            self.lines.append(fields)
            self.stream.write(json.dumps(fields, ensure_ascii=True, sort_keys=True) + "\n")
            self.stream.flush()
