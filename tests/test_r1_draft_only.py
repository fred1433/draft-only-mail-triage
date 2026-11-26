"""R1: no message leaves the mailbox without a human."""
from __future__ import annotations

import ast
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from tests.helpers import make_world, nothing_left_the_mailbox
from triage.transport import PolicyViolation
from triage_fakes.world import ROOT, file_ledger

T = "triage.transport:AllowlistTransport.check"
SEND_ROUTES = [
    ("POST", "https://gmail.googleapis.com/gmail/v1/users/me/drafts/send", {"id": "r1"}),
    ("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send", {"raw": "eA"}),
    ("POST", "https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send", {"raw": "eA"}),
    ("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/abc/trash", {}),
    ("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/abc/modify", {"removeLabelIds": ["INBOX"]}),
    ("DELETE", "https://gmail.googleapis.com/gmail/v1/users/me/messages/abc", None),
    ("DELETE", "https://gmail.googleapis.com/gmail/v1/users/me/drafts/r1", None),
    ("POST", "https://graph.microsoft.com/v1.0/me/messages/AAMk1/send", {}),
    ("POST", "https://graph.microsoft.com/v1.0/me/messages/AAMk1/forward", {"toRecipients": []}),
    ("POST", "https://graph.microsoft.com/v1.0/me/messages/AAMk1/reply", {"comment": "x"}),
    ("POST", "https://graph.microsoft.com/v1.0/me/messages/AAMk1/createReplyAll", {}),
    ("POST", "https://graph.microsoft.com/v1.0/me/sendMail", {"message": {}}),
    ("POST", "https://graph.microsoft.com/v1.0/me/messages/AAMk1/move", {"destinationId": "deleteditems"}),
    ("DELETE", "https://graph.microsoft.com/v1.0/me/messages/AAMk1", None),
    ("PATCH", "https://graph.microsoft.com/v1.0/me/messages/AAMk1", {"subject": "x"}),
    ("POST", "https://api.openai.com/v1/chat/completions", {"model": "gpt"}),
    ("GET", "https://exfil.example.net/collect", None),
]


@pytest.mark.evidence("R1", control=T)
@pytest.mark.parametrize("method,url,body", SEND_ROUTES, ids=[f"{m} {u.split('//')[1][:60]}" for m, u, b in SEND_ROUTES])
def test_send_delete_and_foreign_hosts_are_blocked_before_emission(method, url, body, proof):
    """Every send, forward, reply, move, delete or foreign host is refused before a byte is emitted."""
    world = make_world()
    before = len(world.recorder.emitted)
    with pytest.raises(PolicyViolation) as exc:
        world.http.request(method, url, json_body=body, headers={"Authorization": "Bearer t"})
    assert len(world.recorder.emitted) == before
    assert nothing_left_the_mailbox(world)
    proof.line(f"PolicyViolation: {exc.value}")
    proof.line(f"bytes that reached the network layer: {world.recorder.bytes_sent}")


@pytest.mark.evidence("R1", control="triage_fakes:FakeGmail.handle")
def test_the_fake_would_really_send_if_asked(proof):
    """Control of the control: without the allowlist the fake Gmail does send, so the test above is not empty."""
    world = make_world()
    draft = world.gmail._create_draft({"message": {"raw": base64.urlsafe_b64encode(b"Subject: x\r\n\r\nhi").decode()}})
    did = draft.json()["id"]
    r = world.gmail.handle(httpx.Request("POST", "https://gmail.googleapis.com/gmail/v1/users/me/drafts/send",
                                         json={"id": did}, headers={"Authorization": "Bearer t"}))
    assert r.status_code == 200 and len(world.gmail.sent) == 1
    proof.line(f"fake Gmail, reached directly: drafts.send -> {r.status_code}, sent={len(world.gmail.sent)}")


@pytest.mark.evidence("R1", control=T)
@pytest.mark.parametrize("provider", ["gmail", "graph"])
def test_corpus_replay_emits_only_allowlisted_calls(provider, tmp_path, proof):
    """The whole corpus, replayed through a recording transport: every emitted call is a read, a draft or a task."""
    world = make_world(provider)
    summary = world.job(file_ledger(tmp_path)).run()
    assert summary.exit_code == 0
    rules = world.http.allowlist.rules
    names = set()
    for e in world.recorder.emitted:
        match = [r.name for r in rules if r.matches_route("https", e.host, e.method, e.path)]
        assert match, f"emitted call outside the allowlist: {e}"
        names.add(match[0])
    writes = {n for n in names if any(w in n for w in ("create", "item_add"))}
    assert writes <= {"gmail.drafts.create", "graph.messages.createReply", "todoist.sync.item_add"}
    assert nothing_left_the_mailbox(world)
    proof.line(f"{provider}: {len(world.recorder.emitted)} calls emitted, all allowlisted")
    proof.line("routes used: " + ", ".join(sorted(names)))
    proof.line(f"sent=0 forwarded=0 deleted=0 drafts={summary.drafts} tasks={summary.tasks}")


@pytest.mark.evidence("R1", control="triage.job:Job.run")
def test_a_policy_violation_stops_the_run(tmp_path, proof, monkeypatch):
    """If code ever tried to send, the run stops on the violation; it is never swallowed."""
    from triage.providers import gmail as g
    original = g.GmailProvider.create_draft

    def create_then_send(self, msg, body, lang, marker):
        draft_id = original(self, msg, body, lang, marker)
        self.http.request("POST", g.API + "/drafts/send", json_body={"id": draft_id},
                          headers={"Authorization": "Bearer t"})
        return draft_id
    monkeypatch.setattr(g.GmailProvider, "create_draft", create_then_send)
    world = make_world()
    with pytest.raises(PolicyViolation) as exc:
        world.job(file_ledger(tmp_path)).run()
    assert world.gmail.sent == []
    proof.line(f"run stopped: {exc.value}")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and getattr(node.func, "id", "") == "__import__":
            found.add("__import__")
    return found


@pytest.mark.evidence("R1", control="triage.transport")
def test_one_network_module(proof):
    """Static check (ast): no smtplib, requests, urllib.request, socket or Google client; httpx only in transport.py."""
    banned = {"smtplib", "requests", "googleapiclient", "socket", "http", "urllib3", "aiohttp", "imaplib", "poplib", "__import__"}
    pkg = ROOT / "src" / "triage"
    httpx_users = []
    for f in sorted(pkg.rglob("*.py")):
        imported = _imports(f)
        assert not (imported & banned), f"{f.name} imports {imported & banned}"
        if "httpx" in imported:
            httpx_users.append(f.relative_to(ROOT).as_posix())
        assert "urllib.request" not in f.read_text()
    assert httpx_users == ["src/triage/transport.py"]
    proof.line(f"modules scanned: {len(list(pkg.rglob('*.py')))}; httpx imported by: {httpx_users}")


@pytest.mark.evidence("R1", control=T)
def test_mutations_make_the_suite_red(proof):
    """Mutation: inject drafts.send, messages.send, a Graph DELETE or an OpenAI host; the suite goes red, 0 bytes emitted."""
    if os.environ.get("TRIAGE_INSIDE_MUTATION"):
        pytest.skip("nested mutation run")
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "mutation_check.py"), "--json"],
                         capture_output=True, text=True, timeout=600)
    assert out.returncode == 0, out.stderr[-2000:]
    report = json.loads(out.stdout)
    for m in report:
        assert m["suite"] == "red", m
        assert m["bytes_emitted_by_mutant_call"] == 0, m
        proof.line(f"{m['mutation']}: suite red ({m['failed']} failed), blocked: {m['blocked']}")
