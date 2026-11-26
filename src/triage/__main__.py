"""Command line.

  python -m triage run            one hourly pass (production; reads the environment)
  python -m triage check-config   lists missing settings by name, never by value
  python -m triage demo           replays the synthetic corpus through the fakes with the
                                  recorded local-Qwen outputs (no network, no key)
  python -m triage trace FILE     records the "one hour, traced" scenario as JSON
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path


def _production(argv) -> int:
    from triage.auth import GoogleToken, MicrosoftToken
    from triage.config import ConfigError, Settings
    from triage.job import Job
    from triage.ledger import FileLedger, S3Ledger
    from triage.llm import LLMClient
    from triage.log import JsonlLog
    from triage.providers.gmail import GmailProvider
    from triage.providers.graph import GraphProvider
    from triage.tasks import TodoistSink
    from triage.transport import build_client, build_rules

    try:
        s = Settings.from_env()
    except ConfigError as exc:
        print("missing configuration: " + ", ".join(exc.missing), file=sys.stderr)
        return 2
    rules = build_rules(todoist_project_id=s.todoist_project_id, llm_base_url=s.llm_base_url,
                        gmail_label=s.mailbox if s.provider == "gmail" else None,
                        graph_folder=s.mailbox if s.provider == "graph" else None,
                        ledger_s3=(s.ledger_bucket, s.ledger_prefix) if s.ledger_backend == "s3" else None,
                        ms_tenant=s.ms_tenant or None)
    http = build_client(rules)
    if s.provider == "gmail":
        provider = GmailProvider(http, s.mailbox, GoogleToken(http, s.google_client_id, s.google_client_secret,
                                                              s.google_refresh_token))
    else:
        provider = GraphProvider(http, s.mailbox, MicrosoftToken(http, s.ms_tenant, s.ms_client_id, s.ms_refresh_token))
    ledger = (FileLedger(s.ledger_path) if s.ledger_backend == "file"
              else S3Ledger(http, s.ledger_bucket, s.ledger_prefix, s.s3_access_key, s.s3_secret_key))
    job = Job(provider=provider, llm=LLMClient(http, s.llm_base_url, s.llm_model, s.llm_api_key),
              sink=TodoistSink(http, s.todoist_project_id, s.todoist_token), ledger=ledger, log=JsonlLog(sys.stdout))
    return job.run().exit_code


def _check_config(argv) -> int:
    from triage.config import ConfigError, Settings
    try:
        s = Settings.from_env()
    except ConfigError as exc:
        print("missing: " + ", ".join(exc.missing))
        return 2
    print(f"ok: provider={s.provider} ledger={s.ledger_backend} llm_host={s.llm_base_url.split('/')[2]}")
    return 0


def _demo(argv) -> int:
    from triage_fakes import recorded_script
    from triage_fakes.world import ROOT, build_world, file_ledger

    p = argparse.ArgumentParser(prog="python -m triage demo")
    p.add_argument("--provider", choices=["gmail", "graph"], default="gmail")
    p.add_argument("--examples", default=str(ROOT / "examples" / "qwen3-8b"))
    p.add_argument("--ledger", help="ledger directory (default: a new temporary one)")
    p.add_argument("--log", help="write the JSONL journal to this file (default: stdout)")
    a = p.parse_args(argv)
    if not Path(a.examples).is_dir():
        print(f"no recorded outputs in {a.examples}", file=sys.stderr)
        return 2
    world = build_world(recorded_script(a.examples), provider=a.provider)
    ledger_dir = a.ledger or tempfile.mkdtemp(prefix="triage-ledger-")
    if a.log:
        Path(a.log).parent.mkdir(parents=True, exist_ok=True)
    stream = open(a.log, "w") if a.log else sys.stdout
    try:
        summary = world.job(file_ledger(ledger_dir), stream=stream).run()
    finally:
        if a.log:
            stream.close()
    sent = len(world.gmail.sent) + len(world.graph.sent) + len(world.graph.forwarded)
    deleted = len(world.gmail.deleted) + len(world.graph.deleted)
    other_projects = sum(1 for t in world.todoist.tasks.values() if t["project_id"] != "6Jf8VQXxpwv56VQ7")
    print(f"messages={summary.seen} drafts={summary.drafts} tasks={summary.tasks} "
          f"needs_human={summary.needs_human} retry_later={summary.retry_later} "
          f"sent={sent} deleted={deleted} tasks_outside_project={other_projects} "
          f"requests={len(world.recorder.emitted)} ledger={ledger_dir}", file=sys.stderr)
    if summary.exit_code == 0 and (sent or deleted or other_projects):
        return 1
    return summary.exit_code


def _trace(argv) -> int:
    from triage_fakes.trace import record_trace
    p = argparse.ArgumentParser(prog="python -m triage trace")
    p.add_argument("out", help="JSON file to write")
    a = p.parse_args(argv)
    record_trace(Path(a.out))
    print(f"trace written to {a.out}", file=sys.stderr)
    return 0


COMMANDS = {"run": _production, "check-config": _check_config, "demo": _demo, "trace": _trace}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
