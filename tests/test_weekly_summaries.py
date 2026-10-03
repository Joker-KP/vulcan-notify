"""Friday summaries: local time, persistent completion and output failures."""

import smtplib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from vulcan_notify import __main__ as cli
from vulcan_notify import email
from vulcan_notify.config import Settings
from vulcan_notify.models import Student
from vulcan_notify.sync import FullSyncResult, SyncResult, SyncSessionExpiredError

FRIDAY = datetime(2026, 10, 2, 13, tzinfo=UTC)  # 15:00 in Warsaw


@pytest.fixture
def config(tmp_path, monkeypatch):
    config = Settings(
        _env_file=None,
        tz="Europe/Warsaw",
        email_enabled=True,
        smtp_host="smtp.example.org",
        email_from="school@example.org",
        email_to=["parent@example.org"],
        llm_api_key="synthetic-key",
        session_file=tmp_path / "session.json",
    )
    monkeypatch.setattr(cli, "settings", config)
    monkeypatch.setattr(email, "settings", config)
    monkeypatch.setattr(cli, "_messages_context", AsyncMock(return_value=("Message input", 1)))
    monkeypatch.setattr(cli, "lessons_context", AsyncMock(return_value="Lesson input"))
    monkeypatch.setattr(cli, "summarize", AsyncMock(return_value="AI summary"))
    monkeypatch.setattr(email, "_send_email", MagicMock())
    return config


@pytest.mark.parametrize(
    "timestamp,zone,due",
    [
        ("2026-10-02T12:59:59+00:00", "Europe/Warsaw", False),
        ("2026-10-02T13:00:00+00:00", "Europe/Warsaw", True),
        ("2026-10-02T21:59:59+00:00", "Europe/Warsaw", True),
        ("2026-10-02T22:00:00+00:00", "Europe/Warsaw", False),
        ("2026-10-01T15:00:00+00:00", "Europe/Warsaw", False),
        ("2026-10-05T15:00:00+00:00", "Europe/Warsaw", False),
        ("2027-01-08T13:59:59+00:00", "Europe/Warsaw", False),
        ("2027-01-08T14:00:00+00:00", "Europe/Warsaw", True),
        ("2026-10-02T13:00:00+00:00", "UTC", False),
        ("2026-10-02T15:00:00+00:00", "UTC", True),
    ],
)
async def test_weekly_summary_uses_local_friday_start_time(db, config, timestamp, zone, due):
    config.tz = zone
    await cli._weekly_mix_summary(db, datetime.fromisoformat(timestamp))
    assert cli.summarize.await_count == (2 if due else 0)
    assert email._send_email.call_count == int(due)
    if due:
        cli._messages_context.assert_awaited_once_with(db, 7)
        cli.lessons_context.assert_awaited_once_with(db, config, days=7)
        assert await db.get_state(email.WEEKLY_MIX_STATE) == timestamp[:10]


@pytest.mark.parametrize("disabled", ["weekly_summary_enabled", "email_enabled", "llm_api_key"])
async def test_weekly_summary_requires_enabled_outputs(db, config, disabled):
    setattr(config, disabled, None if disabled == "llm_api_key" else False)
    await cli._weekly_mix_summary(db, FRIDAY)
    cli.summarize.assert_not_awaited()
    assert await db.get_state(email.WEEKLY_MIX_STATE) is None


async def test_weekly_summary_survives_restart_and_runs_next_friday(db, config):
    await cli._weekly_mix_summary(db, FRIDAY)
    await db.close()
    await db.connect()
    await cli._weekly_mix_summary(db, FRIDAY.replace(hour=16))
    assert cli.summarize.await_count == 2
    assert email._send_email.call_count == 1
    await cli._weekly_mix_summary(db, FRIDAY.replace(day=9))
    assert cli.summarize.await_count == 4
    assert email._send_email.call_count == 2
    assert await db.get_state(email.WEEKLY_MIX_STATE) == "2026-10-09"


async def test_smtp_failure_keeps_marker_and_retries_without_ai(db, config):
    email._send_email.side_effect = smtplib.SMTPException("private-value")
    await cli._weekly_mix_summary(db, FRIDAY)
    saved = (await db.list_email_outbox())[0]
    await db.close()
    await db.connect()
    assert await db.get_state(email.WEEKLY_MIX_STATE) == "2026-10-02"
    await cli._weekly_mix_summary(db, FRIDAY)
    assert cli.summarize.await_count == 2
    assert email._send_email.call_count == 1
    email._send_email.side_effect = None
    assert await email.drain_email_outbox(db) == (1, 0)
    retried = email._send_email.call_args.args[0]
    for key in ("body", "html_body", "subject", "message_id"):
        assert saved[key] == retried[key]
    assert cli.summarize.await_count == 2


