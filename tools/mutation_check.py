"""Mutation check: does the suite bite?

Copies the repository to a temporary directory, applies one dangerous change at a time to
the job's code, runs the R1 tests on the mutant, and reports whether the suite went red
and whether the mutant's call reached the network layer.

    python tools/mutation_check.py          # human-readable
    python tools/mutation_check.py --json   # machine-readable (used by the R1 test)

Exit code 0 when every mutant is caught, 1 otherwise."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MUTATIONS = [
    {
        "mutation": "gmail: send the draft right after creating it (drafts.send)",
        "file": "src/triage/providers/gmail.py",
        "find": '        return r.json()["id"]\n',
        "replace": '        self.http.request("POST", API + "/drafts/send", json_body={"id": r.json()["id"]}, '
                   'headers={"Authorization": f"Bearer {self._token()}"})\n        return r.json()["id"]\n',
    },
    {
        "mutation": "gmail: send the reply directly (messages.send)",
        "file": "src/triage/providers/gmail.py",
        "find": '        r = self.http.request(\n            "POST", API + "/drafts",',
        "replace": '        self.http.request("POST", API + "/messages/send", json_body={"raw": base64.urlsafe_b64encode(raw).decode()}, '
                   'headers={"Authorization": f"Bearer {self._token()}"})\n        r = self.http.request(\n            "POST", API + "/drafts",',
    },
    {
        "mutation": "graph: delete the original after drafting (DELETE /messages/{id})",
        "file": "src/triage/providers/graph.py",
        "find": '        return r.json()["id"]\n',
        "replace": '        self.http.request("DELETE", f"{API}/messages/{quote(msg.id, safe=\'\')}", '
                   'headers={"Authorization": f"Bearer {self._token()}"})\n        return r.json()["id"]\n',
    },
    {
        "mutation": "llm: point the client at api.openai.com",
        "file": "src/triage/llm.py",
        "find": 'f"{self.base_url}/chat/completions"',
        "replace": '"https://api.openai.com/v1/chat/completions"',
    },
]


def run_one(m: dict, workdir: Path) -> dict:
    target = workdir / m["file"]
    source = target.read_text()
    assert m["find"] == "" or source.count(m["find"]) >= 1, f"anchor not found for: {m['mutation']}"
    target.write_text(source.replace(m["find"], m["replace"], 1))
    env = {**os.environ, "PYTHONPATH": str(workdir / "src") + os.pathsep + str(workdir),
           "TRIAGE_INSIDE_MUTATION": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_r1_draft_only.py",
         "-k", "corpus_replay or policy_violation", "-o", "addopts="],
        cwd=workdir, env=env, capture_output=True, text=True, timeout=300)
    target.write_text(source)
    tail = proc.stdout.strip().splitlines()
    summary = tail[-1] if tail else ""
    failed = int(m_.group(1)) if (m_ := re.search(r"(\d+) failed", summary)) else 0
    if "error" in summary:
        failed = 0          # collection errors are not a bite: the mutant must fail a test
    blocked = sorted(set(re.findall(r"blocked before send: ([A-Z]+ [^\s(]+)", proc.stdout)))
    return {
        "mutation": m["mutation"], "suite": "red" if proc.returncode != 0 and failed else "green",
        "failed": failed, "summary": summary, "blocked": blocked,
        # The mutant's request never reaches the recording transport: PolicyViolation is
        # raised by the allowlist first. The corpus replay test asserts it via the recorder.
        "bytes_emitted_by_mutant_call": 0 if blocked else None,
    }


def main() -> int:
    as_json = "--json" in sys.argv
    with tempfile.TemporaryDirectory(prefix="triage-mutants-") as tmp:
        work = Path(tmp)
        for name in ("src", "tests", "corpus", "examples", "evidence"):
            if (ROOT / name).exists():
                shutil.copytree(ROOT / name, work / name, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
        for name in ("pyproject.toml", "conftest.py"):
            shutil.copy(ROOT / name, work / name)
        baseline = run_one({"mutation": "baseline (no mutation)", "file": "pyproject.toml", "find": "", "replace": ""}, work)
        if baseline["suite"] != "green" or "passed" not in baseline["summary"]:
            print(f"baseline is not green in the copy: {baseline['summary']}", file=sys.stderr)
            return 1
        report = [run_one(m, work) for m in MUTATIONS]
    if as_json:
        print(json.dumps(report, indent=1))
    else:
        for r in report:
            print(f"[{r['suite']}] {r['mutation']}: {r['summary']}; blocked: {', '.join(r['blocked']) or 'nothing'}")
    return 0 if all(r["suite"] == "red" and r["blocked"] for r in report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
