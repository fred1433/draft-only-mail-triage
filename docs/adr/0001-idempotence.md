# ADR 0001: one draft and one set of tasks per email

## Context

A Scaleway Serverless Job starts on an empty disk every hour. Without `gmail.modify` the
job cannot label a message as processed. Two runs can overlap. A network answer can be
lost after the provider performed the write. A person may edit, send or delete the
draft between two runs.

## Decision

1. **Ledger.** Durable state outside the container, key =
   `sha256(provider|mailbox|message_id)`, one entry per external effect:
   `draft_created`, `tasks_planned:<k>`, `task_created:<n>`, `needs_human`,
   `human_replied`, `done`. Values are opaque ids and reason codes, never content.
   Backends: local file (tests, demo) and S3 on Object Storage fr-par.
2. **Claim per message.** Conditional create (`O_EXCL` locally, `If-None-Match: *` on
   S3) with an expiry. A run that cannot claim skips the message.
3. **Ledger unavailable: stop.** Nothing is read or written by a run that cannot reach
   its ledger.
4. **Reconcile before writing.** Before creating a draft the job looks in the thread for
   a draft carrying its marker (header `X-Triage-Key` on Gmail, a named extended
   property on Graph) and for a reply already sent from the mailbox.
5. **Lost answer after a write.** Reconcile first. If the draft is there, record it. If
   it is not, the outcome is unknown: `needs_human`, never a blind second attempt.
6. **The person wins.** Once `draft_created` is recorded the job never writes that draft
   again, whether the person edited, sent or deleted it.
7. **Todoist.** Each task is a Sync `item_add` command whose uuid is
   `uuid5(namespace, key:n)`; Todoist does not execute a uuid twice. A resumed run never
   adds tasks beyond the first plan (`tasks_planned:<k>`).

## Consequences

- A resumed run asks the model again for task titles (the ledger holds no text); the
  draft is not touched.
- The Gmail marker header is an opaque digest. If the person sends the draft it travels
  with the message; it carries no content.
- If the ledger is lost **and** the person deleted the draft, the job would draft again.
  The ledger is versioned for that reason.