async def test_ai_failure_is_retryable_and_does_not_mark_completion(db, config):
    cli.summarize.return_value = None
    await cli._weekly_mix_summary(db, FRIDAY)
    assert await db.get_state(email.WEEKLY_MIX_STATE) is None
    assert await db.list_email_outbox() == []
    cli.summarize.return_value = "Recovered summary"
    await cli._weekly_mix_summary(db, FRIDAY)
    assert await db.get_state(email.WEEKLY_MIX_STATE) == "2026-10-02"
    email._send_email.assert_called_once()


async def test_no_source_data_finishes_week_without_ai_or_email(db, config):
    cli._messages_context.return_value = ("", 0)
    cli.lessons_context.return_value = ""
    await cli._weekly_mix_summary(db, FRIDAY)
    await db.close()
    await db.connect()
    await cli._weekly_mix_summary(db, FRIDAY)
    assert await db.get_state(email.WEEKLY_MIX_STATE) == "2026-10-02"
    cli._messages_context.assert_awaited_once()
    cli.summarize.assert_not_awaited()
    email._send_email.assert_not_called()


async def test_marker_failure_rolls_back_email_before_retry(db, config, monkeypatch, caplog):
    original = db.set_state
    monkeypatch.setattr(db, "set_state", AsyncMock(side_effect=RuntimeError("private-value")))
    await cli._weekly_mix_summary(db, FRIDAY)
    await db.close()
    await db.connect()
    assert await db.get_state(email.WEEKLY_MIX_STATE) is None
    assert await db.list_email_outbox() == []
    email._send_email.assert_not_called()
    assert "private-value" not in caplog.text
    monkeypatch.setattr(db, "set_state", original)
    await cli._weekly_mix_summary(db, FRIDAY)
    email._send_email.assert_called_once()


async def test_scheduled_identity_deduplicates_each_recipient(db, config):
    config.email_to = ["first@example.org", "second@example.org"]
    for _ in range(2):
        await email.queue_mix_summary(db, "Messages", "Lessons", 7, weekly_period="2026-10-02")
    assert len(await db.list_email_outbox()) == 2
    assert await email.drain_email_outbox(db) == (2, 0)
    await email.queue_mix_summary(db, "Messages", "Lessons", 7, weekly_period="2026-10-02")
    assert await db.list_email_outbox() == []
    # Explicit summaries remain independent of the automatic weekly action.
    await email.queue_mix_summary(db, "Messages", "Lessons", 7)
    assert len(await db.list_email_outbox()) == 2


@pytest.mark.parametrize("outcome", ["success", "degraded", "no_students", "recovered", "expired"])
async def test_sync_runs_summary_only_after_successful_outputs(db, config, monkeypatch, outcome):
    class Clock:
        @staticmethod
        def now(zone):
            return FRIDAY

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(cli, "Database", lambda path: db)
    monkeypatch.setattr(cli, "_ensure_session", AsyncMock(return_value={}))
    monkeypatch.setattr(cli, "VulcanClient", MagicMock(return_value=MagicMock(close=AsyncMock())))
    student = SyncResult(Student("key", "Student", "1A", "School", 1, ""))
    result = FullSyncResult([student])
    if outcome == "degraded":
        student.failed_sections["completed_lessons"] = "fetch_failed"
    elif outcome == "no_students":
        result.student_results = []
    sync = AsyncMock(return_value=result)
    if outcome in ("recovered", "expired"):
        failure = SyncSessionExpiredError("expired", result)
        sync.side_effect = [failure, result if outcome == "recovered" else failure]
        monkeypatch.setattr(cli, "_get_credentials", lambda: ("fixture", "fixture"))
        monkeypatch.setattr(cli, "_recover_session", AsyncMock(return_value={}))
    monkeypatch.setattr(cli, "sync_all", sync)
    events = []
    for name in ("publish_email", "_sync_calendar", "publish_changes"):

        async def output(*args, name=name):
            events.append(name)

        monkeypatch.setattr(cli, name, output)

    async def weekly(database, started_at):
        assert database is db and started_at == FRIDAY
        events.append("weekly")

    monkeypatch.setattr(cli, "_weekly_mix_summary", weekly)
    if outcome in ("success", "recovered"):
        await cli.cmd_sync()
        assert events[-4:] == ["publish_email", "_sync_calendar", "publish_changes", "weekly"]
        assert events.count("weekly") == 1
    else:
        monkeypatch.setattr(cli, "publish_auth_failure", AsyncMock())
        with pytest.raises(SystemExit):
            await cli.cmd_sync()
        assert "weekly" not in events
