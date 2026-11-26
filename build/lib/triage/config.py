"""Configuration. Every identifier the job acts on (mailbox, label or folder, project,
endpoint, model) comes from here, never from the model."""
from __future__ import annotations

import os
from dataclasses import dataclass

# Scopes requested at consent time. Kept here so a test can refuse a wider one.
GMAIL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
)
GRAPH_SCOPES = ("Mail.ReadWrite", "offline_access")      # delegated, one mailbox, no Mail.Send
TODOIST_SCOPES = ("task:add",)

FORBIDDEN_SCOPES = (
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "Mail.Send", "Mail.Send.Shared", "Mail.ReadWrite.Shared",
    "data:read_write", "data:delete", "project:delete",
)

DEFAULT_LLM_BASE_URL = "https://api.scaleway.ai/v1"
DEFAULT_LLM_MODEL = "qwen3-235b-a22b-instruct-2507"
REGION = "fr-par"


class ConfigError(Exception):
    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__("missing configuration: " + ", ".join(missing))


@dataclass(frozen=True)
class Settings:
    provider: str                 # "gmail" or "graph"
    mailbox: str                  # Gmail label id or Graph folder id
    todoist_project_id: str
    todoist_token: str
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    ledger_backend: str           # "file" or "s3"
    ledger_path: str
    ledger_bucket: str
    ledger_prefix: str
    s3_access_key: str
    s3_secret_key: str
    google_client_id: str
    google_client_secret: str
    google_refresh_token: str
    ms_tenant: str
    ms_client_id: str
    ms_refresh_token: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = dict(os.environ if env is None else env)
        provider = e.get("MAIL_PROVIDER", "")
        need = ["MAIL_PROVIDER", "TODOIST_PROJECT_ID", "TODOIST_TOKEN", "LLM_API_KEY"]
        if provider == "gmail":
            need += ["GMAIL_LABEL_ID", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"]
        elif provider == "graph":
            need += ["GRAPH_FOLDER_ID", "MS_TENANT_ID", "MS_CLIENT_ID", "MS_REFRESH_TOKEN"]
        backend = e.get("LEDGER_BACKEND", "file")
        need += ["LEDGER_PATH"] if backend == "file" else ["LEDGER_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY"]
        missing = [k for k in need if not e.get(k)]
        if provider and provider not in ("gmail", "graph"):
            missing.append("MAIL_PROVIDER (gmail or graph)")
        if backend not in ("file", "s3"):
            missing.append("LEDGER_BACKEND (file or s3)")
        if missing:
            raise ConfigError(missing)
        return cls(
            provider=provider,
            mailbox=e.get("GMAIL_LABEL_ID", "") if provider == "gmail" else e.get("GRAPH_FOLDER_ID", ""),
            todoist_project_id=e["TODOIST_PROJECT_ID"], todoist_token=e["TODOIST_TOKEN"],
            llm_base_url=e.get("LLM_BASE_URL", DEFAULT_LLM_BASE_URL),
            llm_model=e.get("LLM_MODEL", DEFAULT_LLM_MODEL), llm_api_key=e["LLM_API_KEY"],
            ledger_backend=backend, ledger_path=e.get("LEDGER_PATH", ""),
            ledger_bucket=e.get("LEDGER_BUCKET", ""), ledger_prefix=e.get("LEDGER_PREFIX", "ledger"),
            s3_access_key=e.get("S3_ACCESS_KEY", ""), s3_secret_key=e.get("S3_SECRET_KEY", ""),
            google_client_id=e.get("GOOGLE_CLIENT_ID", ""),
            google_client_secret=e.get("GOOGLE_CLIENT_SECRET", ""),
            google_refresh_token=e.get("GOOGLE_REFRESH_TOKEN", ""),
            ms_tenant=e.get("MS_TENANT_ID", ""), ms_client_id=e.get("MS_CLIENT_ID", ""),
            ms_refresh_token=e.get("MS_REFRESH_TOKEN", ""),
        )
