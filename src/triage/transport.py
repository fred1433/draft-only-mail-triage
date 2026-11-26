"""The only module of the job that talks to the network.

Every request the job makes goes through one ``httpx`` client whose transport is an
``AllowlistTransport``. A request that is not on the allowlist (host, method, path
pattern, and for some routes a check of the query or the body) raises
``PolicyViolation`` *before* a single byte is handed to the network layer.

The allowlist protects against a mistake or a drift in this program. It does not
protect against a stolen token or a compromised process: see SECURITY.md.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qsl, urlsplit

import httpx

__all__ = [
    "PolicyViolation", "Rule", "AllowlistTransport", "RecordingTransport",
    "HttpClient", "Reply", "TransportFailure", "WriteOutcomeUnknown",
    "build_rules", "build_client",
]

ID = r"[A-Za-z0-9_\-=]+"          # Gmail / Graph / Todoist identifiers
GRAPH_ID = r"[A-Za-z0-9_\-=%.]+"   # Graph ids are base64 and may arrive url-encoded


class PolicyViolation(Exception):
    """A request outside the allowlist. Never caught by the job: the run stops."""

    def __init__(self, method: str, host: str, path: str, reason: str):
        self.method, self.host, self.path, self.reason = method, host, path, reason
        super().__init__(f"blocked before send: {method} {host}{path} ({reason})")


class TransportFailure(Exception):
    """The request could not be sent (connection refused, DNS). Nothing was written."""


class WriteOutcomeUnknown(Exception):
    """A write was sent but no response came back. The effect may or may not exist."""


@dataclass(frozen=True)
class Rule:
    name: str
    scheme: str
    host: str                      # includes ":port" when the port is not the default
    method: str
    path: str                      # full-match regular expression
    query_check: Callable[[dict[str, str]], str | None] | None = None
    body_check: Callable[[bytes], str | None] | None = None

    def matches_route(self, scheme: str, host: str, method: str, path: str) -> bool:
        return (scheme == self.scheme and host == self.host and method == self.method
                and re.fullmatch(self.path, path) is not None)


def _netloc(url: httpx.URL) -> str:
    default = {"https": 443, "http": 80}.get(url.scheme)
    return url.host if url.port in (None, default) else f"{url.host}:{url.port}"


class AllowlistTransport(httpx.BaseTransport):
    """Refuses, before emission, any request that no rule allows."""

    def __init__(self, rules: list[Rule], inner: httpx.BaseTransport):
        self.rules = list(rules)
        self._inner = inner

    def check(self, request: httpx.Request) -> Rule:
        url = request.url
        scheme, host, method, path = url.scheme, _netloc(url), request.method, url.path
        route = [r for r in self.rules if r.matches_route(scheme, host, method, path)]
        if not route:
            known_host = any(r.host == host for r in self.rules)
            reason = "route not on the allowlist" if known_host else "host not on the allowlist"
            raise PolicyViolation(method, host, path, reason)
        query = dict(url.params.multi_items())
        body = request.read() if method in ("POST", "PUT", "PATCH") else b""
        refusals = []
        for rule in route:
            why = None
            if rule.query_check is not None:
                why = rule.query_check(query)
            if why is None and rule.body_check is not None:
                why = rule.body_check(body)
            if why is None:
                return rule
            refusals.append(f"{rule.name}: {why}")
        raise PolicyViolation(method, host, path, "; ".join(refusals))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.check(request)
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


@dataclass
class Emitted:
    method: str
    host: str
    path: str
    bytes_sent: int
    status: int | None = None


class RecordingTransport(httpx.BaseTransport):
    """Sits *behind* the allowlist and records what actually reached the network layer."""

    def __init__(self, inner: httpx.BaseTransport):
        self._inner = inner
        self.emitted: list[Emitted] = []
        self._lock = threading.Lock()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        entry = Emitted(request.method, _netloc(request.url), request.url.path, len(body))
        with self._lock:
            self.emitted.append(entry)
        response = self._inner.handle_request(request)
        entry.status = response.status_code
        return response

    @property
    def bytes_sent(self) -> int:
        return sum(e.bytes_sent for e in self.emitted)


# ---------------------------------------------------------------- rules


def _json(body: bytes) -> dict | None:
    try:
        data = json.loads(body or b"null")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _gmail_draft_body(body: bytes) -> str | None:
    data = _json(body)
    if data is None or set(data) != {"message"} or not isinstance(data["message"], dict):
        return "draft body must be {'message': {...}}"
    if not set(data["message"]) <= {"raw", "threadId"} or "raw" not in data["message"]:
        return "draft message may only carry raw and threadId"
    return None


def _graph_reply_body(body: bytes) -> str | None:
    data = _json(body) if body else {}
    if data is None or not set(data) <= {"message", "comment"}:
        return "createReply body may only carry message and comment"
    message = data.get("message", {})
    if not isinstance(message, dict) or not set(message) <= {"body", "singleValueExtendedProperties"}:
        return "createReply message may only set body and the triage marker (no recipients)"
    return None


def _graph_attachment_query(query: dict[str, str]) -> str | None:
    select = query.get("$select", "")
    if not select or "contentBytes" in select:
        return "attachments may be listed by name, size and type only"
    return None


def _todoist_sync_body(project_id: str) -> Callable[[bytes], str | None]:
    """One item_add command, in the configured project, with a few known fields."""
    allowed_args = {"content", "description", "project_id", "labels"}

    def check(body: bytes) -> str | None:
        try:
            form = dict(parse_qsl(body.decode("utf-8"), strict_parsing=True))
            commands = json.loads(form.get("commands", "null"))
        except (ValueError, UnicodeDecodeError):
            return "sync body must be a form with a JSON commands field"
        if set(form) != {"commands"} or not isinstance(commands, list) or len(commands) != 1:
            return "exactly one command per request"
        cmd = commands[0]
        if not isinstance(cmd, dict) or cmd.get("type") != "item_add":
            return "only item_add is allowed"
        if not set(cmd) <= {"type", "uuid", "temp_id", "args"} or not cmd.get("uuid"):
            return "command must carry a uuid and nothing else"
        args = cmd.get("args")
        if not isinstance(args, dict) or not set(args) <= allowed_args:
            return f"task fields outside {sorted(allowed_args)}"
        if str(args.get("project_id")) != project_id:
            return "task outside the configured project"
        return None
    return check


def _gmail_list_query(label: str) -> Callable[[dict[str, str]], str | None]:
    """messages.list only on the configured label, nothing else in the query."""
    def check(query: dict[str, str]) -> str | None:
        if query.get("labelIds") != label:
            return "messages may be listed in the configured label only"
        if not set(query) <= {"labelIds", "maxResults", "pageToken", "q"}:
            return "unexpected list parameter"
        return None
    return check


def _graph_folders(folder: str) -> str:
    """The configured folder, as sent (url-encoded) or decoded, plus Drafts and Sent Items."""
    from urllib.parse import quote
    names = {re.escape(folder), re.escape(quote(folder, safe="")), "drafts", "sentitems"}
    return "(?:" + "|".join(sorted(names)) + ")"


def build_rules(*, todoist_project_id: str, llm_base_url: str,
                gmail_label: str | None = None, graph_folder: str | None = None,
                ledger_s3: tuple[str, str] | None = None,
                ms_tenant: str | None = None) -> list[Rule]:
    """The complete allowlist. Anything not produced here is refused. Mail routes exist
    only for the provider whose label or folder is configured, and listing is pinned to
    that label or folder."""
    g = "/gmail/v1/users/me"
    gm = "gmail.googleapis.com"
    rules: list[Rule] = []
    if gmail_label:
        rules += _gmail_rules(g, gm, gmail_label)
    ms = "graph.microsoft.com"
    m = "/v1.0/me"
    if graph_folder:
        folders = _graph_folders(graph_folder)
        rules += [
            Rule("graph.folder.messages", "https", ms, "GET", rf"{m}/mailFolders/{folders}/messages"),
            Rule("graph.messages.get", "https", ms, "GET", rf"{m}/messages/{GRAPH_ID}"),
            Rule("graph.attachments.list", "https", ms, "GET", rf"{m}/messages/{GRAPH_ID}/attachments",
                 query_check=_graph_attachment_query),
            Rule("graph.messages.createReply", "https", ms, "POST", rf"{m}/messages/{GRAPH_ID}/createReply",
                 body_check=_graph_reply_body),
        ]
    if ms_tenant:
        rules.append(Rule("microsoft.oauth.refresh", "https", "login.microsoftonline.com", "POST",
                          rf"/{re.escape(ms_tenant)}/oauth2/v2\.0/token"))
    return rules + _common_rules(todoist_project_id, llm_base_url, ledger_s3)


def _gmail_rules(g: str, gm: str, label: str) -> list[Rule]:
    return [
        Rule("gmail.messages.list", "https", gm, "GET", rf"{g}/messages", query_check=_gmail_list_query(label)),
        Rule("gmail.messages.get", "https", gm, "GET", rf"{g}/messages/{ID}"),
        Rule("gmail.threads.get", "https", gm, "GET", rf"{g}/threads/{ID}"),
        Rule("gmail.drafts.create", "https", gm, "POST", rf"{g}/drafts", body_check=_gmail_draft_body),
        Rule("google.oauth.refresh", "https", "oauth2.googleapis.com", "POST", r"/token"),
    ]


def _common_rules(todoist_project_id: str, llm_base_url: str,
                  ledger_s3: tuple[str, str] | None) -> list[Rule]:
    rules = [Rule("todoist.sync.item_add", "https", "api.todoist.com", "POST", r"/api/v1/sync",
                  body_check=_todoist_sync_body(str(todoist_project_id)))]
    llm = urlsplit(llm_base_url)
    rules.append(Rule("llm.chat.completions", llm.scheme, llm.netloc, "POST",
                      re.escape(llm.path.rstrip("/")) + r"/chat/completions"))
    if ledger_s3:
        bucket, prefix = ledger_s3
        base = rf"/{re.escape(bucket)}/{re.escape(prefix.strip('/'))}"
        for verb in ("GET", "PUT", "HEAD"):
            rules.append(Rule(f"ledger.s3.{verb.lower()}", "https", "s3.fr-par.scw.cloud", verb,
                              base + r"/[A-Za-z0-9_\-./]+"))
    return rules


# ---------------------------------------------------------------- client


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes = field(repr=False)

    def json(self):
        return json.loads(self.body or b"null")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class HttpClient:
    """Thin wrapper so the rest of the job never imports httpx."""

    def __init__(self, client: httpx.Client, allowlist: AllowlistTransport):
        self._client = client
        self.allowlist = allowlist

    def request(self, method: str, url: str, *, params=None, json_body=None,
                content: bytes | None = None, form=None, headers=None) -> Reply:
        try:
            r = self._client.request(method, url, params=params, json=json_body, data=form,
                                     content=content, headers=headers)
        except httpx.ConnectError as exc:
            raise TransportFailure(type(exc).__name__) from None
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError,
                httpx.ReadError) as exc:
            if method == "GET" or method == "HEAD":
                raise TransportFailure(type(exc).__name__) from None
            raise WriteOutcomeUnknown(type(exc).__name__) from None
        except httpx.TimeoutException as exc:
            raise TransportFailure(type(exc).__name__) from None
        return Reply(r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.content)

    def close(self) -> None:
        self._client.close()


def build_client(rules: list[Rule], inner: httpx.BaseTransport | None = None,
                 timeout_s: float = 60.0) -> HttpClient:
    """Production: inner is a plain HTTPS transport. Tests and the demo pass fakes."""
    allowlist = AllowlistTransport(rules, inner or httpx.HTTPTransport(retries=0))
    client = httpx.Client(transport=allowlist, timeout=timeout_s, follow_redirects=False)
    return HttpClient(client, allowlist)
