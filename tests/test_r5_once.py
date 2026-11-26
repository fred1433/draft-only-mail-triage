"""R5: each email handled once, across hours, containers, crashes and concurrent runs."""
from __future__ import annotations

import threading

import pytest

from tests.helpers import drafts, inbound_ids, make_world
from triage.ledger import FileLedger, LedgerUnavailable, S3Ledger, ledger_key
from triage.transport import build_client, build_rules
from triage_fakes import FakeCloud, FakeS3, Fault
from triage_fakes.world import PROJECT_ID, file_ledger

J = "triage.job:Job._handle"
DE = "AW: Geschäftsordnung des Prüfungsausschusses: Entwurf"


def counts(world):
    return len(drafts(world)), len(world.todoist.tasks)


@pytest.mark.evidence("R5", control=J)
@pytest.mark.parametrize("provider", ["gmail", "graph"])
def test_next_hour_in_a_new_container_changes_nothing(provider, tmp_path, proof):
    """Next hour, fresh container (new process, ledger reloaded from storage): no new draft, no new task."""
    world = make_world(provider)
    world.job(FileLedger(tmp_path), run_id="hour1").run()
    first = counts(world)
    s2 = world.job(FileLedger(tmp_path), run_id="hour2").run()     # new objects, same storage
    assert counts(world) == first and s2.drafts == 0 and s2.tasks == 0
    proof.line(f"{provider} hour 1: drafts={first[0]} tasks={first[1]}")
    proof.line(f"{provider} hour 2 (new container): drafts +{s2.drafts}, tasks +{s2.tasks}, skipped={s2.skipped}")


