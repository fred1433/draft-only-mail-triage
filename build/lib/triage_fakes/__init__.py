"""In-process fakes of Gmail, Microsoft Graph, Todoist, an OpenAI-compatible model and S3.

They answer on the real host names, follow the public request and response shapes of the
endpoints the job uses, and sit *behind* the real AllowlistTransport. They are faithful
in one uncomfortable way on purpose: if a request reaches them, they perform it. A
``drafts.send`` that got past the allowlist would really be "sent" here, so a test can
see it.

This package is test and demo infrastructure, outside the job package.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import Message
from email.policy import default as default_policy
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, unquote

import httpx

OWNER = "office@advisory.example.org"


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _json(status: int, data) -> httpx.Response:
    return httpx.Response(status, json=data)


def _subject_core(s: str) -> str:
    return re.sub(r"^\s*((re|aw|fwd?|wg|ر)\s*:\s*)+", "", s or "", flags=re.I).strip().lower()


def _stable_id(*parts: str, n: int = 16) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:n]


# ------------------------------------------------------------------ Gmail


@dataclass
class GMsg:
    id: str
    thread_id: str
    raw: bytes
    labels: list[str]
    internal_ms: int

    @property
    def parsed(self) -> Message:
        return message_from_bytes(self.raw, policy=default_policy)


class FakeGmail:
    host = "gmail.googleapis.com"
    LABEL = "Label_triage"

    def __init__(self):
        self.messages: dict[str, GMsg] = {}
        self.drafts: dict[str, str] = {}           # draft id -> message id
        self.sent: list[str] = []                  # message ids sent through the API
        self.deleted: list[str] = []
        self.requests: list[tuple[str, str]] = []

    # corpus
    def load_eml(self, raw: bytes, labels: list[str] | None = None) -> str:
        msg = message_from_bytes(raw, policy=default_policy)
        mid = msg.get("Message-ID", "")
        refs = (msg.get("References", "") + " " + msg.get("In-Reply-To", "")).split()
        thread = next((m.thread_id for m in self.messages.values()
                       if m.parsed.get("Message-ID", "") in refs and refs), None)
        gid = _stable_id("gmail", mid)
        outbound = parseaddr(msg.get("From", ""))[1] == OWNER
        when = int(parsedate_to_datetime(msg["Date"]).timestamp() * 1000) if msg.get("Date") else 0
        default = ["SENT"] if outbound else ["INBOX", "UNREAD", self.LABEL]
        self.messages[gid] = GMsg(gid, thread or gid, raw, labels or default, when)
        return gid

    # human actions in the Gmail UI
    def human_send_draft(self, draft_id: str) -> None:
        m = self.messages[self.drafts.pop(draft_id)]
        m.labels = ["SENT"]
        m.internal_ms = int(time.time() * 1000)

    def human_delete_draft(self, draft_id: str) -> None:
        self.deleted.append(self.drafts.pop(draft_id))
        del self.messages[self.deleted[-1]]

    def human_edit_draft(self, draft_id: str, new_text: str) -> None:
        m = self.messages[self.drafts[draft_id]]
        p = m.parsed
        p.clear_content()
        p.set_content(new_text)
        m.raw = p.as_bytes()

    def draft_messages(self) -> list[GMsg]:
        return [self.messages[i] for i in self.drafts.values()]

    # resource builders (public schema: users.messages, MessagePart, MessagePartBody)
    def _part(self, part: Message, pid: str) -> dict:
        out = {"partId": pid, "mimeType": part.get_content_type(), "filename": part.get_filename() or "",
               "headers": [{"name": k, "value": str(v)} for k, v in part.items()]}
        if part.is_multipart():
            out["body"] = {"size": 0}
            out["parts"] = [self._part(p, f"{pid}.{i}" if pid else str(i))
                            for i, p in enumerate(part.iter_parts())]
        else:
            data = part.get_payload(decode=True) or b""
            if part.get_filename():
                out["body"] = {"attachmentId": "ANGj" + _stable_id(pid, str(len(data)), n=24), "size": len(data)}
            else:
                out["body"] = {"size": len(data), "data": _b64u(data)}
        return out

    def _resource(self, m: GMsg, fmt: str, meta_headers: list[str]) -> dict:
        base = {"id": m.id, "threadId": m.thread_id, "labelIds": m.labels, "internalDate": str(m.internal_ms),
                "sizeEstimate": len(m.raw), "historyId": "1"}
        if fmt == "minimal":
            return base
        if fmt == "metadata":
            wanted = {h.lower() for h in meta_headers}
            headers = [{"name": k, "value": str(v)} for k, v in m.parsed.items() if not wanted or k.lower() in wanted]
            return {**base, "payload": {"mimeType": m.parsed.get_content_type(), "headers": headers}}
        return {**base, "payload": self._part(m.parsed, ""), "snippet": ""}

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        q = request.url.params
        self.requests.append((method, path))
        base = "/gmail/v1/users/me"
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return _json(401, {"error": {"code": 401, "status": "UNAUTHENTICATED"}})
        if method == "GET" and path == f"{base}/labels":
            return _json(200, {"labels": [{"id": self.LABEL, "name": "triage", "type": "user"},
                                          {"id": "INBOX", "name": "INBOX", "type": "system"}]})
        if method == "GET" and path == f"{base}/messages":
            label = q.get("labelIds")
            items = [m for m in self.messages.values() if not label or label in m.labels]
            items.sort(key=lambda m: -m.internal_ms)
            return _json(200, {"messages": [{"id": m.id, "threadId": m.thread_id} for m in items],
                               "resultSizeEstimate": len(items)})
        if m := re.fullmatch(rf"{base}/messages/([^/]+)", path):
            if method == "GET":
                msg = self.messages.get(m.group(1))
                if not msg:
                    return _json(404, {"error": {"code": 404}})
                return _json(200, self._resource(msg, q.get("format", "full"), q.get_list("metadataHeaders")))
            if method == "DELETE":
                self.deleted.append(m.group(1))
                self.messages.pop(m.group(1), None)
                return httpx.Response(204)
        if method == "GET" and (m := re.fullmatch(rf"{base}/threads/([^/]+)", path)):
            msgs = sorted((x for x in self.messages.values() if x.thread_id == m.group(1)), key=lambda x: x.internal_ms)
            if not msgs:
                return _json(404, {"error": {"code": 404}})
            return _json(200, {"id": m.group(1), "messages": [
                self._resource(x, q.get("format", "full"), q.get_list("metadataHeaders")) for x in msgs]})
        if method == "GET" and path == f"{base}/drafts":
            return _json(200, {"drafts": [{"id": d, "message": {"id": i, "threadId": self.messages[i].thread_id}}
                                          for d, i in self.drafts.items()]})
        if method == "POST" and path == f"{base}/drafts":
            return self._create_draft(json.loads(request.content))
        if method == "POST" and path in (f"{base}/drafts/send", f"{base}/messages/send"):
            body = json.loads(request.content or b"{}")
            if path.endswith("drafts/send"):
                mid = self.drafts.pop(body.get("id", ""), None)
            else:
                mid = self.load_eml(base64.urlsafe_b64decode(body["raw"] + "=="), ["SENT"])
            if mid:
                self.messages[mid].labels = ["SENT"]
                self.sent.append(mid)
            return _json(200, {"id": mid, "labelIds": ["SENT"]})
        if method == "POST" and (m := re.fullmatch(rf"{base}/messages/([^/]+)/(trash|modify)", path)):
            self.deleted.append(m.group(1))
            return _json(200, {"id": m.group(1)})
        return _json(404, {"error": {"code": 404, "message": "not found in fake"}})

    def _create_draft(self, body: dict) -> httpx.Response:
        raw = base64.urlsafe_b64decode(body["message"]["raw"] + "==")
        parsed = message_from_bytes(raw, policy=default_policy)
        thread = body["message"].get("threadId")
        # Public rule: threadId given, References/In-Reply-To set, and Subject matching.
        if thread:
            in_thread = [m for m in self.messages.values() if m.thread_id == thread]
            ids = {m.parsed.get("Message-ID", "") for m in in_thread}
            refs = set((parsed.get("References", "") + " " + parsed.get("In-Reply-To", "")).split())
            subject_ok = any(_subject_core(m.parsed.get("Subject", "")) == _subject_core(parsed.get("Subject", ""))
                             for m in in_thread)
            if not (ids & refs) or not subject_ok:
                thread = None
        gid = _stable_id("draft", str(len(self.messages)), str(time.time_ns()))
        self.messages[gid] = GMsg(gid, thread or gid, raw, ["DRAFT"], int(time.time() * 1000))
        did = "r" + _stable_id("d", gid, n=18)
        self.drafts[did] = gid
        return _json(200, {"id": did, "message": {"id": gid, "threadId": thread or gid, "labelIds": ["DRAFT"]}})


# ------------------------------------------------------------------ Microsoft Graph


class FakeGraph:
    host = "graph.microsoft.com"
    FOLDER = "AAMkAGTriageFolder="

    def __init__(self):
        self.messages: dict[str, dict] = {}
        self.sent: list[str] = []
        self.forwarded: list[str] = []
        self.deleted: list[str] = []
        self.patched: list[str] = []

    def load_eml(self, raw: bytes) -> str:
        p = message_from_bytes(raw, policy=default_policy)
        mid = p.get("Message-ID", "")
        refs = (p.get("References", "") + " " + p.get("In-Reply-To", "")).split()
        conv = next((m["conversationId"] for m in self.messages.values() if m["internetMessageId"] in refs), None)
        gid = "AAMkAG" + _stable_id("graph", mid, n=20) + "="
        name, addr = parseaddr(p.get("From", ""))
        outbound = addr == OWNER
        text = ""
        atts = []
        for part in p.walk():
            if part.get_filename():
                atts.append({"name": part.get_filename(), "size": len(part.get_payload(decode=True) or b""),
                             "contentType": part.get_content_type(), "contentBytes": "SHOULD-NEVER-BE-READ"})
            elif part.get_content_type() == "text/plain":
                text += part.get_content()
        when = parsedate_to_datetime(p["Date"]).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.messages[gid] = {
            "id": gid, "conversationId": conv or "AAQkAG" + _stable_id("conv", mid, n=20) + "=",
            "subject": p.get("Subject", ""), "from": {"emailAddress": {"name": name, "address": addr}},
            "replyTo": [], "body": {"contentType": "text", "content": text},
            "internetMessageId": mid, "receivedDateTime": when, "sentDateTime": when,
            "hasAttachments": bool(atts), "_attachments": atts, "isDraft": False,
            "parentFolderId": "sentitems" if outbound else self.FOLDER,
            "webLink": f"https://outlook.office365.com/owa/?ItemID={gid}&exvsurl=1&viewmodel=ReadMessageItem",
            "singleValueExtendedProperties": [],
            "toRecipients": [{"emailAddress": {"address": OWNER}}],
        }
        return gid

    def human_send_draft(self, draft_id: str) -> None:
        m = self.messages[draft_id]
        m.update(isDraft=False, parentFolderId="sentitems", sentDateTime=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    def human_delete_draft(self, draft_id: str) -> None:
        self.messages.pop(draft_id)

    def drafts(self) -> list[dict]:
        return [m for m in self.messages.values() if m["parentFolderId"] == "drafts"]

    def _public(self, m: dict, select: str | None, expand: str | None) -> dict:
        out = {k: v for k, v in m.items() if not k.startswith("_")}
        if select:
            keep = set(select.split(",")) | {"id"}
            out = {k: v for k, v in out.items() if k in keep}
        if expand and "singleValueExtendedProperties" in expand:
            out["singleValueExtendedProperties"] = m["singleValueExtendedProperties"]
        else:
            out.pop("singleValueExtendedProperties", None)
        return out

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method, q = unquote(request.url.path), request.method, request.url.params
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return _json(401, {"error": {"code": "InvalidAuthenticationToken"}})
        base = "/v1.0/me"
        if method == "GET" and (m := re.fullmatch(rf"{base}/mailFolders/([^/]+)/messages", path)):
            folder = {"drafts": "drafts", "sentitems": "sentitems"}.get(m.group(1).lower(), m.group(1))
            items = [x for x in self.messages.values() if x["parentFolderId"] == folder]
            if (f := q.get("$filter")) and (c := re.search(r"conversationId eq '([^']+)'", f)):
                items = [x for x in items if x["conversationId"] == c.group(1)]
            items.sort(key=lambda x: x["receivedDateTime"], reverse=True)
            return _json(200, {"value": [self._public(x, q.get("$select"), q.get("$expand")) for x in items]})
        if (m := re.fullmatch(rf"{base}/messages/([^/]+)", path)):
            msg = self.messages.get(m.group(1))
            if not msg:
                return _json(404, {"error": {"code": "ErrorItemNotFound"}})
            if method == "GET":
                return _json(200, self._public(msg, q.get("$select"), q.get("$expand")))
            if method == "DELETE":
                self.deleted.append(m.group(1))
                self.messages.pop(m.group(1))
                return httpx.Response(204)
            if method == "PATCH":
                self.patched.append(m.group(1))
                msg.update(json.loads(request.content))
                return _json(200, self._public(msg, None, None))
        if method == "GET" and (m := re.fullmatch(rf"{base}/messages/([^/]+)/attachments", path)):
            atts = self.messages[m.group(1)]["_attachments"]
            select = q.get("$select", "")
            return _json(200, {"value": [{k: v for k, v in a.items() if not select or k in select.split(",")} for a in atts]})
        if method == "POST" and (m := re.fullmatch(rf"{base}/messages/([^/]+)/(createReply|send|forward|reply|createForward)", path)):
            src = self.messages.get(m.group(1))
            if not src:
                return _json(404, {"error": {"code": "ErrorItemNotFound"}})
            action = m.group(2)
            if action == "send":
                self.human_send_draft(m.group(1))
                self.sent.append(m.group(1))
                return httpx.Response(202)
            if action in ("forward", "reply"):
                (self.forwarded if action == "forward" else self.sent).append(m.group(1))
                return httpx.Response(202)
            body = json.loads(request.content or b"{}")
            message = body.get("message", {})
            if body.get("comment") and message.get("body"):
                return _json(400, {"error": {"code": "ErrorInvalidRequest", "message": "comment and body both set"}})
            did = "AAMkAGdraft" + _stable_id("draft", m.group(1), str(time.time_ns()), n=16) + "="
            self.messages[did] = {
                **{k: v for k, v in src.items() if k in ("conversationId",)},
                "id": did, "subject": "RE: " + src["subject"], "isDraft": True, "parentFolderId": "drafts",
                "from": {"emailAddress": {"address": OWNER}}, "replyTo": [],
                "toRecipients": [src["from"]],
                "body": message.get("body") or {"contentType": "text", "content": body.get("comment", "")},
                "internetMessageId": f"<{did}@fake>", "receivedDateTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "sentDateTime": "", "hasAttachments": False, "_attachments": [], "webLink": "",
                "singleValueExtendedProperties": message.get("singleValueExtendedProperties", []),
            }
            return _json(201, self._public(self.messages[did], None, None))
        return _json(404, {"error": {"code": "NotFoundInFake"}})


# ------------------------------------------------------------------ Todoist


class FakeTodoist:
    host = "api.todoist.com"

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.executed_uuids: set[str] = set()
        self.deleted: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return _json(401, {"error": "unauthorized"})
        path, method = request.url.path, request.method
        if method == "POST" and path == "/api/v1/sync":
            form = dict(parse_qsl(request.content.decode()))
            commands = json.loads(form.get("commands", "[]"))
            status, mapping = {}, {}
            for cmd in commands:
                if cmd["uuid"] in self.executed_uuids:      # documented: same uuid is not executed twice
                    status[cmd["uuid"]] = "ok"
                    continue
                if cmd["type"] == "item_add":
                    tid = "6X" + _stable_id("task", cmd["uuid"], n=14)
                    a = cmd["args"]
                    self.tasks[tid] = {"id": tid, "content": a["content"], "description": a.get("description", ""),
                                       "project_id": str(a.get("project_id")), "labels": a.get("labels", [])}
                    if "temp_id" in cmd:
                        mapping[cmd["temp_id"]] = tid
                elif cmd["type"] == "item_delete":
                    self.deleted.append(cmd["args"]["id"])
                    self.tasks.pop(cmd["args"]["id"], None)
                self.executed_uuids.add(cmd["uuid"])
                status[cmd["uuid"]] = "ok"
            return _json(200, {"sync_status": status, "temp_id_mapping": mapping, "sync_token": "fake", "full_sync": False})
        if method == "POST" and path == "/api/v1/tasks":
            a = json.loads(request.content)
            tid = "6X" + _stable_id("rest", json.dumps(a), str(time.time_ns()), n=14)
            self.tasks[tid] = {"id": tid, **a}
            return _json(200, self.tasks[tid])
        if method == "DELETE" and (m := re.fullmatch(r"/api/v1/tasks/([^/]+)", path)):
            self.deleted.append(m.group(1))
            self.tasks.pop(m.group(1), None)
            return httpx.Response(204)
        return _json(404, {"error": "not found in fake"})


# ------------------------------------------------------------------ model


class FakeModel:
    """OpenAI-compatible /chat/completions. ``script`` returns the content string for a
    request, or raises httpx.ReadTimeout to simulate a timeout."""

    def __init__(self, host: str, script: Callable[[list[dict]], str]):
        self.host, self.script = host, script
        self.calls: list[list[dict]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body["messages"])
        content = self.script(body["messages"])
        return _json(200, {"id": "chatcmpl-fake", "object": "chat.completion", "model": body["model"],
                           "choices": [{"index": 0, "finish_reason": "stop",
                                        "message": {"role": "assistant", "content": content}}],
                           "usage": {"prompt_tokens": 0, "completion_tokens": 0}})


def prompt_hash(messages: list[dict]) -> str:
    return hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def recorded_script(examples_dir: str | Path) -> Callable[[list[dict]], str]:
    """Replays outputs recorded from the local Qwen (examples/), keyed by prompt hash."""
    book = {}
    for f in sorted(Path(examples_dir).glob("*.json")):
        rec = json.loads(f.read_text())
        for call in rec.get("calls", []):
            book[call["prompt_sha256"]] = call["output"]

    def script(messages):
        h = prompt_hash(messages)
        if h not in book:
            raise KeyError(f"no recorded output for prompt {h[:12]}")
        return book[h]
    return script


# ------------------------------------------------------------------ S3


class FakeS3:
    host = "s3.fr-par.scw.cloud"

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.down = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503)
        if not request.headers.get("authorization", "").startswith("AWS4-HMAC-SHA256 "):
            return httpx.Response(403)
        key, method = request.url.path, request.method
        etag = lambda b: '"' + hashlib.md5(b).hexdigest() + '"'
        if method in ("GET", "HEAD"):
            if key not in self.objects:
                return httpx.Response(404)
            data = self.objects[key]
            return httpx.Response(200, content=b"" if method == "HEAD" else data, headers={"ETag": etag(data)})
        if method == "PUT":
            exists = key in self.objects
            if request.headers.get("if-none-match") == "*" and exists:
                return httpx.Response(412)
            if (want := request.headers.get("if-match")) and (not exists or (want != "*" and want != etag(self.objects[key]))):
                return httpx.Response(412)
            self.objects[key] = request.content
            return httpx.Response(200, headers={"ETag": etag(request.content)})
        return httpx.Response(405)


# ------------------------------------------------------------------ the cloud


@dataclass
class Fault:
    method: str
    host: str
    path: str                 # regular expression
    kind: str                 # "lose_response" | "http_503" | "timeout"
    times: int = 1


class FakeCloud(httpx.BaseTransport):
    """Routes by host name. Unknown hosts fail like an unreachable network."""

    def __init__(self, services: list, passthrough_hosts: tuple[str, ...] = ()):
        self.services = {s.host: s for s in services}
        self.faults: list[Fault] = []
        self._lock = threading.RLock()
        self._real = httpx.HTTPTransport() if passthrough_hosts else None
        self.passthrough = set(passthrough_hosts)

    def inject(self, fault: Fault) -> None:
        self.faults.append(fault)

    def _fault_for(self, request: httpx.Request) -> Fault | None:
        host = request.url.host if request.url.port in (None, 443, 80) else f"{request.url.host}:{request.url.port}"
        for f in self.faults:
            if f.times > 0 and f.method == request.method and f.host == host and re.fullmatch(f.path, request.url.path):
                f.times -= 1
                return f
        return None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host if request.url.port in (None, 443, 80) else f"{request.url.host}:{request.url.port}"
        if host in self.passthrough:
            return self._real.handle_request(request)
        service = self.services.get(host)
        if service is None:
            raise httpx.ConnectError(f"fake network: no route to {host}")
        request.read()
        with self._lock:
            fault = self._fault_for(request)
            if fault and fault.kind == "http_503":
                return httpx.Response(503, json={"error": "injected"})
            if fault and fault.kind == "timeout":
                raise httpx.ReadTimeout("injected timeout")
            response = service.handle(request)
            if fault and fault.kind == "lose_response":
                raise httpx.ReadTimeout("injected: response lost after the write")
            return response


def load_corpus(corpus_dir: str | Path, gmail: FakeGmail | None = None, graph: FakeGraph | None = None) -> list[str]:
    files = sorted(Path(corpus_dir).glob("*.eml"))
    names = []
    for f in files:
        raw = f.read_bytes()
        if gmail is not None:
            gmail.load_eml(raw)
        if graph is not None:
            graph.load_eml(raw)
        names.append(f.name)
    return names
