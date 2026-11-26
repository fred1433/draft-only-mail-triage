"""Gmail, with gmail.readonly + gmail.compose. Reads one label, writes drafts only."""
from __future__ import annotations

import base64
import re
from email.utils import parseaddr

from triage.mime import MARKER_HEADER, build_reply
from triage.providers.base import Attachment, Found, InboundMessage
from triage.transport import HttpClient

API = "https://gmail.googleapis.com/gmail/v1/users/me"


class ProviderError(Exception):
    pass


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _walk(part: dict):
    yield part
    for sub in part.get("parts", []) or []:
        yield from _walk(sub)


class GmailProvider:
    name = "gmail"

    def __init__(self, http: HttpClient, label_id: str, token: callable):
        self.http, self.mailbox, self._token = http, label_id, token

    def _get(self, path: str, params=None) -> dict:
        r = self.http.request("GET", API + path, params=params,
                              headers={"Authorization": f"Bearer {self._token()}"})
        if not r.ok:
            raise ProviderError(f"gmail GET {r.status}")
        return r.json()

    def list_new(self) -> list[str]:
        data = self._get("/messages", {"labelIds": self.mailbox, "maxResults": "50", "q": "newer_than:7d"})
        return [m["id"] for m in data.get("messages", [])]

    def fetch(self, message_id: str) -> InboundMessage:
        m = self._get(f"/messages/{message_id}", {"format": "full"})
        payload = m["payload"]
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        text, html_fallback, attachments = [], [], []
        for part in _walk(payload):
            body = part.get("body", {}) or {}
            if part.get("filename"):
                # Metadata only: the attachment bytes are never requested.
                attachments.append(Attachment(part["filename"], int(body.get("size", 0)), part.get("mimeType", "")))
            elif part.get("mimeType") == "text/plain" and body.get("data"):
                text.append(_b64(body["data"]).decode("utf-8", "replace"))
            elif part.get("mimeType") == "text/html" and body.get("data"):
                html_fallback.append(re.sub(r"<[^>]+>", " ", _b64(body["data"]).decode("utf-8", "replace")))
        name, addr = parseaddr(headers.get("from", ""))
        _, reply_to = parseaddr(headers.get("reply-to", ""))
        return InboundMessage(
            provider=self.name, mailbox=self.mailbox, id=m["id"], thread_id=m["threadId"],
            rfc_message_id=headers.get("message-id", ""), references=headers.get("references", ""),
            subject=headers.get("subject", ""), sender=addr, sender_name=name, reply_to=reply_to or addr,
            text="\n".join(text or html_fallback).strip(), received_at=int(m.get("internalDate", 0)),
            link=f"https://mail.google.com/mail/u/0/#all/{m['threadId']}",
            attachments=tuple(attachments),
        )

    def reconcile(self, msg: InboundMessage, marker: str) -> Found:
        thread = self._get(f"/threads/{msg.thread_id}",
                           {"format": "metadata", "metadataHeaders": MARKER_HEADER})
        found = Found.NOTHING
        for m in thread.get("messages", []):
            labels = set(m.get("labelIds", []))
            headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
            ours = headers.get(MARKER_HEADER.lower()) == marker
            if "SENT" in labels and ours:
                return Found.OUR_DRAFT_SENT
            if "SENT" in labels and int(m.get("internalDate", 0)) > msg.received_at:
                found = Found.HUMAN_REPLIED
            elif "DRAFT" in labels and ours and found is Found.NOTHING:
                found = Found.OUR_DRAFT
        return found

    def create_draft(self, msg: InboundMessage, body: str, lang: str, marker: str) -> str:
        raw = build_reply(to=msg.reply_to, subject=msg.subject, in_reply_to=msg.rfc_message_id,
                          references=msg.references, body=body, lang=lang, marker=marker)
        r = self.http.request(
            "POST", API + "/drafts",
            json_body={"message": {"raw": base64.urlsafe_b64encode(raw).decode(), "threadId": msg.thread_id}},
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        if not r.ok:
            raise ProviderError(f"gmail drafts.create {r.status}")
        return r.json()["id"]
