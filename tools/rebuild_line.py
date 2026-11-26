"""Writes one evidence line for a monthly rebuild (rebuild-evidence.json) and prints it
as Markdown for the job summary. Reads step outcomes from the environment."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path


def outcome(name: str) -> str:
    v = os.environ.get(name, "")
    return {"success": "passed", "failure": "failed", "skipped": "skipped", "cancelled": "cancelled"}.get(v, v or "not run")


def gated(name: str) -> str:
    result = outcome(name)
    return "skipped (no credentials)" if result in ("skipped", "not run") else result


def main() -> None:
    sbom = Path("sbom.cdx.json")
    components = len(json.loads(sbom.read_text()).get("components", [])) if sbom.exists() else 0
    lines = Path("pytest.txt").read_text().strip().splitlines() if Path("pytest.txt").exists() else []
    pytest_tail = lines[-1] if lines else ""
    audit = json.loads(Path("pip-audit.json").read_text()) if Path("pip-audit.json").exists() else {}
    vulns = sum(len(d.get("vulns", [])) for d in audit.get("dependencies", []))
    corpus = Path("corpus-run.txt").read_text() if Path("corpus-run.txt").exists() else ""
    corpus_summary = next((l for l in corpus.splitlines() if l.startswith("messages=")), "")
    corpus_summary = re.sub(r" ledger=\S+", "", corpus_summary)
    line = {
        "at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": os.environ.get("GITHUB_SHA", ""),
        "run": f"{os.environ.get('GITHUB_SERVER_URL', '')}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}",
        "platform": "linux/amd64",
        "tests": outcome("TESTS"), "pytest": pytest_tail,
        "pip_audit": outcome("AUDIT"), "known_vulnerabilities": vulns,
        "build_no_cache": outcome("BUILD"), "local_image_id": os.environ.get("IMAGE_ID", ""),
        "corpus_run": outcome("CORPUS"), "corpus_summary": corpus_summary,
        "sbom": outcome("SBOM"), "sbom_components": components,
        "sbom_sha256": hashlib.sha256(sbom.read_bytes()).hexdigest() if sbom.exists() else "",
        "model_catalogue_check": gated("CATALOGUE"),
        "push_and_job_update": gated("PUSH"),
        "pushed_digest": os.environ.get("DIGEST", ""),
    }
    Path("rebuild-evidence.json").write_text(json.dumps(line, indent=1))
    print("### Rebuild evidence\n")
    print("| step | result |\n|---|---|")
    for k, v in line.items():
        print(f"| {k} | {v} |")


if __name__ == "__main__":
    main()
