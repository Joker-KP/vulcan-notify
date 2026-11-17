"""Stored lesson topics: date windows, student isolation and optional AI context."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from vulcan_notify import __main__ as cli
from vulcan_notify import email
from vulcan_notify.config import Settings
from vulcan_notify.differ import Change
from vulcan_notify.models import CompletedLesson, Student
from vulcan_notify.summarizer import lessons_context
from vulcan_notify.sync import FullSyncResult, SyncResult

STUDENT = Student("profile-a", "Jan", "3A", "School", 1, "mailbox-a")
OTHER = replace(STUDENT, key="profile-b", mailbox_key="mailbox-b")
LESSON = CompletedLesson(1, "2026-10-01T09:00:00Z", 1, "Matematyka", "Teacher", "Ułamki")


@pytest.mark.parametrize(
    "now,start",
    [
        ("2026-10-03T12:00:00+00:00", "2026-09-26T22:00:00+00:00"),
        # The seven-day interval crosses the spring DST change.
        ("2026-03-30T12:00:00+00:00", "2026-03-23T23:00:00+00:00"),
    ],
)
async def test_context_filters_by_lesson_date_and_preserves_profiles(db, now, start):
    config = Settings(_env_file=None, tz="Europe/Warsaw")
    moment = datetime.fromisoformat(now)
    boundary = datetime.fromisoformat(start)
    await db.upsert_student(STUDENT)
    await db.upsert_student(OTHER)
    dates = [
        boundary - timedelta(seconds=1),
        boundary,
        moment,
        moment + timedelta(seconds=1),
    ]
    for index, date in enumerate(dates, 1):
        await db.upsert_completed_lesson(
            STUDENT.key, replace(LESSON, id=index, date=date.isoformat(), topic=f"Topic {index}")
        )
    # Equivalent offset and naive UTC dates must be included too.
    await db.upsert_completed_lesson(
        OTHER.key,
        replace(LESSON, date=boundary.astimezone(config.timezone).isoformat(), topic="Other topic"),
    )
    await db.upsert_completed_lesson(
        STUDENT.key, replace(LESSON, id=5, date=now, topic="Deleted topic")
    )
    await db.upsert_completed_lesson(STUDENT.key, replace(LESSON, id=6, date=now, topic="  "))
    await db.upsert_completed_lesson(
        STUDENT.key,
        replace(LESSON, id=7, date=moment.replace(tzinfo=None).isoformat(), topic="Naive topic"),
    )
    await db.mark_missing_completed_lessons(STUDENT.key, {1, 2, 3, 4, 6, 7}, start, now)
    context = await lessons_context(db, config, now=moment)
    rows = json.loads(context[context.index("[") :])
    assert [(r["student_key"], r["topic"]) for r in rows] == [
        (STUDENT.key, "Topic 2"),
        (STUDENT.key, "Topic 3"),
        (STUDENT.key, "Naive topic"),
        (OTHER.key, "Other topic"),
    ]
    assert rows[0]["date"].endswith("00:00:00+01:00" if "03-30" in now else "00:00:00+02:00")
    assert "ostatnie 7 dni" in context
    filtered = await lessons_context(db, config, now=moment, student_keys=[OTHER.key])
    assert "Other topic" in filtered and "Topic 2" not in filtered
    assert await lessons_context(db, config, now=moment, student_keys=[]) == ""
    await db.deactivate_students_except({STUDENT.key})
    assert "Other topic" not in await lessons_context(db, config, now=moment)


async def test_custom_days_empty_window_and_routines_are_left_for_ai(db):
    config = Settings(_env_file=None, llm_lessons_days=2)
    await db.upsert_student(STUDENT)
    await db.upsert_completed_lesson(STUDENT.key, replace(LESSON, topic="Obiad i dojazd"))
    now = datetime(2026, 10, 3, 12, tzinfo=UTC)
    assert await lessons_context(db, config, now=now) == ""
    context = await lessons_context(db, config, days=3, now=now)
    assert "Obiad i dojazd" in context  # Semantic filtering belongs in the prompt.
    assert "ostatnie 3 dni" in context
    with pytest.raises(ValueError, match="positive"):
        await lessons_context(db, config, days=0, now=now)


@pytest.mark.parametrize("days", [0, -1])
def test_lesson_summary_days_validation(days):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_lessons_days=days)


@pytest.fixture
def summary_config(monkeypatch):
    config = Settings(
        _env_file=None,
        email_enabled=True,
        smtp_host="smtp.example.org",
        email_from="school@example.org",
        email_to=["parent@example.org"],
        email_ai_summary=True,
        llm_api_key="synthetic-key",
        llm_include_lessons=True,
    )
    monkeypatch.setattr(email, "settings", config)
    monkeypatch.setattr(cli, "settings", config)
    return config


@pytest.mark.parametrize("enabled", [True, False])
async def test_email_context_is_opt_in_scoped_and_retries_reuse_it(
    db, summary_config, monkeypatch, enabled
):
    summary_config.llm_include_lessons = enabled
    summary_config.email_digest_groups = {"homework": False}
    await db.upsert_student(STUDENT)
    await db.upsert_student(OTHER)
    now = datetime.now(UTC).isoformat()
    await db.upsert_completed_lesson(STUDENT.key, replace(LESSON, date=now))
    await db.upsert_completed_lesson(
        OTHER.key, replace(LESSON, date=now, topic="Private other topic")
    )
    result = FullSyncResult(
        [
            SyncResult(STUDENT, new_grades=[Change("new", "grade", STUDENT.name, "Math: 5", "")]),
            SyncResult(OTHER, new_homework=[Change("new", "homework", OTHER.name, "Excluded", "")]),
        ]
    )

    async def ai(text, config):
        # Persist the ordinary digest before querying the model.
        stored = (await db.list_email_outbox())[0]
        assert "Math: 5" in stored["body"] and "Ułamki" not in stored["body"]
        assert ("Ułamki" in text) == enabled
        assert "Private other topic" not in text and "Excluded" not in text
        return "AI overview"

    mock = AsyncMock(side_effect=ai)
    monkeypatch.setattr(email, "summarize", mock)
    await email.queue_summary(result, db)
    row = (await db.list_email_outbox())[0]
    assert row["body"].startswith("AI overview")
    assert row["subject"] == "[eduVulcan] Oceny"
    await email.queue_summary(result, db)
    mock.assert_awaited_once()
    assert (await db.list_email_outbox())[0] == row


async def test_topics_alone_or_excluded_groups_do_not_trigger_email(
    db, summary_config, monkeypatch
):
    await db.upsert_student(STUDENT)
    await db.upsert_completed_lesson(
        STUDENT.key, replace(LESSON, date=datetime.now(UTC).isoformat())
    )
    mock = AsyncMock()
    monkeypatch.setattr(email, "summarize", mock)
    result = FullSyncResult([SyncResult(STUDENT, is_first_sync=True)])
    await email.queue_summary(result, db)
    result.student_results[0].completed_lesson_changes = [
        Change("new", "completed_lesson", STUDENT.name, "Topic", "")
    ]
    await email.queue_summary(result, db)
    summary_config.email_digest_groups = {"grade": False}
    result.student_results[0].new_grades = [Change("new", "grade", STUDENT.name, "Math: 5", "")]
    await email.queue_summary(result, db)
    assert await db.list_email_outbox() == []
    mock.assert_not_awaited()


async def test_context_failure_keeps_persisted_digest(db, summary_config, monkeypatch):
    monkeypatch.setattr(email, "lessons_context", AsyncMock(side_effect=RuntimeError()))
    ai = AsyncMock()
    monkeypatch.setattr(email, "summarize", ai)
    result = FullSyncResult(
        [SyncResult(STUDENT, new_grades=[Change("new", "grade", STUDENT.name, "Math: 5", "")])]
    )
    await email.queue_summary(result, db)
    assert "Math: 5" in (await db.list_email_outbox())[0]["body"]
    ai.assert_not_awaited()


async def test_cli_lesson_profile_and_change_context(db, summary_config, monkeypatch, capsys):
    await db.upsert_student(STUDENT)
    await db.upsert_completed_lesson(
        STUDENT.key, replace(LESSON, date=datetime.now(UTC).isoformat())
    )
    ai = AsyncMock(return_value="Overview")
    monkeypatch.setattr(cli, "summarize", ai)
    await cli._summarize_lessons(db, 7)
    assert "Ułamki" in ai.call_args.args[0]
    assert ai.call_args.kwargs["profile"] == "lessons"
    assert "Overview" in capsys.readouterr().out
    # Topics can be summarized even when the change query returns no changes.
    monkeypatch.setattr(db, "get_recent_changes", AsyncMock(return_value={}))
    await cli._summarize_changes(db, 7)
    assert "Ułamki" in ai.call_args.args[0]
    assert ai.call_args.kwargs["profile"] == "default"


async def test_cli_default_uses_configured_lesson_days(summary_config, monkeypatch):
    summary_config.llm_lessons_days = 3
    database = AsyncMock()
    monkeypatch.setattr(cli, "Database", lambda path: database)
    mock = AsyncMock()
    monkeypatch.setattr(cli, "_summarize_lessons", mock)
    await cli.cmd_summarize("lessons")
    mock.assert_awaited_once_with(database, 3)
    await cli.cmd_summarize("lessons", days=14)
    assert mock.call_args.args[1] == 14
    with pytest.raises(SystemExit):
        await cli.cmd_summarize("lessons", days=0)
    database.close.assert_awaited()


@pytest.mark.parametrize("day_args,expected_days", [([], 3), (["--days", "14"], 14)])
def test_main_routes_lessons_type(summary_config, monkeypatch, day_args, expected_days):
    summary_config.llm_lessons_days = 3
    database = AsyncMock()
    monkeypatch.setattr(cli, "Database", lambda path: database)
    mock = AsyncMock()
    monkeypatch.setattr(cli, "_summarize_lessons", mock)
    monkeypatch.setattr(
        cli.sys, "argv", ["vulcan-notify", "summarize", "--type", "lessons", *day_args]
    )
    cli.main()
    mock.assert_awaited_once_with(database, expected_days)
    database.close.assert_awaited_once()


@pytest.mark.parametrize("help_arg", ["--help", "-h"])
def test_summary_help_lists_lessons_without_starting_ai(monkeypatch, capsys, help_arg):
    mock = AsyncMock()
    monkeypatch.setattr(cli, "cmd_summarize", mock)
    monkeypatch.setattr(cli.sys, "argv", ["vulcan-notify", "summarize", help_arg])
    cli.main()
    output = capsys.readouterr().out
    assert "--type sync|messages|lessons" in output
    assert "LLM_LESSONS_DAYS" in output
    mock.assert_not_called()
