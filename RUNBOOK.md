# Runbook

## Before the first run

1. **Confirm the Todoist scope.** Create a token with `task:add` only and run one
   `item_add` into the configured project. Todoist's docs do not list scopes per endpoint
   (SECURITY.md, Todoist).
2. **Google OAuth app status.** While the OAuth consent screen is external and in
   "Testing", Google issues refresh tokens that expire after 7 days: the job would stop
   reading after a week. Production path: publish the app (the Gmail scopes used are
   restricted scopes, so Google's verification applies), or use a Workspace internal app.
3. **Confirm conditional writes** on the ledger bucket (`If-None-Match: *` on PUT).
   Scaleway documents them for Object Storage; run `pytest tests/test_r5_once.py -k s3`
   against the real bucket once by pointing the S3 ledger at it.
4. **Graph marker.** The draft marker is written as a named extended property in the
   `message` parameter of `createReply`. Microsoft documents `message.body` there; the
   extended property was exercised against the fake only. Check it on a test mailbox.
5. **IAM permission set names** in `terraform/main.tf` are checked against the IAM
   catalogue at deployment.

## Stop future runs

Delete or disable the trigger: `terraform destroy -target=scaleway_job_trigger.hourly`
(or remove it in the console). The job definition and its secrets stay.

## Stop a run in progress

Scaleway console, Serverless Jobs, the job, Job runs: stop the running one. A stopped run can leave a message half processed: the next
run reconciles it (ADR 0001), it never writes a second draft.

## Detect a missed run, a missing secret, a revoked access

- **Missed run.** Every run writes `run_started` and `run_finished`. Alert when no
  `run_finished` is seen for 2 hours.
- **Secret unavailable.** The job refuses to start and lists the missing names (never
  the values) on stderr, exit code 2 (`python -m triage check-config` shows the same).
- **Access revoked.** A refresh fails with `AuthFailed`; the run stops with a non-zero
  exit code. Alert on any run with a non-zero exit code.
- **Ledger unavailable.** `ledger_unavailable` with exit code 3, before any read or
  write.

## Resume a message marked `needs_human`

The person gets one Todoist task "Manual reply needed" with the thread link, and replies
by hand. The ledger keeps `needs_human` with a reason code (`invalid_output`,
`injection_rule`, `unsupported_date`, `draft_outcome_unknown`, ...). The job never
retries such a message. To let the job try again, remove that message's entry from the
ledger (`entries/<key>.json`, key = sha256 of `provider|mailbox|message id`).

## Retention and access

- **Ledger.** No content: keys, effect names, opaque ids, timestamps. Kept 180 days
  (bucket lifecycle rule), versioned. Read and written by the runtime identity only.
- **Journal.** No content (SECURITY.md, Logs). Kept by Scaleway Cockpit under its own
  retention setting; readable by project members with Cockpit access.
- **Model provider.** The email text and the draft are sent to the inference endpoint.

## Roll back to the last validated image

Each monthly rebuild prints its digest in the run summary. Set the job definition back
to the previous digest: `terraform apply -var image_digest=sha256:<previous>` (Terraform
refuses a tag), or update the job definition's image in the console with
`rg.fr-par.scw.cloud/draft-only-triage/draft-only-triage@sha256:<previous>`.

## Open for production

- Real deployment (Scaleway project, registry, bucket, secrets, trigger) and the
  deployer identity's secrets in GitHub.
- OAuth consents for the real mailbox and Todoist, and the Google app status (step 2).
- Serverless inference (Paris today, Europe committed) or Dedicated Deployment (single
  region, paid).
- Confrontation with the existing code skeleton and control checklist.
