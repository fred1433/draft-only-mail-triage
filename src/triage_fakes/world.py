"""Wires the real job (real AllowlistTransport, real clients) to the fakes."""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from triage.auth import StaticToken
from triage.config import DEFAULT_LLM_BASE_URL
from triage.job import Job
from triage.ledger import FileLedger
from triage.llm import LLMClient
from triage.log import JsonlLog
from triage.providers.gmail import GmailProvider
from triage.providers.graph import GraphProvider
from triage.tasks import TodoistSink
from triage.transport import HttpClient, RecordingTransport, build_client, build_rules
from triage_fakes import FakeCloud, FakeGmail, FakeGraph, FakeModel, FakeTodoist, load_corpus

import os
ROOT = Path(os.environ.get("TRIAGE_ROOT") or Path(__file__).resolve().parents[2])
CORPUS = ROOT / "corpus"
PROJECT_ID = "6Jf8VQXxpwv56VQ7"          # the one configured Todoist project (synthetic)


@dataclass
class World:
    cloud: FakeCloud
    gmail: FakeGmail
    graph: FakeGraph
    todoist: FakeTodoist
    model: FakeModel
    recorder: RecordingTransport
    http: HttpClient
    llm_base_url: str
    provider_name: str
    logs: list[JsonlLog] = field(default_factory=list)

    def job(self, ledger, run_id: str | None = None, stream=None) -> Job:
        """A fresh 'container': new provider, new clients, the ledger passed in."""
        log = JsonlLog(stream or io.StringIO())
        self.logs.append(log)
        token = StaticToken()
        if self.provider_name == "gmail":
            provider = GmailProvider(self.http, FakeGmail.LABEL, token)
        else:
            provider = GraphProvider(self.http, FakeGraph.FOLDER, token)
        llm = LLMClient(self.http, self.llm_base_url, "qwen3:8b")
        sink = TodoistSink(self.http, PROJECT_ID, "fake-todoist-token")
        return Job(provider=provider, llm=llm, sink=sink, ledger=ledger, log=log, run_id=run_id)

    def log_lines(self) -> list[dict]:
        return [line for log in self.logs for line in log.lines]


def build_world(script: Callable[[list[dict]], str], *, provider: str = "gmail",
                corpus: Path | str = CORPUS, llm_base_url: str = DEFAULT_LLM_BASE_URL,
                passthrough: tuple[str, ...] = (), only: tuple[str, ...] = ()) -> World:
    gmail, graph, todoist = FakeGmail(), FakeGraph(), FakeTodoist()
    from urllib.parse import urlsplit
    model = FakeModel(urlsplit(llm_base_url).netloc, script)
    services = [gmail, graph, todoist] + ([] if model.host in passthrough else [model])
    cloud = FakeCloud(services, passthrough_hosts=passthrough)
    corpus = Path(corpus)
    if only:
        tmp_files = [corpus / n for n in only]
        for f in sorted(tmp_files):
            gmail.load_eml(f.read_bytes())
            graph.load_eml(f.read_bytes())
    else:
        load_corpus(corpus, gmail, graph)
    recorder = RecordingTransport(cloud)
    rules = build_rules(todoist_project_id=PROJECT_ID, llm_base_url=llm_base_url)
    http = build_client(rules, inner=recorder, timeout_s=600)
    return World(cloud, gmail, graph, todoist, model, recorder, http, llm_base_url, provider)


def file_ledger(path: Path | str) -> FileLedger:
    return FileLedger(path)
