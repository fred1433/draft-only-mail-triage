# ADR 0001: one draft and one set of tasks per email

## Context

A Scaleway Serverless Job starts on an empty disk every hour. Without `gmail.modify` the
job cannot label a message as processed. Two runs can overlap. A network answer can be
lost after the provider performed the write. A person may edit, send or delete the
draft between two runs.

## Decision

1. **Ledger.** Durable state outside the container, key =
   `sha256(provider|mailbox|message_id)`, one entry per external effect:
   `task_plan`, `draft_created`, `task_created:<n>`, `needs_human`,
   `human_replied`, `done`. Values are opaque ids and reason codes, with one declared
   exception: `task_plan` holds the accepted task titles until the tasks exist.
   Backends: local file (tests, demo) and S3 on Object Storage fr-par.
2. **Claim per message.** Locally, read and write under one exclusive file lock, with
   atomic replace; on S3, `If-None-Match: *` and `If-Match`. Each claim has an expiry.
   A run that cannot claim skips the message. The race test runs 200 times per suite.
3. **Ledger unavailable: stop.** Nothing is read or written by a run that cannot reach
   its ledger.
4. **Plan before the first write.** Once the model's answer passes the checks, its task
   titles are recorded (`task_plan`) before the draft is written. A resumed message
   uses that plan and **never asks the model again**.
5. **Reconcile before writing.** Before creating a draft the job looks in the thread for
   a draft carrying its marker (header `X-Triage-Key` on Gmail, a named extended
   property on Graph) and for a reply already sent from the mailbox.
6. **Lost answer or stopped run after a write.** Reconcile first. If the draft is there,
   record it. If it is not (never written, or written and then deleted by a person), the
   outcome is unknown: `needs_human`, never a blind second attempt.
7. **Human edits take precedence.** Once `draft_created` is recorded the job never
   writes that draft again, whether the person edited, sent or deleted it.
8. **One message never blocks the others.** A provider error on one message is
   journaled (`message_failed`), the message is left for the next hour, and the run ends
   with exit code 6. Listing follows every page.
9. **Todoist.** Each task is a Sync `item_add` command whose uuid is
   `uuid5(namespace, key:n)`; Todoist does not execute a uuid twice. A resumed run never
   adds tasks beyond the recorded plan.

## Consequences

- A resumed run does not call the model. The price is that the ledger briefly holds task
  titles (SECURITY.md, Where data goes).
- The Gmail marker header is an opaque digest. If the person sends the draft it travels
  with the message; it carries no content.
- If the ledger is lost **and** the person deleted the draft, the job would draft again.
  The ledger is versioned for that reason.
