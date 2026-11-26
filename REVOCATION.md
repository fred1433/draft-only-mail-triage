# Revocation

Do these in order when a token may be exposed or the job must lose access now.

1. **Stop the job.** Remove the hourly trigger (RUNBOOK, Stop future runs) and stop any
   run in progress.
2. **Gmail.** In the Google account: Security, Third-party apps with account access,
   remove the app. Or revoke the refresh token at Google's revocation endpoint
   (`https://oauth2.googleapis.com/revoke`). Then delete the version in Secret Manager.
3. **Microsoft 365.** In Entra ID: Enterprise applications, the app, Users and groups or
   Permissions, revoke the user's consent; revoke the user's sessions to invalidate the
   refresh token. For the application variant, remove the RBAC for Applications
   assignment and the app's credentials.
4. **Todoist.** Settings, Integrations, remove the app's access (revokes the token).
5. **Model key.** Scaleway console, IAM, API keys: delete the key used by the job.
6. **Ledger keys and runtime identity.** Delete the runtime application's API key.
7. **Deployer.** Delete the deployer's API key and the GitHub repository secrets
   (`SCW_ACCESS_KEY`, `SCW_SECRET_KEY`, `SCW_PROJECT_ID`, `SCW_JOB_DEFINITION_ID`).

Restart only with new secrets written in Secret Manager by a person.
