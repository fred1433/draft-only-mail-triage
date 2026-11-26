"""Evidence plugin: links tests to register rows and writes evidence.json.

    @pytest.mark.evidence("R1", control="triage.transport:AllowlistTransport.check",
                          scope="local")
    def test_x(proof): proof.line("raw output the reader can check")

Run with --evidence evidence/evidence.json to write the file. The page's register is
built from that file only."""
from __future__ import annotations

import datetime as dt
import importlib
import inspect
import json
import os
import platform
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = json.loads((ROOT / "evidence" / "requirements.json").read_text())
SCOPES = {
    "local": "local, mocked connectors",
    "config": "configuration validated locally",
}
_RESULTS: dict[str, dict] = {}


def pytest_addoption(parser):
    parser.addoption("--evidence", default=None, help="write the evidence register to this JSON file")


def resolve_control(ref: str) -> str:
    """'pkg.module:Qualified.name' -> 'src/pkg/module.py:LINE'."""
    file_part, _, pattern = ref.partition("#")
    if (ROOT / file_part).is_file():
        if not pattern:
            return file_part
        for n, line in enumerate((ROOT / file_part).read_text().splitlines(), 1):
            if pattern in line:
                return f"{file_part}:{n}"
        raise LookupError(f"{pattern!r} not found in {file_part}")
    module_name, _, qual = ref.partition(":")
    obj = importlib.import_module(module_name)
    for part in qual.split(".") if qual else []:
        obj = getattr(obj, part)
    path = Path(inspect.getsourcefile(obj)).resolve()
    line = inspect.getsourcelines(obj)[1] if qual else 1
    try:
        rel = path.relative_to(ROOT)
    except ValueError:
        rel = path
    return f"{rel}:{line}"


class Proof:
    def __init__(self):
        self.lines: list[str] = []

    def line(self, text: str) -> None:
        self.lines.append(str(text))


@pytest.fixture
def proof(request):
    p = Proof()
    request.node._proof = p
    return p


def pytest_runtest_makereport(item, call):
    marker = item.get_closest_marker("evidence")
    if marker is None:
        return
    if call.when == "setup" and call.excinfo is None:
        return
    if call.when == "teardown" and item.nodeid in _RESULTS:
        return
    if call.excinfo is None:
        outcome = "passed"
    elif call.excinfo.errisinstance(pytest.skip.Exception):
        outcome = "not executed"
    else:
        outcome = "failed"
    proof = getattr(item, "_proof", None)
    lines = list(proof.lines) if proof else []
    if outcome == "not executed":
        lines.append("skipped: " + str(call.excinfo.value))
    elif outcome == "failed":
        lines.append("failure: " + str(call.excinfo.value).splitlines()[0][:300] if str(call.excinfo.value) else "failure")
    doc = (inspect.getdoc(item.function) or item.name).strip().splitlines()[0]
    control = marker.kwargs.get("control")
    _RESULTS[item.nodeid] = {
        "req": marker.args[0],
        "test": item.nodeid,
        "scenario": doc,
        "control": resolve_control(control) if control else None,
        "scope": SCOPES[marker.kwargs.get("scope", "local")] if outcome != "not executed" else "not executed",
        "outcome": outcome,
        "duration_ms": int(call.duration * 1000),
        "command": f"python -m pytest '{item.nodeid}'",
        "output": lines[:40],
    }


def _git(*args) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def pytest_sessionfinish(session, exitstatus):
    out = session.config.getoption("--evidence")
    if not out:
        return
    commit = os.environ.get("GITHUB_SHA") or _git("rev-parse", "HEAD")
    dirty = bool(_git("status", "--porcelain", "--", "src", "tests", "terraform", ".github", "Dockerfile"))
    run_url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    rows = []
    for rid, req in REQUIREMENTS.items():
        tests = [r for r in _RESULTS.values() if r["req"] == rid]
        outcomes = {t["outcome"] for t in tests}
        if not tests:
            result = "no linked test"
        elif "failed" in outcomes:
            result = "failed"
        elif outcomes == {"not executed"}:
            result = "not executed"
        else:
            result = "passed"
        rows.append({"id": rid, **req, "result": result,
                     "passed": sum(t["outcome"] == "passed" for t in tests),
                     "failed": sum(t["outcome"] == "failed" for t in tests),
                     "not_executed": sum(t["outcome"] == "not executed" for t in tests),
                     "scopes": sorted({t["scope"] for t in tests}),
                     "controls": sorted({t["control"] for t in tests
                                         if t["control"] and not t["control"].startswith("src/triage_fakes/")}),
                     "tests": tests})
    doc = {
        "generated_by": "pytest --evidence",
        "recorded_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": commit, "worktree_dirty": dirty,
        "runner": "github-actions" if run_url else "local",
        "run_url": run_url,
        "python": platform.python_version(), "platform": platform.machine(),
        "exit_status": int(exitstatus),
        "rows": rows,
    }
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(doc, indent=1, ensure_ascii=False))
