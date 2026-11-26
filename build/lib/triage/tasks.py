"""Todoist task sink, through the Sync endpoint's item_add command.

Todoist documents command idempotency on the Sync endpoint: a command whose uuid was
already executed is not executed again. The uuid is derived from the ledger key, so a
retry after a lost response cannot create a second task.

One configured project; the token itself is not limited to it, so the project check
lives in the code (here and in the allowlist body check)."""
from __future__ import annotations

import json
import uuid
from typing import Protocol

from triage.transport import HttpClient, TransportFailure, WriteOutcomeUnknown

API = "https://api.todoist.com/api/v1/sync"
LABEL = "triage-proposed"
NAMESPACE = uuid.UUID("7c1f2a52-8d0e-4f39-9f6b-2a4c1d0b7e11")


class TaskFailed(Exception):
    pass


def command_uuid(key: str, n: str) -> str:
    """Deterministic: a retry of the same task reuses the same uuid."""
    return str(uuid.uuid5(NAMESPACE, f"{key}:{n}"))


class TaskSink(Protocol):
    def create(self, title: str, link: str, key: str, n: str) -> str: ...


class TodoistSink:
    def __init__(self, http: HttpClient, project_id: str, token: str):
        self.http, self.project_id, self._token = http, str(project_id), token

    def create(self, title: str, link: str, key: str, n: str) -> str:
        cmd_uuid = command_uuid(key, n)
        command = {"type": "item_add", "uuid": cmd_uuid, "temp_id": f"t-{cmd_uuid[:8]}", "args": {
            "content": title, "description": link, "project_id": self.project_id, "labels": [LABEL],
        }}
        try:
            r = self.http.request("POST", API, form={"commands": json.dumps([command])},
                                  headers={"Authorization": f"Bearer {self._token}"})
        except (TransportFailure, WriteOutcomeUnknown) as exc:
            raise TaskFailed(type(exc).__name__) from None
        if not r.ok:
            raise TaskFailed(f"status {r.status}")
        data = r.json()
        if data.get("sync_status", {}).get(cmd_uuid) != "ok":
            raise TaskFailed("command not ok")
        return str(data.get("temp_id_mapping", {}).get(command["temp_id"], cmd_uuid))