@pytest.mark.evidence("R5", control="triage.ledger:FileLedger.claim")
def test_two_concurrent_runs_make_one_draft_per_email(tmp_path, proof):
    """Two runs started at the same instant on the same mailbox: one draft per email, never two."""
    world = make_world()
    barrier = threading.Barrier(2)
    results = []

    def go(name):
        job = world.job(FileLedger(tmp_path), run_id=name)
        barrier.wait()
        results.append(job.run())
    threads = [threading.Thread(target=go, args=(n,)) for n in ("run-a", "run-b")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    per_thread = {}
    for m in world.gmail.draft_messages():
        per_thread[m.thread_id] = per_thread.get(m.thread_id, 0) + 1
    assert all(v == 1 for v in per_thread.values())
    assert sum(r.drafts for r in results) == len(per_thread)
    proof.line(f"run-a drafts={results[0].drafts} run-b drafts={results[1].drafts}; drafts per thread: {sorted(per_thread.values())}")


@pytest.mark.evidence("R5", control=J)
def test_lost_response_after_draft_is_reconciled_not_recreated(tmp_path, proof):
    """Response lost after the draft was written: the job finds its draft by marker and does not write a second one."""
    world = make_world(only=("00_de_previous_outbound.eml", "02_de_audit_followup.eml"))
    world.cloud.inject(Fault("POST", "gmail.googleapis.com", r".*/drafts", "lose_response"))
    s = world.job(FileLedger(tmp_path)).run()
    assert len(world.gmail.draft_messages()) == 1
    assert s.drafts == 1 and s.needs_human == 0
    events = [l["event"] for l in world.log_lines()]
    assert "draft_created" in events
    proof.line(f"POST drafts -> response lost; reconcile found marker; drafts in thread: {len(world.gmail.draft_messages())}")


@pytest.mark.evidence("R5", control=J)
def test_unknown_outcome_goes_to_a_person_not_a_blind_retry(tmp_path, proof):
    """Write timed out and no draft is visible: needs_human, one manual task, no second attempt."""
    world = make_world(only=("00_de_previous_outbound.eml", "02_de_audit_followup.eml"))
    world.cloud.inject(Fault("POST", "gmail.googleapis.com", r".*/drafts", "timeout"))
    s = world.job(FileLedger(tmp_path)).run()
    s2 = world.job(FileLedger(tmp_path)).run()
    assert len(world.gmail.draft_messages()) == 0
    assert [t["content"] for t in world.todoist.tasks.values()] == ["Manual reply needed"]
    assert s.needs_human == 1 and s2.drafts == 0
    proof.line("POST drafts -> timeout, nothing found on reconcile -> needs_human:draft_outcome_unknown")
    proof.line(f"next hour: drafts +{s2.drafts}")


@pytest.mark.evidence("R5", control=J)
def test_todoist_failure_then_resume_creates_the_task_not_a_second_draft(tmp_path, proof):
    """Todoist fails between draft and task; next hour the tasks are created and the draft is not written again."""
    world = make_world(only=("00_de_previous_outbound.eml", "02_de_audit_followup.eml"))
    world.cloud.inject(Fault("POST", "api.todoist.com", r"/api/v1/sync", "http_503"))
    s1 = world.job(FileLedger(tmp_path), run_id="hour1").run()
    assert (s1.drafts, len(world.todoist.tasks)) == (1, 0)
    s2 = world.job(FileLedger(tmp_path), run_id="hour2").run()
    assert s2.drafts == 0 and len(world.gmail.draft_messages()) == 1
    assert len(world.todoist.tasks) == 2
    proof.line(f"hour 1: drafts=1 tasks=0 (todoist 503) -> hour 2: drafts +{s2.drafts}, tasks +{s2.tasks}")


@pytest.mark.evidence("R5", control="triage.tasks:command_uuid")
def test_todoist_lost_response_does_not_duplicate_the_task(tmp_path, proof):
    """Todoist answer lost after the task was made: the retry reuses the same command uuid, Todoist does not run it twice."""
    world = make_world(only=("01_en_board_secretary.eml",))
    world.cloud.inject(Fault("POST", "api.todoist.com", r"/api/v1/sync", "lose_response"))
    world.job(FileLedger(tmp_path)).run()
    world.job(FileLedger(tmp_path)).run()
    titles = [t["content"] for t in world.todoist.tasks.values()]
    assert len(titles) == len(set(titles)) == 2
    proof.line(f"tasks after lost response and retry: {len(titles)} (no duplicate)")


@pytest.mark.evidence("R5", control=J)
@pytest.mark.parametrize("action", ["deleted", "sent", "edited"])
def test_a_draft_the_human_touched_is_never_recreated(action, tmp_path, proof):
    """Draft deleted, sent or edited by a person: the next hours leave it alone."""
    world = make_world(only=("01_en_board_secretary.eml",))
    world.job(FileLedger(tmp_path)).run()
    did = next(iter(world.gmail.drafts))
    getattr(world.gmail, {"deleted": "human_delete_draft", "sent": "human_send_draft", "edited": "human_edit_draft"}[action])(
        *([did, "My own words."] if action == "edited" else [did]))
    s2 = world.job(FileLedger(tmp_path)).run()
    s3 = world.job(FileLedger(tmp_path)).run()
    assert s2.drafts == s3.drafts == 0
    proof.line(f"draft {action} by a person; hours 2 and 3: drafts +0")


@pytest.mark.evidence("R5", control="triage.providers.gmail:GmailProvider.reconcile")
def test_sent_draft_is_recognised_even_if_the_ledger_was_lost(tmp_path, proof):
    """Ledger lost and the person already sent our draft: the marker in the thread stops a second draft."""
    world = make_world(only=("01_en_board_secretary.eml",))
    world.job(FileLedger(tmp_path / "a")).run()
    world.gmail.human_send_draft(next(iter(world.gmail.drafts)))
    s = world.job(FileLedger(tmp_path / "b")).run()      # empty ledger
    assert s.drafts == 0
    proof.line("empty ledger + sent draft carrying the marker -> draft_reconciled:our_draft_sent, drafts +0")


@pytest.mark.evidence("R5", control="triage.providers.gmail:GmailProvider.reconcile")
def test_a_reply_already_sent_by_a_person_means_no_draft(tmp_path, proof):
    """Someone answered from the mailbox before the job ran: no draft, no task."""
    world = make_world(only=("01_en_board_secretary.eml",))
    inbound = next(iter(world.gmail.messages.values()))
    world.gmail.load_eml(b"From: office@advisory.example.org\r\nTo: g.holloway@harbourline.example.org\r\n"
                         b"Subject: Re: Training for our new board secretary\r\nDate: Thu, 26 Nov 2026 08:00:00 +0000\r\n"
                         b"Message-ID: <manual@advisory.example.org>\r\nIn-Reply-To: <c7e1@harbourline.example.org>\r\n"
                         b"References: <c7e1@harbourline.example.org>\r\n\r\nAnswered by phone.\r\n")
    s = world.job(FileLedger(tmp_path)).run()
    assert s.drafts == 0 and not world.todoist.tasks
    assert inbound.thread_id in {m.thread_id for m in world.gmail.messages.values() if "SENT" in m.labels}
    proof.line("human reply in thread -> human_replied, drafts=0 tasks=0")


class BrokenLedger(FileLedger):
    def ping(self):
        raise LedgerUnavailable("PermissionError")


@pytest.mark.evidence("R5", control="triage.job:Job.run")
def test_ledger_unavailable_stops_before_any_call(tmp_path, proof):
    """Ledger unreachable: the run stops with exit code 3 before reading or writing anything."""
    world = make_world()
    s = world.job(BrokenLedger(tmp_path)).run()
    assert s.exit_code == 3 and world.recorder.emitted == []
    proof.line(f"exit_code={s.exit_code}; requests emitted: {len(world.recorder.emitted)}")


@pytest.mark.evidence("R5", control="triage.ledger:S3Ledger.claim")
def test_s3_ledger_conditional_claims(proof):
    """Object Storage backend (fake S3, fr-par endpoint): conditional claim, second claimer refused, outage stops the run."""
    s3 = FakeS3()
    rules = build_rules(todoist_project_id=PROJECT_ID, llm_base_url="https://api.scaleway.ai/v1",
                        ledger_s3=("triage-ledger", "ledger"))
    http = build_client(rules, inner=FakeCloud([s3]))
    a = S3Ledger(http, "triage-ledger", "ledger", "AK", "SK")
    b = S3Ledger(http, "triage-ledger", "ledger", "AK", "SK")
    key = ledger_key("gmail", "Label_triage", "m1")
    a.ping()
    assert a.claim(key, "run-a") is True
    assert b.claim(key, "run-b") is False
    a.record(key, "draft_created", ref="r1", run_id="run-a")
    assert "draft_created" in b.get(key)
    a.release(key, "run-a")
    assert b.claim(key, "run-b") is True
    s3.down = True
    with pytest.raises(LedgerUnavailable):
        b.ping()
    proof.line("If-None-Match claim: run-a True, run-b False; after release: run-b True; outage -> LedgerUnavailable")


def test_revoked_access_stops_the_run_cleanly(tmp_path):
    """Refresh refused (access revoked): the run stops with exit code 5 and writes nothing."""
    from triage.auth import AuthFailed
    world = make_world()
    job = world.job(FileLedger(tmp_path))

    def revoked():
        raise AuthFailed("token refresh 400")
    job.provider._token = revoked
    s = job.run()
    assert s.exit_code == 5 and not world.todoist.tasks and not drafts(world)
    assert [l["event"] for l in world.log_lines()][-1] == "auth_failed"


def test_partial_configuration_is_refused():
    from triage.config import ConfigError, Settings
    with pytest.raises(ConfigError) as exc:
        Settings.from_env({"MAIL_PROVIDER": "gmail", "TODOIST_PROJECT_ID": "1"})
    assert "GMAIL_LABEL_ID" in exc.value.missing and "TODOIST_TOKEN" in exc.value.missing
    with pytest.raises(ConfigError) as exc:
        Settings.from_env({"MAIL_PROVIDER": "outlook", "TODOIST_PROJECT_ID": "1", "TODOIST_TOKEN": "t",
                           "LLM_API_KEY": "k", "LEDGER_PATH": "/tmp/x"})
    assert any("gmail or graph" in m for m in exc.value.missing)
