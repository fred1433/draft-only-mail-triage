"""R6: no content in logs."""
from __future__ import annotations

import io
import json

import pytest

from tests.helpers import make_world
from triage.ledger import FileLedger
from triage.log import JsonlLog, LogFieldRefused
from triage_fakes.world import ROOT

CANARIES = {
    "body": "CANARY-BODY-QFTX",
    "subject": "CANARY-SUBJECT-ZKMB",
    "sender": "canary.sender.wqpz@example.org",
    "draft": "CANARY-DRAFT-JRVH",
    "task": "CANARY-TASK-PLMV",
    "token": "CANARY-TOKEN-YBNC",
}


@pytest.mark.evidence("R6", control="triage.log:JsonlLog.event")
def test_canaries_never_reach_the_journal(tmp_path, proof):
    """Canary strings in body, subject, sender, draft, task title and token: none appears in the JSONL journal."""
    eml = (f"From: Canary <{CANARIES['sender']}>\r\nTo: office@advisory.example.org\r\n"
           f"Subject: Board minutes {CANARIES['subject']}\r\nDate: Wed, 25 Nov 2026 10:00:00 +0000\r\n"
           f"Message-ID: <canary@example.org>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
           f"Dear team, please confirm the board minutes {CANARIES['body']}. Thank you and kind regards.\r\n")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "canary.eml").write_text(eml)
    reply = json.dumps({"draft_body": f"Dear team,\n\nThank you, we will confirm the board minutes {CANARIES['draft']}.\n\nKind regards",
                        "proposed_tasks": [{"title": f"Confirm minutes {CANARIES['task']}"}],
                        "language": "en", "uncertainties": [], "injection_suspected": False})
    world = make_world(overrides={f"Board minutes {CANARIES['subject']}": reply}, corpus=corpus)
    from triage.auth import StaticToken
    stream = io.StringIO()
    job = world.job(FileLedger(tmp_path / "ledger"), stream=stream)
    job.provider._token = StaticToken(CANARIES["token"])
    job.sink._token = CANARIES["token"]
    job.run()
    journal = stream.getvalue()
    ledger_text = "".join(p.read_text() for p in (tmp_path / "ledger").rglob("*.json"))
    assert journal.count("\n") >= 3
    for where, canary in CANARIES.items():
        assert canary not in journal, f"{where} canary leaked into the journal"
        assert canary.lower() not in journal.lower()
        assert canary not in ledger_text, f"{where} canary leaked into the ledger"
    proof.line(f"journal lines: {journal.count(chr(10))}; canaries found: 0 of {len(CANARIES)} (journal and ledger)")
    proof.line("sample line: " + journal.splitlines()[1])


@pytest.mark.evidence("R6", control="triage.log:JsonlLog.event")
@pytest.mark.parametrize("field,value", [
    ("reason", "Dear Grace, thank you"), ("key", "g.holloway@harbourline.example.org"),
    ("status", "Training for our new board secretary"), ("subject", "x"), ("body", "x"),
])
def test_the_journal_refuses_text(field, value, proof):
    """A sentence, an address or an unknown field name is refused by the journal itself."""
    with pytest.raises(LogFieldRefused) as exc:
        JsonlLog(io.StringIO()).event("x", **{field: value})
    proof.line(f"{field}={value[:20]!r} -> {exc.value}")
