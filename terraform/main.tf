# Draft-only mail triage on Scaleway, fr-par only.
# Validated locally with `terraform validate`; this repository deploys nothing.

locals {
  region = "fr-par"
  # Secret values are written in Secret Manager by a person, never by Terraform,
  # so they never sit in a Terraform state file.
  secrets = {
    GOOGLE_CLIENT_ID     = "triage-google-client-id"
    GOOGLE_CLIENT_SECRET = "triage-google-client-secret"
    GOOGLE_REFRESH_TOKEN = "triage-google-refresh-token"
    MS_TENANT_ID         = "triage-ms-tenant-id"
    MS_CLIENT_ID         = "triage-ms-client-id"
    MS_REFRESH_TOKEN     = "triage-ms-refresh-token"
    TODOIST_TOKEN        = "triage-todoist-token"
    LLM_API_KEY          = "triage-llm-api-key"
    S3_ACCESS_KEY        = "triage-ledger-access-key"
    S3_SECRET_KEY        = "triage-ledger-secret-key"
  }
}

resource "scaleway_registry_namespace" "triage" {
  name       = "draft-only-triage"
  region     = local.region
  project_id = var.project_id
  is_public  = false
}

resource "scaleway_secret" "job" {
  for_each   = local.secrets
  name       = each.value
  region     = local.region
  project_id = var.project_id
  tags       = ["draft-only-triage"]
}

resource "scaleway_object_bucket" "ledger" {
  name       = var.ledger_bucket
  region     = local.region
  project_id = var.project_id
  versioning {
    enabled = true
  }
  # Ledger entries carry no content; claims are short lived. Keep 180 days (see RUNBOOK).
  lifecycle_rule {
    id      = "expire-ledger"
    enabled = true
    expiration {
      days = 180
    }
  }
}

# Two identities: the job reads its secrets and its ledger; the deployer pushes images
# and updates the job. Neither can do the other's work.
resource "scaleway_iam_application" "job_runtime" {
  name        = "draft-only-triage-runtime"
  description = "Runs the hourly job: secrets read, ledger read and write."
}

resource "scaleway_iam_policy" "job_runtime" {
  name           = "draft-only-triage-runtime"
  application_id = scaleway_iam_application.job_runtime.id
  rule {
    project_ids          = [var.project_id]
    permission_set_names = ["SecretManagerSecretAccess", "ObjectStorageObjectsRead", "ObjectStorageObjectsWrite"]
  }
}

resource "scaleway_iam_application" "deployer" {
  name        = "draft-only-triage-deployer"
  description = "Monthly rebuild: pushes the image by digest and updates the job definition."
}

resource "scaleway_iam_policy" "deployer" {
  name           = "draft-only-triage-deployer"
  application_id = scaleway_iam_application.deployer.id
  rule {
    project_ids          = [var.project_id]
    permission_set_names = ["ContainerRegistryFullAccess", "ServerlessJobsFullAccess"]
  }
}

resource "scaleway_job_definition" "triage" {
  name                   = "draft-only-triage"
  region                 = local.region
  project_id             = var.project_id
  cpu_limit              = 280
  memory_limit           = 512
  local_storage_capacity = 1000
  image_uri              = "rg.fr-par.scw.cloud/${scaleway_registry_namespace.triage.name}/draft-only-triage@${var.image_digest}"
  startup_command        = ["python", "-m", "triage", "run"]
  timeout                = "15m"

  env = {
    MAIL_PROVIDER      = var.mail_provider
    GMAIL_LABEL_ID     = var.mail_provider == "gmail" ? var.mailbox : ""
    GRAPH_FOLDER_ID    = var.mail_provider == "graph" ? var.mailbox : ""
    TODOIST_PROJECT_ID = var.todoist_project_id
    LLM_BASE_URL       = "https://api.scaleway.ai/v1"
    LLM_MODEL          = var.llm_model
    LEDGER_BACKEND     = "s3"
    LEDGER_BUCKET      = scaleway_object_bucket.ledger.name
    LEDGER_PREFIX      = "ledger"
  }

  dynamic "secret_reference" {
    for_each = local.secrets
    content {
      secret_id   = scaleway_secret.job[secret_reference.key].id
      environment = secret_reference.key
    }
  }
}

# Hourly, in UTC, at minute 7 (away from the top of the hour).
resource "scaleway_job_trigger" "hourly" {
  job_definition_id = scaleway_job_definition.triage.id
  name              = "hourly"
  region            = local.region
  cron_config = {
    schedule = "7 * * * *"
    timezone = "UTC"
  }
}
