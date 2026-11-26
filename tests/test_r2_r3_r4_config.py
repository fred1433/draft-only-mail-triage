"""R2 one region, R3 least privilege, R4 monthly rebuild: configuration checks."""
from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from triage import config
from triage.transport import PolicyViolation, build_client, build_rules
from triage_fakes import FakeCloud, FakeTodoist
from triage_fakes.world import PROJECT_ID, ROOT

TF = ROOT / "terraform"
WF = ROOT / ".github" / "workflows" / "monthly-rebuild.yml"
OTHER_REGIONS = ("nl-ams", "pl-waw", "it-mil", "fr-par-2", "fr-par-3", "nl-ams-1", "pl-waw-1")


def tf_text() -> str:
    return "\n".join(p.read_text() for p in sorted(TF.glob("*.tf")))


# ---------------------------------------------------------------- R2


@pytest.mark.evidence("R2", control="terraform/main.tf", scope="config")
def test_every_region_and_zone_is_fr_par(proof):
    """Terraform: every region and zone is fr-par; no Amsterdam, Warsaw or Milan fallback anywhere."""
    text = tf_text()
    regions = re.findall(r'\bregion\s*=\s*"([^"]+)"', text)
    zones = re.findall(r'\bzone\s*=\s*"([^"]+)"', text)
    assert regions and set(regions) == {"fr-par"}
    assert set(zones) <= {"fr-par-1"}
    everything = text + WF.read_text() + (ROOT / "src" / "triage" / "ledger.py").read_text()
    assert not any(r in everything for r in OTHER_REGIONS)
    proof.line(f"region values: {sorted(set(regions))} ({len(regions)} declarations); zones: {sorted(set(zones))}")
    proof.line("other Scaleway regions mentioned: none")


@pytest.mark.evidence("R2", control="triage.config", scope="config")
def test_registry_ledger_and_model_endpoints_are_paris(proof):
    """Image registry rg.fr-par.scw.cloud, ledger s3.fr-par.scw.cloud, model api.scaleway.ai (serverless, hosted in Paris today)."""
    text = tf_text()
    assert "rg.fr-par.scw.cloud" in text and "rg.fr-par.scw.cloud" in WF.read_text()
    from triage.ledger import S3Ledger
    assert S3Ledger.ENDPOINT == "https://s3.fr-par.scw.cloud" and S3Ledger.REGION == "fr-par"
    assert config.DEFAULT_LLM_BASE_URL == "https://api.scaleway.ai/v1"
    proof.line("registry rg.fr-par.scw.cloud; ledger s3.fr-par.scw.cloud; LLM_BASE_URL default https://api.scaleway.ai/v1")


@pytest.mark.evidence("R2", control="terraform/main.tf", scope="config")
def test_terraform_fmt_and_validate(proof, tmp_path):
    """terraform fmt -check and terraform validate (init without backend): configuration validated locally, nothing deployed."""
    tf = shutil.which("terraform")
    if not tf:
        pytest.skip("terraform is not installed on this runner")
    work = tmp_path / "tf"
    shutil.copytree(TF, work)
    fmt = subprocess.run([tf, "fmt", "-check", "-recursive"], cwd=work, capture_output=True, text=True)
    assert fmt.returncode == 0, fmt.stdout
    init = subprocess.run([tf, "init", "-backend=false", "-input=false", "-no-color"], cwd=work,
                          capture_output=True, text=True, timeout=300)
    assert init.returncode == 0, init.stdout[-1500:] + init.stderr[-1500:]
    val = subprocess.run([tf, "validate", "-no-color"], cwd=work, capture_output=True, text=True)
    assert val.returncode == 0, val.stdout + val.stderr
    version = subprocess.run([tf, "version"], capture_output=True, text=True).stdout.splitlines()[0]
    provider = re.search(r"scaleway/scaleway v(\d+\.\d+\.\d+)", init.stdout)
    proof.line(f"{version}; provider scaleway {provider.group(1) if provider else '?'}")
    proof.line("terraform fmt -check: ok")
    proof.line(val.stdout.strip())


