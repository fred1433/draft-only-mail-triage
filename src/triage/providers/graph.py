"""Microsoft Graph, delegated Mail.ReadWrite + offline_access, no Mail.Send.

Microsoft refuses send and forward for this token. Mail.ReadWrite would allow deleting
and touching every folder: the allowlist keeps the job to one folder, reads, and
createReply."""
from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from triage.mime import html_body
from triage.providers.base import Attachment, Found, InboundMessage
from triage.transport import HttpClient

API = "https://graph.microsoft.com/v1.0/me"
# Named extended property holding the opaque marker (PublicStrings property set).
MARKER_PROP = "String {00020329-0000-0000-C000-000000000046} Name X-Triage-Key"


class ProviderError(Exception):
    pass


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000) if iso else 0


class GraphProvider:
    name = "graph"

    def __init__(self, http: HttpClient, folder_id: str, token: callable):
        self.http, self.mailbox, self._token = http, folder_id, token

    def _get(self, path: str, params=None, headers=None) -> dict:
        h = {"Authorization": f"Bearer {self._token()}", **(headers or {})}
        r = self.http.request("GET", API + path, params=params, headers=h)
        if not r.ok:
            raise ProviderError(f"graph GET {r.status}")
        return r.json()

    def list_new(self) -> list[str]:
        data = self._get(f"/mailFolders/{quote(self.mailbox, safe='')}/messages",
                         {"$select": "id", "$top": "50", "$orderby": "receivedDateTime desc"})
        return [m["id"] for m in data.get("value", [])]

    def fetch(self, message_id: str) -> InboundMessage:
        m = self._get(
            f"/messages/{quote(message_id, safe='')}",
            {"$select": "id,conversationId,subject,from,replyTo,body,internetMessageId,receivedDateTime,hasAttachments,webLink"},
            {"Prefer": 'outlook.body-content-type="text"'},
        )
        attachments = ()
        if m.get("hasAttachments"):
            a = self._get(f"/messages/{quote(message_id, safe='')}/attachments", {"$select": "name,size,contentType"})
            attachments = tuple(Attachment(x["name"], int(x["size"]), x.get("contentType", "")) for x in a.get("value", []))
        sender = (m.get("from") or {}).get("emailAddress", {})
        reply_to = ((m.get("replyTo") or [{}])[0] or {}).get("emailAddress", {}).get("address") if m.get("replyTo") else None
        return InboundMessage(
            provider=self.name, mailbox=self.mailbox, id=m["id"], thread_id=m["conversationId"],
            rfc_message_id=m.get("internetMessageId", ""), references="", subject=m.get("subject", ""),
            sender=sender.get("address", ""), sender_name=sender.get("name", ""),
            reply_to=reply_to or sender.get("address", ""),
            text=(m.get("body") or {}).get("content", "").strip(), received_at=_ms(m.get("receivedDateTime", "")),
            link=m.get("webLink", ""), attachments=attachments,
        )

    def _conversation(self, folder: str, conversation_id: str) -> list[dict]:
        data = self._get(f"/mailFolders/{folder}/messages", {
            "$filter": f"conversationId eq '{conversation_id}'",
            "$select": "id,sentDateTime,isDraft",
            "$expand": f"singleValueExtendedProperties($filter=id eq '{MARKER_PROP}')",
        })
        return data.get("value", [])

    def reconcile(self, msg: InboundMessage, marker: str) -> Found:
        def ours(m):
            return any(p.get("value") == marker for p in m.get("singleValueExtendedProperties", []) or [])
        sent = self._conversation("sentitems", msg.thread_id)
        if any(ours(m) for m in sent):
            return Found.OUR_DRAFT_SENT
        if any(_ms(m.get("sentDateTime", "")) > msg.received_at for m in sent):
            return Found.HUMAN_REPLIED
        if any(ours(m) for m in self._conversation("drafts", msg.thread_id)):
            return Found.OUR_DRAFT
        return Found.NOTHING

    def create_draft(self, msg: InboundMessage, body: str, lang: str, marker: str) -> str:
        # createReply builds the reply (recipients, subject, threading) server side and
        # saves it in Drafts. Only the body and the marker are set here.
        r = self.http.request(
            "POST", f"{API}/messages/{quote(msg.id, safe='')}/createReply",
            json_body={"message": {
                "body": {"contentType": "HTML", "content": html_body(body, lang)},
                "singleValueExtendedProperties": [{"id": MARKER_PROP, "value": marker}],
            }},
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        if not r.ok:
            raise ProviderError(f"graph createReply {r.status}")
        return r.json()["id"]
