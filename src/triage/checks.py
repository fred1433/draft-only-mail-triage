"""Deterministic checks around the model: language, output schema, and what the model
is not allowed to add (figures, dates, names that are not in the email).

The model writes two kinds of free text: the draft body and short task titles. Both are
checked here before anything is written anywhere."""
from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

LANGS = {"en": "English", "de": "German", "ar": "Arabic"}

_STOP = {
    "en": {"the", "and", "to", "of", "you", "we", "is", "for", "please", "thank", "our", "with",
           "would", "could", "your", "are", "on", "in", "be", "this", "that", "kind", "regards"},
    "de": {"der", "die", "das", "und", "nicht", "ist", "wir", "sie", "ich", "mit", "für", "bitte",
           "vielen", "dank", "den", "dem", "zu", "noch", "unsere", "ihre", "grüße", "freundlichen",
           "sehr", "geehrte", "geehrter", "auf", "eine", "einen", "wie", "vor", "des"},
}


def detect_language(text: str) -> str:
    """'en', 'de', 'ar' or 'other'. Deterministic, so the code (not the model) picks the
    reply language."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "other"
    arabic = sum(1 for c in letters if "؀" <= c <= "ۿ" or "ݐ" <= c <= "ݿ")
    if arabic / len(letters) > 0.3:
        return "ar"
    words = re.findall(r"[a-zäöüß]+", text.lower())
    if not words:
        return "other"
    scores = {lang: sum(1 for w in words if w in stop) for lang, stop in _STOP.items()}
    best = max(scores, key=scores.get)
    if scores[best] < 2 or scores[best] / len(words) < 0.08:
        return "other"
    return best


class ProposedTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=80)


class ModelReply(BaseModel):
    """The only shape of model output the job accepts. No recipient, project, mailbox,
    endpoint or due date can be expressed in it."""
    model_config = ConfigDict(extra="forbid")
    draft_body: str = Field(min_length=1, max_length=4000)
    proposed_tasks: list[ProposedTask] = Field(max_length=3)
    language: Literal["en", "de", "ar"]
    uncertainties: list[str] = Field(default_factory=list, max_length=5)
    injection_suspected: bool


def json_schema() -> dict:
    return ModelReply.model_json_schema()


def parse_reply(raw: str) -> ModelReply | None:
    """None for anything that is not exactly a valid ModelReply."""
    text = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S).strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text)
    try:
        return ModelReply.model_validate(json.loads(text))
    except (ValueError, ValidationError):
        return None


_MONTHS = (
    "january february march april may june july august september october november december "
    "januar februar märz mai juni juli oktober dezember "
    "monday tuesday wednesday thursday friday saturday sunday "
    "montag dienstag mittwoch donnerstag freitag samstag sonntag"
).split()
_HONORIFIC = r"(?:Dr\.?|Prof\.?|Mr\.?|Mrs\.?|Ms\.?|Herr|Frau|Dear|Liebe|Lieber|Sehr geehrte[r]?\s+(?:Frau|Herr)(?:\s+Dr\.)?)"


def _norm(text: str) -> str:
    # Arabic-Indic and Eastern Arabic-Indic digits count as the same figures.
    return "".join(str(unicodedata.digit(c)) if c.isdigit() else c for c in text)


def _figures(text: str) -> set[str]:
    return {m.lstrip("0") or "0" for m in re.findall(r"\d+", _norm(text))}


# English words that are also months: counted as dates only next to a day number.
_AMBIGUOUS = {"may", "march"}


def _dates(text: str) -> set[str]:
    low = _norm(text.lower())
    found = set()
    for m in _MONTHS:
        if m in _AMBIGUOUS:
            pattern = rf"\b\d{{1,2}}(?:st|nd|rd|th|\.)?\s+(?:of\s+)?{m}\b|\b{m}\s+\d{{1,2}}\b"
        else:
            pattern = rf"\b{m}\b"
        if re.search(pattern, low):
            found.add(m)
    return found


def _names(text: str) -> set[str]:
    found = set()
    for m in re.finditer(_HONORIFIC + r"\s+((?:[A-ZÄÖÜ][\w\-']+)(?:\s+[A-ZÄÖÜ][\w\-']+)?)", text):
        found.update(m.group(1).split())
    return found


def unsupported(generated: str, source: str) -> list[str]:
    """Reason codes for figures, dates and names the model added. Source = everything the
    model was shown (headers it saw, body, attachment metadata)."""
    reasons = []
    if _figures(generated) - _figures(source):
        reasons.append("unsupported_figure")
    if _dates(generated) - _dates(source):
        reasons.append("unsupported_date")
    source_words = set(re.findall(r"[\w\-']+", source))
    if any(n not in source_words for n in _names(generated)):
        reasons.append("unsupported_name")
    return reasons


_INJECTION = re.compile(
    r"ignore (all |any )?(previous|prior|above) instructions|disregard (the|your) (rules|instructions)"
    r"|system prompt|you are now|forward (this|the) (thread|email|message)|ignoriere .*anweisungen"
    r"|create \d+ .*tasks|in project [A-Za-z0-9]+|send (it|this) to",
    re.I,
)


def screen_injection(text: str) -> bool:
    """A deterministic second opinion. It never replaces the schema: the model's output is
    bounded whatever this returns."""
    return bool(_INJECTION.search(text or ""))


def echoes(draft: str, source: str) -> bool:
    """True when the 'reply' mostly repeats the email back (a known small-model failure)."""
    a = re.sub(r"\s+", " ", draft).strip()
    b = re.sub(r"\s+", " ", source).strip()
    return len(a) > 40 and SequenceMatcher(None, a, b, autojunk=False).ratio() > 0.6


def review(reply: ModelReply, *, source: str, target_lang: str) -> list[str]:
    """Every reason to refuse a parsed reply. Empty list = the draft may be written."""
    reasons = []
    if reply.injection_suspected:
        reasons.append("injection_suspected")
    if reply.language != target_lang or detect_language(reply.draft_body) != target_lang:
        reasons.append("language_mismatch")
    generated = reply.draft_body + "\n" + "\n".join(t.title for t in reply.proposed_tasks)
    reasons += unsupported(generated, source)
    if echoes(reply.draft_body, source):
        reasons.append("echo_of_email")
    if any(re.search(r"https?://|@|www\.", t.title) for t in reply.proposed_tasks):
        reasons.append("task_title_carries_address")
    return reasons