@pytest.mark.evidence("R2", control="terraform/main.tf", scope="config")
def test_tflint(proof, tmp_path):
    """tflint on the Terraform files, when tflint is available on the runner."""
    lint = shutil.which("tflint")
    if not lint:
        pytest.skip("tflint is not installed on this runner")
    work = tmp_path / "tf"
    shutil.copytree(TF, work)
    init = subprocess.run([lint, "--init"], cwd=work, capture_output=True, text=True, timeout=300)
    assert init.returncode == 0, init.stderr[-1500:]
    out = subprocess.run([lint, "--format", "compact"], cwd=work, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    version = subprocess.run([lint, "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    proof.line(f"{version}: no issue reported")


# ---------------------------------------------------------------- R3


@pytest.mark.evidence("R3", control="triage.config", scope="config")
def test_requested_scopes_are_the_narrow_ones(proof):
    """Gmail readonly + compose, Graph Mail.ReadWrite + offline_access, Todoist task:add; none of the wider scopes."""
    asked = set(config.GMAIL_SCOPES) | set(config.GRAPH_SCOPES) | set(config.TODOIST_SCOPES)
    assert not asked & set(config.FORBIDDEN_SCOPES)
    assert config.GMAIL_SCOPES == ("https://www.googleapis.com/auth/gmail.readonly",
                                   "https://www.googleapis.com/auth/gmail.compose")
    assert "Mail.Send" not in config.GRAPH_SCOPES
    proof.line("gmail: " + " ".join(s.rsplit("/", 1)[1] for s in config.GMAIL_SCOPES))
    proof.line("graph: " + " ".join(config.GRAPH_SCOPES) + " (delegated)")
    proof.line("todoist: " + " ".join(config.TODOIST_SCOPES))


@pytest.mark.evidence("R3", control="SECURITY.md", scope="config")
def test_security_md_declares_three_fields_per_connector(proof):
    """SECURITY.md states, for Gmail, Graph and Todoist: rights granted by the token, restriction in code, residual risk."""
    text = (ROOT / "SECURITY.md").read_text()
    for connector in ("Gmail", "Microsoft Graph", "Todoist", "Model endpoint"):
        section = text.split(f"### {connector}", 1)
        assert len(section) == 2, connector
        body = section[1].split("\n### ", 1)[0]
        for field in ("Rights the token really grants", "Restriction enforced by the code", "Residual risk"):
            assert field in body, f"{connector}: {field}"
    assert "gmail.compose" in text and "can send" in text
    assert "stolen token" in text
    proof.line("4 connectors x 3 fields present; 'gmail.compose ... can send' and the stolen-token limit are stated")


@pytest.mark.evidence("R3", control="triage.transport:_todoist_sync_body")
@pytest.mark.parametrize("command,why", [
    ({"type": "item_add", "uuid": "u", "args": {"content": "x", "project_id": "OTHER"}}, "other project"),
    ({"type": "item_add", "uuid": "u", "args": {"content": "x"}}, "no project (would land in Inbox)"),
    ({"type": "item_add", "uuid": "u", "args": {"content": "x", "project_id": PROJECT_ID, "due": {"string": "tomorrow"}}}, "due date"),
    ({"type": "item_delete", "uuid": "u", "args": {"id": "1"}}, "delete"),
    ({"type": "project_add", "uuid": "u", "args": {"name": "X"}}, "new project"),
])
def test_todoist_token_is_kept_to_one_project_by_code(command, why, proof):
    """The Todoist token reaches every project; the code allows item_add in the configured project only."""
    import json
    http = build_client(build_rules(todoist_project_id=PROJECT_ID, llm_base_url="https://api.scaleway.ai/v1"),
                        inner=FakeCloud([FakeTodoist()]))
    with pytest.raises(PolicyViolation) as exc:
        http.request("POST", "https://api.todoist.com/api/v1/sync", form={"commands": json.dumps([command])},
                     headers={"Authorization": "Bearer t"})
    proof.line(f"{why}: {exc.value.reason}")


@pytest.mark.evidence("R3", control="triage.transport:_graph_reply_body")
@pytest.mark.parametrize("body,why", [
    ({"message": {"toRecipients": [{"emailAddress": {"address": "x@example.net"}}]}}, "add a recipient"),
    ({"message": {"bccRecipients": [{"emailAddress": {"address": "x@example.net"}}]}}, "add a bcc"),
])
def test_graph_create_reply_cannot_change_recipients(body, why, proof):
    """Mail.ReadWrite would let a draft be readdressed; the allowlist only lets createReply set the body and marker."""
    http = build_client(build_rules(todoist_project_id=PROJECT_ID, llm_base_url="https://api.scaleway.ai/v1"),
                        inner=FakeCloud([]))
    with pytest.raises(PolicyViolation) as exc:
        http.request("POST", "https://graph.microsoft.com/v1.0/me/messages/AAMk1/createReply", json_body=body,
                     headers={"Authorization": "Bearer t"})
    proof.line(f"{why}: {exc.value.reason}")


# ---------------------------------------------------------------- R4


@pytest.mark.evidence("R4", control=".github/workflows/monthly-rebuild.yml", scope="config")
def test_monthly_rebuild_workflow(proof):
    """Monthly cron and manual trigger; build without cache for linux/amd64; tests, pip-audit, SBOM, corpus run; push and update gated on secrets."""
    wf = WF.read_text()
    checks = {
        "cron '0 3 1 * *'": "cron: '0 3 1 * *'" in wf,
        "workflow_dispatch": "workflow_dispatch:" in wf,
        "--no-cache": "--no-cache" in wf,
        "linux/amd64": "linux/amd64" in wf,
        "pytest": "pytest" in wf,
        "pip-audit": "pip-audit" in wf,
        "CycloneDX SBOM": "cyclonedx" in wf.lower(),
        "image run on the corpus, network off": '--network none "$IMAGE:candidate" demo' in wf,
        "model catalogue check": "/v1/models" in wf,
        "push by digest": "@sha256" in wf or "digest" in wf,
        "push gated on secrets": "steps.secrets.outputs.present == 'true'" in wf,
    }
    assert all(checks.values()), {k: v for k, v in checks.items() if not v}
    for name in checks:
        proof.line(f"{name}: present")


@pytest.mark.evidence("R4", control="Dockerfile", scope="config")
def test_image_runs_as_non_root(proof):
    """The image declares a numeric non-root user and pins its Python base."""
    df = (ROOT / "Dockerfile").read_text()
    users = re.findall(r"^USER\s+(\S+)", df, re.M)
    assert users and users[-1] not in ("root", "0") and users[-1].split(":")[0].isdigit()
    assert re.search(r"^FROM python:3\.12[\w.-]*@sha256:[0-9a-f]{64}", df, re.M)
    proof.line(f"USER {users[-1]}; base image pinned by digest")
