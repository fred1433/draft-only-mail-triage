"""Access tokens from refresh tokens held in Scaleway Secret Manager (injected as
environment variables by the job definition). Never logged."""
from __future__ import annotations

import time

from triage.config import GMAIL_SCOPES, GRAPH_SCOPES
from triage.transport import HttpClient


class AuthFailed(Exception):
    pass


class _Refreshing:
    url = ""

    def __init__(self, http: HttpClient):
        self.http = http
        self._token, self._until = "", 0.0

    def _form(self) -> dict:
        raise NotImplementedError

    def __call__(self) -> str:
        if self._token and time.time() < self._until - 60:
            return self._token
        r = self.http.request("POST", self.url, form=self._form())
        if not r.ok:
            raise AuthFailed(f"token refresh {r.status}")   # revoked or expired: see RUNBOOK
        data = r.json()
        self._token, self._until = data["access_token"], time.time() + int(data.get("expires_in", 600))
        return self._token


class GoogleToken(_Refreshing):
    url = "https://oauth2.googleapis.com/token"

    def __init__(self, http, client_id, client_secret, refresh_token):
        super().__init__(http)
        self._f = {"grant_type": "refresh_token", "client_id": client_id,
                   "client_secret": client_secret, "refresh_token": refresh_token}

    def _form(self):
        return self._f


class MicrosoftToken(_Refreshing):
    def __init__(self, http, tenant, client_id, refresh_token):
        super().__init__(http)
        self.url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        self._f = {"grant_type": "refresh_token", "client_id": client_id,
                   "refresh_token": refresh_token, "scope": " ".join(GRAPH_SCOPES)}

    def _form(self):
        return self._f


class StaticToken:
    """Demo and tests only: the fakes accept any bearer."""

    def __init__(self, value: str = "fake-token"):
        self.value = value

    def __call__(self) -> str:
        return self.value


__all__ = ["GoogleToken", "MicrosoftToken", "StaticToken", "AuthFailed", "GMAIL_SCOPES"]
