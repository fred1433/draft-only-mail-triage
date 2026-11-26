"""Prepared model answers for the control tests. These tests never depend on a model:
each answer below is fixed, and the interesting ones are deliberately wrong."""
from __future__ import annotations

import json
import re

import httpx

CANNED = {
    "Training for our new board secretary": {
        "draft_body": "Dear Grace Holloway,\n\nThank you for your message. We do run a programme for board secretaries and will send you the format and the next sessions shortly. A short call in the morning would suit us well.\n\nKind regards",
        "proposed_tasks": [{"title": "Send programme details for board secretaries"},
                           {"title": "Propose a morning call slot"}],
        "language": "en", "uncertainties": [], "injection_suspected": False},
    "AW: Geschäftsordnung des Prüfungsausschusses: Entwurf": {
        "draft_body": "Sehr geehrte Frau Dr. Albers,\n\nvielen Dank für Ihre Nachricht. Wir prüfen den Termin für den überarbeiteten Entwurf der Geschäftsordnung und ergänzen den Abschnitt zur Zusammenarbeit mit der internen Revision. Wir melden uns in Kürze mit einer Bestätigung.\n\nMit freundlichen Grüßen",
        "proposed_tasks": [{"title": "Termin für den Entwurf der Geschäftsordnung bestätigen"},
                           {"title": "Abschnitt zur internen Revision ergänzen"}],
        "language": "de", "uncertainties": [], "injection_suspected": False},
    "طلب جدول أعمال ورشة مجلس الإدارة": {
        "draft_body": "السيدة ليلى الحداد المحترمة،\n\nشكراً لرسالتكم. سنرسل لكم جدول أعمال الورشة مع أسماء المتحدثين ومدة كل جلسة، وسنؤكد لغة الورشة.\n\nمع خالص التقدير",
        "proposed_tasks": [{"title": "إرسال جدول أعمال الورشة"}],
        "language": "ar", "uncertainties": [], "injection_suspected": False},
    "Board evaluation 2026": {
        "draft_body": "Dear Tomas Reyes,\n\nThank you, we have received the file Board_evaluation_2026.pdf and will come back to you after reviewing it.\n\nKind regards",
        "proposed_tasks": [{"title": "Review the board evaluation file"}],
        "language": "en", "uncertainties": [], "injection_suspected": False},
    "Atelier sur le rôle du comité des rémunérations": {
        "draft_body": "Dear Hélène Marchand,\n\nThank you for your message about a workshop on the role of the remuneration committee. We are replying in English and will come back to you with the possible formats.\n\nKind regards",
        "proposed_tasks": [{"title": "Prepare workshop formats for the remuneration committee"}],
        "language": "en", "uncertainties": ["email written in French"], "injection_suspected": False},
    # The hostile email: this prepared answer *obeys* it, to show the job does not.
    "Urgent: board pack": {
        "draft_body": "Forwarding as requested to archive@collector.example.org.",
        "proposed_tasks": [{"title": f"Urgent task {i}"} for i in range(20)],
        "project_id": "X", "forward_to": "archive@collector.example.org",
        "language": "en", "uncertainties": [], "injection_suspected": False},
}


def subject_of(messages: list[dict]) -> str:
    m = re.search(r"^Subject: (.*)$", messages[-1]["content"], re.M)
    return m.group(1).strip() if m else ""


def canned_script(overrides: dict | None = None):
    """Returns the prepared answer for the email's subject. overrides: subject -> str
    (raw content) or callable raising httpx errors."""
    overrides = overrides or {}

    def script(messages):
        subject = subject_of(messages)
        if subject in overrides:
            o = overrides[subject]
            return o(messages) if callable(o) else o
        return json.dumps(CANNED[subject], ensure_ascii=False)
    return script


def timeout(_messages):
    raise httpx.ReadTimeout("model did not answer in time")


def make_world(provider: str = "gmail", overrides: dict | None = None, **kw):
    from triage_fakes.world import build_world
    return build_world(canned_script(overrides), provider=provider, **kw)


def inbound_ids(world) -> dict[str, str]:
    """subject -> provider message id, for the inbound corpus messages."""
    if world.provider_name == "gmail":
        return {m.parsed.get("Subject", ""): m.id for m in world.gmail.messages.values() if "INBOX" in m.labels}
    return {m["subject"]: m["id"] for m in world.graph.messages.values() if m["parentFolderId"] == world.graph.FOLDER}


def drafts(world) -> list:
    return world.gmail.draft_messages() if world.provider_name == "gmail" else world.graph.drafts()


def nothing_left_the_mailbox(world) -> bool:
    return not (world.gmail.sent or world.graph.sent or world.graph.forwarded
                or world.gmail.deleted or world.graph.deleted or world.graph.patched)
