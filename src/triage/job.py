"""One hourly pass over one mailbox.

Order of operations for each message (ADR 0001):
  claim -> read ledger -> reconcile with the provider -> ask the model -> check the
  answer -> draft (once) -> tasks (each once) -> done.
Anything the code cannot vouch for becomes ``needs_human``: no draft, one task
"Manual reply needed" carrying the thread link and nothing else.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from triage.auth import AuthFailed
from triage.checks import detect_language, parse_reply, review, screen_injection
from triage.ledger import Ledger, LedgerUnavailable, ledger_key
from triage.llm import LLMClient, LLMUnavailable, build_messages, source_text
from triage.log import JsonlLog
from triage.providers.base import Found, InboundMessage, MailProvider
from triage.tasks import TaskFailed, TaskSink
from triage.transport import TransportFailure, WriteOutcomeUnknown

MANUAL_TITLE = "Manual reply needed"
TERMINAL = ("done", "human_replied")


@dataclass
class RunSummary:
    run_id: str
    seen: int = 0
    drafts: int = 0
    tasks: int = 0
    needs_human: int = 0
    skipped: int = 0
    retry_later: int = 0
    exit_code: int = 0
    outcomes: dict[str, str] = field(default_factory=dict)   # key prefix -> outcome code


def prepare(msg: InboundMessage) -> tuple[list[dict], str, str]:
    """The code, not the model, picks the reply language. Returns (messages, detected, target)."""
    detected = detect_language(msg.text) if msg.text.strip() else "en"
    target = detected if detected in ("en", "de", "ar") else "en"
    note = "" if detected != "other" else (
        "Note from the system: the email is in a language other than English, German or Arabic. "
        "Reply in English and say so in uncertainties.")
    return build_messages(msg, target, note), detected, target


class Job:
    def __init__(self, *, provider: MailProvider, llm: LLMClient, sink: TaskSink, ledger: Ledger,
                 log: JsonlLog, run_id: str | None = None):
        self.provider, self.llm, self.sink, self.ledger, self.log = provider, llm, sink, ledger, log
        self.run_id = run_id or uuid.uuid4().hex[:12]

    # ------------------------------------------------------------ run

    def run(self) -> RunSummary:
        s = RunSummary(self.run_id)
        started = time.monotonic()
        self.log.event("run_started", run=self.run_id, provider=self.provider.name)
        try:
            self.ledger.ping()
        except LedgerUnavailable:
            self.log.event("ledger_unavailable", run=self.run_id, status="stopped")
            s.exit_code = 3
            return s
        try:
            ids = self.provider.list_new()
        except TransportFailure:
            self.log.event("provider_unreachable", run=self.run_id, status="stopped")
            s.exit_code = 4
            return s
        except AuthFailed:
            self.log.event("auth_failed", run=self.run_id, status="stopped")
            s.exit_code = 5
            return s
        for message_id in ids:
            key = ledger_key(self.provider.name, self.provider.mailbox, message_id)
            s.seen += 1
            try:
                if not self.ledger.claim(key, self.run_id):
                    self.log.event("claimed_elsewhere", run=self.run_id, key=key)
                    s.skipped += 1
                    s.outcomes[key[:16]] = "claimed_elsewhere"
                    continue
                try:
                    outcome = self._handle(message_id, key, s)
                finally:
                    self.ledger.release(key, self.run_id)
            except LedgerUnavailable:
                self.log.event("ledger_unavailable", run=self.run_id, key=key, status="stopped")
                s.exit_code = 3
                return s
            except (AuthFailed, TransportFailure) as exc:
                self.log.event("stopped", run=self.run_id, key=key, reason=_code(exc))
                s.exit_code = 5 if isinstance(exc, AuthFailed) else 4
                return s
            s.outcomes[key[:16]] = outcome
        self.log.event("run_finished", run=self.run_id, count=s.seen, n=s.drafts,
                       ms=int((time.monotonic() - started) * 1000))
        return s

    # ------------------------------------------------------------ one message

    def _handle(self, message_id: str, key: str, s: RunSummary) -> str:
        state = self.ledger.get(key)
        if any(t in state for t in TERMINAL):
            s.skipped += 1
            return "already_done"
        if "needs_human" in state:
            if "task_created:manual" not in state:
                msg = self.provider.fetch(message_id)
                return self._manual_task(msg, key, s)
            s.skipped += 1
            return "already_needs_human"

        msg = self.provider.fetch(message_id)
        draft_done = "draft_created" in state
        if not draft_done:
            found = self.provider.reconcile(msg, key)
            if found in (Found.OUR_DRAFT, Found.OUR_DRAFT_SENT):
                self.ledger.record(key, "draft_created", ref=found.value, run_id=self.run_id)
                self.log.event("draft_reconciled", run=self.run_id, key=key, status=found.value)
                draft_done = True
            elif found is Found.HUMAN_REPLIED:
                self.ledger.record(key, "human_replied", run_id=self.run_id)
                self.log.event("human_replied", run=self.run_id, key=key)
                s.skipped += 1
                return "human_replied"

        if not msg.text.strip() and not msg.attachments:
            return self._needs_human(msg, key, "empty_body", s)

        messages, detected, target = prepare(msg)
        try:
            raw = self.llm.complete(messages)
        except LLMUnavailable as exc:
            self.log.event("llm_unavailable", run=self.run_id, key=key, reason=_code(exc))
            s.retry_later += 1
            return "retry_later"
        reasons = ["injection_rule"] if screen_injection(msg.text + " " + msg.subject) else []
        reply = parse_reply(raw)
        if reply is None:
            reasons.append("invalid_output")
        else:
            reasons += review(reply, source=source_text(msg), target_lang=target)
        if reasons:
            return self._needs_human(msg, key, reasons[0], s, count=len(reasons))
        if detected == "other":
            self.log.event("language_fallback", run=self.run_id, key=key, lang="en")

        if not draft_done:
            try:
                draft_id = self.provider.create_draft(msg, reply.draft_body, target, key)
            except WriteOutcomeUnknown:
                # The draft may exist. Look before anything else; never recreate blindly.
                found = self.provider.reconcile(msg, key)
                if found is not Found.OUR_DRAFT:
                    return self._needs_human(msg, key, "draft_outcome_unknown", s)
                draft_id = "reconciled"
            self.ledger.record(key, "draft_created", ref=draft_id, run_id=self.run_id)
            self.log.event("draft_created", run=self.run_id, key=key, lang=target)
            s.drafts += 1

        planned = [t.title for t in reply.proposed_tasks]
        recorded = [int(k.split(":")[1]) for k in state if k.startswith("tasks_planned:")]
        if recorded:
            planned = planned[:recorded[0]]      # a resumed run never adds tasks to the first plan
        else:
            self.ledger.record(key, f"tasks_planned:{len(planned)}", run_id=self.run_id)
            state = self.ledger.get(key)
        for n, title in enumerate(planned):
            if f"task_created:{n}" in state:
                continue
            try:
                task_id = self.sink.create(title, msg.link, key, str(n))
            except TaskFailed as exc:
                self.log.event("task_failed", run=self.run_id, key=key, n=n, reason=_code(exc))
                s.retry_later += 1
                return "tasks_pending"
            self.ledger.record(key, f"task_created:{n}", ref=task_id, run_id=self.run_id)
            self.log.event("task_created", run=self.run_id, key=key, n=n)
            s.tasks += 1
        self.ledger.record(key, "done", run_id=self.run_id)
        return "done"

    def _needs_human(self, msg: InboundMessage, key: str, reason: str, s: RunSummary, count: int = 1) -> str:
        self.ledger.record(key, "needs_human", ref=reason, run_id=self.run_id)
        self.log.event("needs_human", run=self.run_id, key=key, reason=reason, count=count)
        s.needs_human += 1
        return self._manual_task(msg, key, s, reason)

    def _manual_task(self, msg: InboundMessage, key: str, s: RunSummary, reason: str = "") -> str:
        try:
            task_id = self.sink.create(MANUAL_TITLE, msg.link, key, "manual")
        except TaskFailed as exc:
            self.log.event("task_failed", run=self.run_id, key=key, reason=_code(exc))
            s.retry_later += 1
            return "needs_human_task_pending"
        self.ledger.record(key, "task_created:manual", ref=task_id, run_id=self.run_id)
        self.log.event("task_created", run=self.run_id, key=key, n=0, rule="manual")
        s.tasks += 1
        return f"needs_human:{reason}" if reason else "needs_human"


def _code(exc: Exception) -> str:
    text = str(exc).lower().replace(" ", "_")
    return "".join(c for c in text if c.isalnum() or c in "_:.-")[:64] or type(exc).__name__.lower()
