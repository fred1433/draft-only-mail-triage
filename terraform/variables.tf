variable "project_id" {
  description = "Scaleway project that holds the job."
  type        = string
}

variable "image_digest" {
  description = "sha256 digest of the image pushed by the monthly rebuild (sha256:...)."
  type        = string
  validation {
    condition     = can(regex("^sha256:[0-9a-f]{64}$", var.image_digest))
    error_message = "Deploy by digest only, never by tag."
  }
}

variable "mail_provider" {
  description = "gmail or graph"
  type        = string
  default     = "gmail"
}

variable "mailbox" {
  description = "Gmail label id or Graph folder id the job reads."
  type        = string
}

variable "todoist_project_id" {
  description = "The one Todoist project tasks may be created in."
  type        = string
}

variable "llm_model" {
  description = "Model id in the Scaleway Generative APIs catalogue."
  type        = string
  default     = "qwen3-235b-a22b-instruct-2507"
}

variable "ledger_bucket" {
  description = "Object Storage bucket for the ledger (fr-par)."
  type        = string
}
