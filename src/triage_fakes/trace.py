"""Records "one hour of the job, traced" for the page: the German follow-up, its draft in
the thread, a Todoist failure injected between draft and task, and the next hour that
creates the tasks without a second draft. Everything in the JSON comes from this run."""
from __future__ import annotations

import datetime as dt
import io
import json
import subprocess
import tempfile
from email import message_from_bytes
from email.policy import default as default_policy
from pathlib import Path

from triage.ledger import FileLedger
from triage.mime import MARKER_HEADER
from triage_fakes import Fault, recorded_script
from triage_fakes.world import ROOT, build_world

FILES = ("00_de_previous_outbound.eml", "02_de_audit_followup.eml")


def _calls(world, start: int) -> list[dict]:
    rules = world.http.allowlist.rules
    out = []
    for e in world.recorder.emitted[start:]:
        rule = next((r.name for r in rules if r.matches_route("https", e.host, e.method, e.path)
                     or r.matches_route("http", e.host, e.method, e.path)), "?")
        out.append({"method": e.method, "host": e.host, "path": e.path, "status": e.status, "rule": rule})
    return out


def record_trace(out: Path) -> dict:
    examples = ROOT / "examples" / "qwen3-8b"
    world = build_world(recorded_script(examples), only=FILES)
    inbound = next(m for m in world.gmail.messages.values() if "INBOX" in m.labels)
    p = inbound.parsed
    world.cloud.inject(Fault("POST", "api.todoist.com", r"/api/v1/sync", "http_503"))
    hours = []
    with tempfile.TemporaryDirectory(prefix="trace-ledger-") as tmp:
        for name in ("hour-1", "hour-2"):
            start = len(world.recorder.emitted)
            stream = io.StringIO()
            s = world.job(FileLedger(tmp), run_id=name, stream=stream).run()
            hours.append({
                "run": name, "drafts_created": s.drafts, "tasks_created": s.tasks,
                "retry_later": s.retry_later, "exit_code": s.exit_code,
                "calls": _calls(world, start),
                "journal": [json.loads(l) for l in stream.getvalue().splitlines()],
            })
        ledger_entry = json.loads(next(Path(tmp, "entries").glob("*.json")).read_text())
    (draft,) = world.gmail.draft_messages()
    d = draft.parsed
    record = json.loads((examples / "02_de_audit_followup.json").read_text())
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    trace = {
        "recorded_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": commit,
        "data": "synthetic data, mocked connectors",
        "email": {
            "file": f"corpus/{FILES[1]}", "from": str(p["From"]), "to": str(p["To"]),
            "subject": str(p["Subject"]), "date": str(p["Date"]), "message_id": str(p["Message-ID"]),
            "thread_id": inbound.thread_id, "body": p.get_body(("plain",)).get_content().strip(),
        },
        "model": {**record["model"], "generated_at_utc": record["generated_at_utc"],
                  "seconds": record["calls"][0]["seconds"]},
        "draft": {
            "thread_id": draft.thread_id, "same_thread": draft.thread_id == inbound.thread_id,
            "labels": draft.labels, "subject": str(d["Subject"]), "to": str(d["To"]),
            "in_reply_to": str(d["In-Reply-To"]), "references": str(d["References"]),
            "content_language": str(d["Content-Language"]), "marker": str(d[MARKER_HEADER])[:16],
            "body": d.get_body(("plain",)).get_content().strip(),
        },
        "tasks": [{k: t[k] for k in ("content", "description", "project_id", "labels")}
                  for t in world.todoist.tasks.values()],
        "ledger_entry": ledger_entry,
        "hours": hours,
        "totals": {"drafts_in_thread": len(world.gmail.draft_messages()), "sent": len(world.gmail.sent),
                   "tasks": len(world.todoist.tasks)},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(trace, indent=1, ensure_ascii=False))
    return trace
