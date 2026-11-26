# Security model

This job lists one Gmail label or one Outlook folder, asks a model for a reply, saves
that reply as a **draft** in the right thread, and adds proposed tasks to **one** Todoist
project. Its only write to the mailbox is a draft: sending is left to a person, and the
program has no route to delete, move or forward. The email text does leave the mailbox
for the model endpoint, and short model-written task titles go to Todoist (see "Where
data goes").

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
  `messages.list` only with `labelIds` equal to the configured label (any other label,
  or a search without it, is refused); GET on a message and on a thread by id; POST on
  `users/me/drafts` only, with a body limited to `message.raw` + `message.threadId`.
  A read by id is not tied to the label at the transport: the ids come from the label
  listing, and the token itself can read any message. `drafts.send`, `messages.send`,
  the upload send endpoint, trash, modify and every DELETE raise `PolicyViolation`
  before a byte is emitted. Attachments are read as name, size and type from the message
  metadata; the attachment endpoint is not on the allowlist.
- **Residual risk.** The token itself can send and can read every message. Someone
  holding the refresh token, or code running outside this program, could send mail.
  Without `gmail.modify` the job cannot label a message as processed, so it relies on
  its ledger (ADR 0001). The draft carries an opaque `X-Triage-Key` header (a digest, no
  content); if the person sends the draft, that header reaches the correspondent and
  shows that a tool prepared the draft.

### Microsoft Graph

- **Rights the token really grants.** Delegated `Mail.ReadWrite` + `offline_access` on
  one signed-in mailbox, **without `Mail.Send`**: Microsoft refuses send and forward for
  this token. But Microsoft's permission reference describes delegated `Mail.ReadWrite`
  as "Allows the app to create, read, update, and delete email in user mailboxes", every
  folder included.
- **Restriction enforced by the code.** Listing only in the configured folder and in
  Drafts and Sent Items (to recognise our own draft): `mailFolders/inbox`, `archive` or
  any other folder is refused. GET on a message by id and on its attachment list with
  `$select=name,size,contentType` (never `contentBytes`); as with Gmail, a read by id
  is not tied to the folder at the transport. POST on
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
  still be wrong in ways no check sees: a promise nobody made, a confirmation the email
  only asked for, a dropped title. The local examples show all three
  (`examples/qwen3-8b/`). That is why it stays a draft.

### Scaleway identities

- **Rights.** The runtime identity has `SecretManagerSecretAccess`,
  `ObjectStorageObjectsRead` and `ObjectStorageObjectsWrite`; the deployer has
  `ContainerRegistryFullAccess` and `ServerlessJobsFullAccess`.
- **Restriction.** Two identities, neither can do the other's work (`terraform/main.tf`).
- **Residual risk.** Both policies are scoped to the **whole Scaleway project**, not to
  the job's secrets, bucket or registry namespace. Use a project that holds this job
  only.

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
Scaleway region: an EU-hosted model does not make the whole flow EU-only. Task titles
are written by the model from the email, so they can carry its subject matter (a
committee, a document name) to Todoist.

The ledger holds no email text. Between a draft and its tasks it holds the accepted
task titles (`task_plan`), so a resumed run never asks the model again; they are emptied
when the message is done. With bucket versioning on, earlier versions keep them until
the 180-day expiry.

## Logs

The JSONL journal accepts a fixed list of field names, and string values must be short
machine tokens (event names, reason codes, hashes). A subject, an address, a sentence or
a token cannot pass that check. A canary test fails if one does.

## Reporting

Open a private security advisory on this repository.
