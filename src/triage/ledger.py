"""Durable state, one record per message, no content.

A Serverless Job starts with an empty disk every hour, and without gmail.modify the job
cannot mark a message as processed. The ledger is what remembers. Key =
sha256(provider|mailbox|message_id). One state per external effect:
``task_plan``, ``draft_created``, ``task_created:<n>``, ``needs_human``,
``human_replied``, ``done``. Values are opaque references (draft id, task id, reason
code), never text from the email. One exception, declared: ``task_plan`` holds the
accepted task titles (the short model-written text that goes to Todoist) from just
before the draft is written until the tasks exist, so that a resumed run never asks the
model again. It is emptied when the message is done.

Two backends: a local file backend (the demo and the tests) and an S3-compatible backend
for Scaleway Object Storage in fr-par (written and tested against a fake; no bucket is
provisioned by this repository).
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from triage.transport import HttpClient, TransportFailure, WriteOutcomeUnknown


class LedgerUnavailable(Exception):
    """The run stops before any provider write."""


def ledger_key(provider: str, mailbox: str, message_id: str) -> str:
    return hashlib.sha256(f"{provider}|{mailbox}|{message_id}".encode()).hexdigest()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ledger(Protocol):
    def ping(self) -> None: ...
    def get(self, key: str) -> dict[str, dict]: ...
    def record(self, key: str, effect: str, ref: str = "", run_id: str = "") -> None: ...
    def claim(self, key: str, run_id: str, ttl_s: int = 900) -> bool: ...
    def release(self, key: str, run_id: str) -> None: ...


class FileLedger:
    """Atomic writes with os.replace; every claim decided under one exclusive file lock."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def ping(self) -> None:
        try:
            (self.root / "entries").mkdir(parents=True, exist_ok=True)
            (self.root / "claims").mkdir(parents=True, exist_ok=True)
            probe = self.root / ".probe"
            probe.write_text(_now())
        except OSError as exc:
            raise LedgerUnavailable(type(exc).__name__) from None

    @contextmanager
    def _locked(self):
        with open(self.root / ".lock", "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _entry(self, key: str) -> Path:
        return self.root / "entries" / f"{key}.json"

    def get(self, key: str) -> dict[str, dict]:
        try:
            return json.loads(self._entry(key).read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise LedgerUnavailable(type(exc).__name__) from None

    def record(self, key: str, effect: str, ref: str = "", run_id: str = "") -> None:
        try:
            with self._locked():
                state = self.get(key)
                state[effect] = {"at": _now(), "ref": ref, "run": run_id}
                tmp = self._entry(key).with_name(f"{key}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps(state, sort_keys=True))
                os.replace(tmp, self._entry(key))
        except OSError as exc:
            raise LedgerUnavailable(type(exc).__name__) from None

    def _write(self, path: Path, data: dict) -> None:
        """Atomic: a reader sees the old file or the new one, never half of one."""
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)

    def claim(self, key: str, run_id: str, ttl_s: int = 900) -> bool:
        """Read, decide and write under one exclusive lock. An unreadable claim counts
        as held until its file is older than the ttl."""
        path = self.root / "claims" / f"{key}.json"
        body = {"run": run_id, "until": time.time() + ttl_s, "released": False}
        try:
            with self._locked():
                try:
                    current = json.loads(path.read_text())
                except FileNotFoundError:
                    current = None
                except ValueError:
                    current = {"until": path.stat().st_mtime + ttl_s}
                if current is None or current.get("released") or current.get("until", 0) < time.time():
                    self._write(path, body)
                    return True
                return False
        except OSError as exc:
            raise LedgerUnavailable(type(exc).__name__) from None

    def release(self, key: str, run_id: str) -> None:
        path = self.root / "claims" / f"{key}.json"
        try:
            with self._locked():
                try:
                    current = json.loads(path.read_text())
                except (FileNotFoundError, ValueError):
                    return
                if current.get("run") == run_id:
                    current["released"] = True
                    self._write(path, current)
        except OSError as exc:
            raise LedgerUnavailable(type(exc).__name__) from None


class S3Ledger:
    """Scaleway Object Storage (S3 API) in fr-par, path-style, SigV4, through the
    allowlisted transport. Claims use conditional writes (If-None-Match / If-Match);
    whether the bucket honours them is checked once at deployment (see RUNBOOK)."""

    ENDPOINT = "https://s3.fr-par.scw.cloud"
    REGION = "fr-par"

    def __init__(self, http: HttpClient, bucket: str, prefix: str, access_key: str, secret_key: str):
        self.http, self.bucket, self.prefix = http, bucket, prefix.strip("/")
        self._ak, self._sk = access_key, secret_key

    def _sign(self, method: str, path: str, payload: bytes) -> dict[str, str]:
        now = dt.datetime.now(dt.timezone.utc)
        amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        digest = hashlib.sha256(payload).hexdigest()
        host = self.ENDPOINT.split("//")[1]
        canonical = "\n".join([method, path, "", f"host:{host}", f"x-amz-content-sha256:{digest}",
                               f"x-amz-date:{amz}", "", "host;x-amz-content-sha256;x-amz-date", digest])
        scope = f"{day}/{self.REGION}/s3/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canonical.encode()).hexdigest()])
        k = hmac.new(f"AWS4{self._sk}".encode(), day.encode(), hashlib.sha256).digest()
        for part in (self.REGION, "s3", "aws4_request"):
            k = hmac.new(k, part.encode(), hashlib.sha256).digest()
        sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
        return {"x-amz-date": amz, "x-amz-content-sha256": digest,
                "Authorization": f"AWS4-HMAC-SHA256 Credential={self._ak}/{scope}, "
                                 f"SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature={sig}"}

    def _call(self, method: str, name: str, body: bytes = b"", extra: dict | None = None):
        path = f"/{self.bucket}/{self.prefix}/{name}"
        headers = {**self._sign(method, path, body), **(extra or {})}
        try:
            return self.http.request(method, self.ENDPOINT + path, content=body or None, headers=headers)
        except (TransportFailure, WriteOutcomeUnknown) as exc:
            raise LedgerUnavailable(type(exc).__name__) from None

    def ping(self) -> None:
        r = self._call("PUT", "health/probe", _now().encode())
        if not r.ok:
            raise LedgerUnavailable(f"status {r.status}")

    def _read(self, name: str) -> tuple[dict, str | None]:
        r = self._call("GET", name)
        if r.status == 404:
            return {}, None
        if not r.ok:
            raise LedgerUnavailable(f"status {r.status}")
        return r.json(), r.headers.get("etag")

    def get(self, key: str) -> dict[str, dict]:
        return self._read(f"entries/{key}.json")[0]

    def record(self, key: str, effect: str, ref: str = "", run_id: str = "") -> None:
        for _ in range(5):
            state, etag = self._read(f"entries/{key}.json")
            state[effect] = {"at": _now(), "ref": ref, "run": run_id}
            cond = {"If-Match": etag} if etag else {"If-None-Match": "*"}
            r = self._call("PUT", f"entries/{key}.json", json.dumps(state, sort_keys=True).encode(), cond)
            if r.ok:
                return
            if r.status != 412:
                raise LedgerUnavailable(f"status {r.status}")
        raise LedgerUnavailable("contention")

    def claim(self, key: str, run_id: str, ttl_s: int = 900) -> bool:
        body = json.dumps({"run": run_id, "until": time.time() + ttl_s, "released": False}).encode()
        r = self._call("PUT", f"claims/{key}.json", body, {"If-None-Match": "*"})
        if r.ok:
            return True
        if r.status != 412:
            raise LedgerUnavailable(f"status {r.status}")
        current, etag = self._read(f"claims/{key}.json")
        if current.get("released") or current.get("until", 0) < time.time():
            r = self._call("PUT", f"claims/{key}.json", body, {"If-Match": etag or "*"})
            return r.ok
        return False

    def release(self, key: str, run_id: str) -> None:
        current, etag = self._read(f"claims/{key}.json")
        if current.get("run") == run_id:
            current["released"] = True
            self._call("PUT", f"claims/{key}.json", json.dumps(current).encode(), {"If-Match": etag or "*"})
