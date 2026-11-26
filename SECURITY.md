# Security model

This job reads one mailbox folder, asks a model for a reply, saves that reply as a
**draft** in the right thread, and adds proposed tasks to **one** Todoist project.
Nothing leaves the mailbox without a person pressing Send. Nothing is deleted, moved or
forwarded.

That promise does not come from the OAuth scopes alone. For each connector below there
are three fields: what the token really allows, what the code adds on top, and what is
left over. Read them together.

### Gmail

- **Rights the token really grants.** `gmail.readonly` + `gmail.compose`. Google
  describes `gmail.compose` as "Manage drafts and send emails". **No Gmail scope lets
  an application create a draft without also letting it send**: `gmail.compose` can send
  (`messages.send`, `drafts.send`) and can delete drafts. It cannot trash or delete a
  message. `gmail.modify` and `https://mail.google.com/` are never requested (a test
  fails if they are).
- **Restriction enforced by the code.** `src/triage/transport.py`, `AllowlistTransport`:
  GET on labels, messages, threads and drafts; POST on `users/me/drafts` only, with a
  body limited to `message.raw` + `message.threadId`. `drafts.send`, `messages.send`,
  the upload send endpoint, trash, modify and every DELETE raise `PolicyViolation`
  before a byte is emitted. Attachments are read as name, size and type from the message
  metadata; the attachment endpoint is not on the allowlist.
- **Residual risk.** The token itself can send. Someone holding the refresh token, or
  code running outside this program, could send mail. Without `gmail.modify` the job
  cannot label a message as processed, so it relies on its ledger (ADR 0001).

### Microsoft Graph

- **Rights the token really grants.** Delegated `Mail.ReadWrite` + `offline_access` on
  one signed-in mailbox, **without `Mail.Send`**: Microsoft refuses send and forward for
  this token. But Microsoft's permission reference says `Mail.ReadWrite` allows the app
  to "create, read, update, and delete mail", in every folder of that mailbox.
- **Restriction enforced by the code.** GET on one configured folder, on the Drafts and
  Sent Items folders (to recognise our own draft), on a message and on its attachment
  list with `$select=name,size,contentType` (never `contentBytes`). POST on
  `messages/{id}/createReply` only, whose body may set the reply body and the triage
  marker and nothing else (no recipients). No `/send`, `/forward`, `/reply`,
  `createReplyAll`, `move`, PATCH or DELETE.
- **Residual risk.** The token can delete and edit any message in the mailbox. That
  limit lives in this program, not at Microsoft. The **application** variant (no signed-in
  user) covers every mailbox in the tenant; if it is ever used, it must be scoped to the
  one mailbox with RBAC for Applications in Exchange Online, and the unscoped Entra
  consent removed (Microsoft documents scoped grants as a union with Entra grants).

### Todoist

- **Rights the token really grants.** OAuth scope `task:add`, which Todoist describes
  as "Grants permission to add new tasks (the application cannot read or modify any
  existing data)". The token is not limited to a project.
- **Restriction enforced by the code.** One POST route, the Sync endpoint, with exactly
  one `item_add` command whose `project_id` equals `TODOIST_PROJECT_ID` and whose fields
  are `content`, `description`, `project_id`, `labels` (no due date). Each command
  carries a uuid derived from the ledger key: Todoist documents that it "will not
  execute a command that has same UUID as a previously executed command". Task titles
  are short and the description is the thread link: never the body of the email.
- **Residual risk.** The token can add tasks to any project. **Gap with the docs:**
  Todoist's documentation does not list the scope accepted by each endpoint, so the fact
  that `task:add` is enough for a Sync `item_add` is implied by the scope's wording, not
  stated. It must be confirmed on a real token before production (RUNBOOK, step 1).

### Model endpoint

- **Rights the token really grants.** A Scaleway Generative APIs key (IAM permission to
  call the inference API). No mailbox or Todoist right.
- **Restriction enforced by the code.** POST `{LLM_BASE_URL}/chat/completions` only.
  The model has no tools. It never chooses a mailbox, a thread, a project, a recipient
  or an endpoint: those come from configuration. The email is fenced as untrusted data.
  The answer must validate against a strict schema (`draft_body`, at most 3
  `proposed_tasks`, `language`, `uncertainties`, `injection_suspected`); extra fields
  are rejected. A draft or task that adds a figure, a date or a name absent from the
  email, replies in the wrong language, repeats the email back, or flags an injection is
  refused: no draft, one task "Manual reply needed" with the thread link.
- **Residual risk.** The email text and the draft go to the model provider. A reply can
  still be wrong in ways no check sees; that is why it stays a draft.

## What the allowlist does not do

The allowlist protects against **a mistake or a drift in this program**: a bad merge, a
library that adds a call, a model answer that tries to steer the job. It does **not**
protect against a **stolen token** or a compromised process, which can call the APIs
directly. Scaleway Serverless Jobs does not support VPC or Private Networks today, so
there is no network-level egress filter behind the allowlist. The answers to those are
short-lived access, rotation and revocation (REVOCATION.md).

## Where data goes

The job, its secrets (Secret Manager), its image registry and its ledger are declared in
**fr-par** only, with no fallback region. Inference: Scaleway's FAQ says serverless
models are "currently hosted in a secure data center located in Paris"; its commitment
for serverless is Europe, and it recommends Dedicated Deployment (a paid option) to keep
processing in a single region. Gmail, Microsoft 365 and Todoist are outside the
Scaleway region: an EU-hosted model does not make the whole flow EU-only.

## Logs

The JSONL journal accepts a fixed list of field names, and string values must be short
machine tokens (event names, reason codes, hashes). A subject, an address, a sentence or
a token cannot pass that check. A canary test fails if one does.

## Reporting

Open a private security advisory on this repository.
