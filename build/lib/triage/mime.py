"""Builds the reply MIME for a Gmail draft: same thread, right headers, right direction."""
from __future__ import annotations

import html
import re
from email.message import EmailMessage
from email.policy import SMTP

MARKER_HEADER = "X-Triage-Key"
_RE = re.compile(r"^\s*(re|aw|ر)\s*:\s*", re.I)


def reply_subject(subject: str) -> str:
    subject = (subject or "").strip()
    return subject if _RE.match(subject) else f"Re: {subject}"


def html_body(text: str, lang: str) -> str:
    direction = "rtl" if lang == "ar" else "ltr"
    paragraphs = "".join(
        f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in text.strip().split("\n\n") if p.strip()
    )
    return f'<div dir="{direction}" lang="{lang}" style="text-align:{"right" if direction == "rtl" else "left"}">{paragraphs}</div>'


def build_reply(*, to: str, subject: str, in_reply_to: str, references: str,
                body: str, lang: str, marker: str) -> bytes:
    msg = EmailMessage(policy=SMTP)
    msg["To"] = to
    msg["Subject"] = reply_subject(subject)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (references + " " + in_reply_to).strip() if in_reply_to not in references else references
    # Opaque digest, no content. Lets a later run find this draft again (see ADR 0001).
    msg[MARKER_HEADER] = marker
    msg.set_content(body, subtype="plain", charset="utf-8", cte="base64")
    msg.add_alternative(html_body(body, lang), subtype="html", charset="utf-8", cte="base64")
    msg["Content-Language"] = lang          # after set_content, which clears Content-* headers
    return msg.as_bytes()
