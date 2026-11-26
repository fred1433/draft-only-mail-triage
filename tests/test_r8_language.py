"""R8: replies in the sender's language, in the sender's thread."""
from __future__ import annotations

import base64
from email import message_from_bytes
from email.policy import default

import pytest

from tests.helpers import make_world
from triage.checks import detect_language
from triage.ledger import FileLedger
from triage.mime import build_reply


def only_draft(tmp_path, *files):
    world = make_world(only=files)
    world.job(FileLedger(tmp_path)).run()
    (m,) = world.gmail.draft_messages()
    return world, m, m.parsed


def body_parts(parsed):
    plain = parsed.get_body(("plain",)).get_content()
    html = parsed.get_body(("html",)).get_content()
    return plain, html


@pytest.mark.evidence("R8", control="triage.job:prepare")
def test_german_email_gets_a_german_draft(tmp_path, proof):
    """German follow-up from an audit committee chair: the draft is in German."""
    _, _, p = only_draft(tmp_path, "00_de_previous_outbound.eml", "02_de_audit_followup.eml")
    plain, _ = body_parts(p)
    assert detect_language(plain) == "de" and p["Content-Language"] == "de"
    proof.line(f"detected draft language: {detect_language(plain)}; Content-Language: {p['Content-Language']}")


@pytest.mark.evidence("R8", control="triage.mime:html_body")
def test_arabic_email_gets_a_right_to_left_arabic_draft(tmp_path, proof):
    """Arabic agenda request: Arabic draft, HTML part marked dir=rtl lang=ar, UTF-8 intact."""
    _, _, p = only_draft(tmp_path, "03_ar_agenda_request.eml")
    plain, html = body_parts(p)
    assert detect_language(plain) == "ar"
    assert '<div dir="rtl" lang="ar"' in html
    assert p.get_body(("plain",)).get_content_charset() == "utf-8"
    assert "جدول أعمال" in plain
    proof.line('html part: <div dir="rtl" lang="ar" ...>; charset utf-8; Arabic text decodes intact')


@pytest.mark.evidence("R8", control="triage.job:prepare")
def test_other_language_gets_english_and_is_flagged(tmp_path, proof):
    """French email (outside the three): English draft, and the fallback is logged."""
    world, _, p = only_draft(tmp_path, "06_fr_other_language.eml")
    plain, _ = body_parts(p)
    assert detect_language(plain) == "en"
    assert any(l["event"] == "language_fallback" for l in world.log_lines())
    prompt = world.model.calls[0][1]["content"]
    assert "Reply in English and say so in uncertainties" in prompt
    proof.line("detected: other -> reply language: en; event language_fallback logged")


@pytest.mark.evidence("R8", control="triage.mime:build_reply")
def test_draft_sits_in_the_senders_thread(tmp_path, proof):
    """threadId, In-Reply-To, References and a matching 'Re:' subject: Gmail keeps the draft in the thread."""
    world, m, p = only_draft(tmp_path, "00_de_previous_outbound.eml", "02_de_audit_followup.eml")
    inbound = next(x for x in world.gmail.messages.values() if "INBOX" in x.labels)
    assert m.thread_id == inbound.thread_id
    assert p["In-Reply-To"] == "<5d2f-albers@nordwerk.example.org>"
    assert p["References"].split() == ["<a1-geschaeftsordnung@advisory.example.org>", "<5d2f-albers@nordwerk.example.org>"]
    assert p["Subject"] == "AW: Geschäftsordnung des Prüfungsausschusses: Entwurf"
    proof.line(f"draft threadId == inbound threadId ({m.thread_id}); In-Reply-To and References set; subject kept")


@pytest.mark.evidence("R8", control="triage_fakes:FakeGmail._create_draft")
def test_the_fake_enforces_gmail_threading_rules(proof):
    """Control: a draft without matching headers lands in a new thread in the fake, as the Gmail docs describe."""
    world = make_world(only=("01_en_board_secretary.eml",))
    inbound = next(iter(world.gmail.messages.values()))
    raw = build_reply(to="a@example.org", subject="Something else", in_reply_to="", references="",
                      body="Hello", lang="en", marker="k")
    r = world.gmail._create_draft({"message": {"raw": base64.urlsafe_b64encode(raw).decode(), "threadId": inbound.thread_id}})
    assert r.json()["message"]["threadId"] != inbound.thread_id
    proof.line("threadId alone, no References, other subject -> new thread (fake follows the documented rule)")
