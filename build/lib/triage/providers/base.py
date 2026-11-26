"""What the job needs from a mailbox, whichever provider holds it."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Attachment:
    """Metadata only. Attachments are never downloaded, opened or shown to the model."""
    filename: str
    size: int
    mime_type: str


@dataclass(frozen=True)
class InboundMessage:
    provider: str
    mailbox: str
    id: str
    thread_id: str
    rfc_message_id: str
    references: str
    subject: str
    sender: str
    sender_name: str
    reply_to: str
    text: str
    received_at: int                      # epoch milliseconds
    link: str                             # link to the thread, for the human
    attachments: tuple[Attachment, ...] = field(default_factory=tuple)


class Found(enum.Enum):
    NOTHING = "nothing"
    OUR_DRAFT = "our_draft_present"       # a draft carrying our marker is in the thread
    OUR_DRAFT_SENT = "our_draft_sent"     # the human sent our draft
    HUMAN_REPLIED = "human_replied"       # someone replied from the mailbox after this message


class MailProvider(Protocol):
    name: str
    mailbox: str

    def list_new(self) -> list[str]: ...
    def fetch(self, message_id: str) -> InboundMessage: ...
    def reconcile(self, msg: InboundMessage, marker: str) -> Found: ...
    def create_draft(self, msg: InboundMessage, body: str, lang: str, marker: str) -> str: ...
