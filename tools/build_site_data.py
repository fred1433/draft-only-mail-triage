"""Builds the page's data files. Nothing here is written by hand: the register comes from
pytest's evidence.json, the traced hour from `triage trace`, and the examples from a
replay of the recorded Qwen outputs through the job.

    python tools/build_site_data.py --evidence <evidence.json> [--rebuild <rebuild-evidence.json>]
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import tempfile
from email import message_from_bytes
from email.policy import default as default_policy
from pathlib import Path

from triage.ledger import FileLedger, ledger_key
from triage_fakes import FakeGmail, recorded_script
from triage_fakes.trace import record_trace
from triage_fakes.world import CORPUS, ROOT, build_world

SITE = ROOT / "site" / "data"


def all_reasons(rec: dict | None) -> list[str]:
    """Every reason the job's checks give for a recorded output (the journal keeps the first)."""
    from triage.checks import parse_reply, review, screen_injection
    if not rec:
        return []
    call = rec["calls"][0]
    user = call["messages"][1]["content"]
    reasons = ["injection_rule"] if screen_injection(user) else []
    reply = parse_reply(call["output"])
    if reply is None:
        return reasons + ["invalid_output"]
    return reasons + review(reply, source=user, target_lang=rec["target_language"])


def examples() -> dict:
    ex_dir = ROOT / "examples" / "qwen3-8b"
    world = build_world(recorded_script(ex_dir))
    with tempfile.TemporaryDirectory() as tmp:
        summary = world.job(FileLedger(tmp), stream=io.StringIO()).run()
    reasons = {}
    for line in world.log_lines():
        if line["event"] == "needs_human":
            reasons[line["key"]] = line["reason"]
    drafts_by_thread = {m.thread_id: m.parsed for m in world.gmail.draft_messages()}
    rows = []
    for f in sorted(CORPUS.glob("*.eml")):
        p = message_from_bytes(f.read_bytes(), policy=default_policy)
        gm = next((m for m in world.gmail.messages.values() if m.parsed["Message-ID"] == p["Message-ID"]), None)
        if gm is None or "INBOX" not in gm.labels:
            continue
        key = ledger_key("gmail", FakeGmail.LABEL, gm.id)[:16]
        rec_path = ex_dir / (f.stem + ".json")
        rec = json.loads(rec_path.read_text()) if rec_path.exists() else None
        body = p.get_body(("plain",))
        draft = drafts_by_thread.get(gm.thread_id)
        attachments = [{"name": a.get_filename(), "type": a.get_content_type()} for a in p.iter_attachments()]
        rows.append({
            "file": f"corpus/{f.name}",
            "from": str(p["From"]), "subject": str(p["Subject"] or ""),
            "body": body.get_content().strip() if body else "",
            "attachments": attachments,
            "detected_language": rec["detected_language"] if rec else None,
            "model_output": rec["calls"][0]["output"] if rec else None,
            "model_seconds": rec["calls"][0]["seconds"] if rec else None,
            "outcome": summary.outcomes.get(key, ""),
            "reason": reasons.get(key, ""),
            "all_reasons": all_reasons(rec),
            "draft_written": draft is not None,
            "draft_body": draft.get_body(("plain",)).get_content().strip() if draft else "",
            "tasks": [t["content"] for t in world.todoist.tasks.values() if gm.thread_id in t["description"]],
        })
    model = json.loads(next(ex_dir.glob("*.json")).read_text())["model"]
    return {"model": model, "rows": rows,
            "totals": {"emails": summary.seen, "drafts": summary.drafts, "needs_human": summary.needs_human,
                       "tasks": summary.tasks}}


def main() -> int:
    a = argparse.ArgumentParser()
    a.add_argument("--evidence", required=True)
    a.add_argument("--rebuild")
    args = a.parse_args()
    SITE.mkdir(parents=True, exist_ok=True)
    evidence = json.loads(Path(args.evidence).read_text())
    req = json.loads((ROOT / "evidence" / "requirements.json").read_text())
    for row in evidence["rows"]:
        row["lead"] = req.get(row["id"], {}).get("lead", "")
    (SITE / "evidence.json").write_text(json.dumps(evidence, indent=1, ensure_ascii=False))
    if args.rebuild:
        shutil.copy(args.rebuild, SITE / "rebuild.json")
    record_trace(SITE / "trace.json")
    (SITE / "examples.json").write_text(json.dumps(examples(), indent=1, ensure_ascii=False))
    print("site data written:", ", ".join(sorted(p.name for p in SITE.glob("*.json"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
