"""Tests for macOS Calendar integration."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vulcan_notify import __main__ as cli
from vulcan_notify import calendar as calendar_mod
from vulcan_notify.calendar import (
    CalendarSyncResult,
    _escape_applescript,
    _event_body,
    _exam_title,
    _homework_title,
    _parse_date,
    sync_to_calendar,
)
from vulcan_notify.config import settings
from vulcan_notify.db import Database
from vulcan_notify.models import Exam, Homework, Student

STUDENT = Student(
    key="KEY1",
    name="Alice Smith",
    class_name="4B",
    school="Szkola",
    diary_id=1001,
    mailbox_key="aaa",
)

EXAM = Exam(
    id=100,
    date="2026-03-25T00:00:00+01:00",
    subject="Matematyka",
    type=2,
    description="Test z mnozenia",
    teacher="Kowalski Jan",
)

HOMEWORK = Homework(
    id=200,
    date="2026-03-26T00:00:00+01:00",
    subject="Plastyka",
    content="Przyniesc blok rysunkowy",
    teacher="Nowak Anna",
)


# ── Unit tests for helpers ────────────────────────────────────────


def test_parse_date_iso() -> None:
    assert _parse_date("2026-03-25T00:00:00+01:00") == "2026-03-25"


def test_parse_date_plain() -> None:
    assert _parse_date("2026-03-25") == "2026-03-25"


def test_parse_date_invalid() -> None:
    assert _parse_date("not-a-date") == "not-a-date"


def test_exam_title_quiz() -> None:
    assert _exam_title("Matematyka", 2) == "Kartkowka - Matematyka"


def test_exam_title_test() -> None:
    assert _exam_title("Historia", 1) == "Sprawdzian - Historia"


def test_exam_title_unknown() -> None:
    assert _exam_title("Fizyka", 99) == "Sprawdzian/kartkowka - Fizyka"


def test_homework_title() -> None:
    assert _homework_title("Plastyka") == "Zadanie domowe - Plastyka"


def test_event_body_full() -> None:
    body = _event_body("Do page 5", "Kowalski")
    assert "Do page 5" in body
    assert "Nauczyciel: Kowalski" in body


def test_event_body_no_description() -> None:
    body = _event_body(None, "Kowalski")
    assert body == "Nauczyciel: Kowalski"


def test_event_body_no_teacher() -> None:
    body = _event_body("Do page 5", None)
    assert body == "Do page 5"


def test_event_body_empty() -> None:
    assert _event_body(None, None) == ""


def test_escape_applescript() -> None:
    assert _escape_applescript('He said "hi"') == 'He said \\"hi\\"'
    assert _escape_applescript("back\\slash") == "back\\\\slash"


# ── DB integration tests ─────────────────────────────────────────


async def test_calendar_uid_column_exists(db: Database) -> None:
    """Verify calendar_uid column exists on exams and homework tables."""
    for table in ("exams", "homework"):
        cursor = await db.db.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "calendar_uid" in columns


async def test_set_and_clear_calendar_uid(db: Database) -> None:
    await db.upsert_student(STUDENT)
    await db.upsert_exam(STUDENT.key, EXAM)
    await db.commit()

    await db.set_calendar_uid("exams", EXAM.id, "UID-123")
    await db.commit()

    cursor = await db.db.execute("SELECT calendar_uid FROM exams WHERE id = ?", (EXAM.id,))
    assert (await cursor.fetchone())[0] == "UID-123"

    await db.clear_calendar_uid("exams", EXAM.id)
    await db.commit()

    cursor = await db.db.execute("SELECT calendar_uid FROM exams WHERE id = ?", (EXAM.id,))
    assert (await cursor.fetchone())[0] is None


async def test_get_items_for_calendar(db: Database) -> None:
    await db.upsert_student(STUDENT)
    await db.upsert_exam(STUDENT.key, EXAM)
    await db.upsert_homework(STUDENT.key, HOMEWORK)
    await db.commit()

    items = await db.get_items_for_calendar(STUDENT.key)
    assert len(items["exams"]) == 1
    assert len(items["homework"]) == 1
    assert items["exams"][0]["subject"] == "Matematyka"
    assert items["homework"][0]["subject"] == "Plastyka"


async def test_get_items_excludes_soft_deleted(db: Database) -> None:
    await db.upsert_student(STUDENT)
    await db.upsert_exam(STUDENT.key, EXAM)
    await db.commit()

    # Soft-delete by marking with a different set of IDs
    await db.mark_missing(STUDENT.key, "exams", {999})
    await db.commit()

    items = await db.get_items_for_calendar(STUDENT.key)
    assert len(items["exams"]) == 0


async def test_get_deleted_items_with_calendar_uid(db: Database) -> None:
    await db.upsert_student(STUDENT)
    await db.upsert_exam(STUDENT.key, EXAM)
    await db.set_calendar_uid("exams", EXAM.id, "UID-456")
    await db.commit()

    # Soft-delete by marking with a different set of IDs
    await db.mark_missing(STUDENT.key, "exams", {999})
    await db.commit()

    deleted = await db.get_deleted_items_with_calendar_uid(STUDENT.key)
    assert len(deleted["exams"]) == 1
    assert deleted["exams"][0]["calendar_uid"] == "UID-456"


async def test_clear_all_calendar_uids(db: Database) -> None:
    await db.upsert_student(STUDENT)
    await db.upsert_exam(STUDENT.key, EXAM)
    await db.upsert_homework(STUDENT.key, HOMEWORK)
    await db.set_calendar_uid("exams", EXAM.id, "UID-1")
    await db.set_calendar_uid("homework", HOMEWORK.id, "UID-2")
    await db.commit()

    await db.clear_all_calendar_uids()

    cursor = await db.db.execute("SELECT calendar_uid FROM exams WHERE id = ?", (EXAM.id,))
    assert (await cursor.fetchone())[0] is None
    cursor = await db.db.execute("SELECT calendar_uid FROM homework WHERE id = ?", (HOMEWORK.id,))
    assert (await cursor.fetchone())[0] is None


# ── sync_to_calendar integration tests (mocked AppleScript) ──────


@patch("vulcan_notify.calendar.settings")
@patch("vulcan_notify.calendar._run_applescript")
async def test_sync_creates_events(
    mock_applescript: AsyncMock,
    mock_settings: AsyncMock,
    db: Database,
) -> None:
    mock_settings.calendar_map = {"Alice Smith": "School Alice"}
    mock_settings.calendar_reminder_hours = 24
    mock_applescript.return_value = "NEW-UID-1"

    await db.upsert_student(STUDENT)
    await db.upsert_exam(STUDENT.key, EXAM)
    await db.upsert_homework(STUDENT.key, HOMEWORK)
    await db.commit()

    result = await sync_to_calendar(db)

    assert result.created == 2
    assert result.updated == 0
    assert result.errors == 0

    # Verify UIDs stored
    cursor = await db.db.execute("SELECT calendar_uid FROM exams WHERE id = ?", (EXAM.id,))
    assert (await cursor.fetchone())[0] == "NEW-UID-1"


@patch("vulcan_notify.calendar.settings")
@patch("vulcan_notify.calendar._run_applescript")
async def test_sync_updates_existing_events(
    mock_applescript: AsyncMock,
    mock_settings: AsyncMock,
    db: Database,
) -> None:
    mock_settings.calendar_map = {"Alice Smith": "School Alice"}
    mock_settings.calendar_reminder_hours = 24
    mock_applescript.return_value = ""

    await db.upsert_student(STUDENT)
    await db.upsert_exam(STUDENT.key, EXAM)
    await db.set_calendar_uid("exams", EXAM.id, "EXISTING-UID")
    await db.commit()

    result = await sync_to_calendar(db)

    assert result.updated == 1
    assert result.created == 0


@pytest.mark.parametrize("table,item", [("exams", EXAM), ("homework", HOMEWORK)])
@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_transient_calendar_failure_retains_uid_and_retries(
    db, monkeypatch, table, item, operation
):
    monkeypatch.setattr(settings, "calendar_map", {STUDENT.name: "School Alice"})
    script = AsyncMock(side_effect=RuntimeError("temporary Calendar failure"))
    monkeypatch.setattr(calendar_mod, "_run_applescript", script)
    await db.upsert_student(STUDENT)
    await getattr(db, "upsert_exam" if table == "exams" else "upsert_homework")(STUDENT.key, item)
    await db.set_calendar_uid(table, item.id, "EXISTING")
    if operation == "delete":
        await db.db.execute(
            f"UPDATE {table} SET deleted_at=CURRENT_TIMESTAMP WHERE id=?", (item.id,)
        )
    await db.commit()
    failed = await sync_to_calendar(db)
    assert failed.errors == 1
    cursor = await db.db.execute(f"SELECT calendar_uid FROM {table} WHERE id=?", (item.id,))
    assert (await cursor.fetchone())[0] == "EXISTING"
    script.side_effect = None
    script.return_value = "UPDATED"
    retried = await sync_to_calendar(db)
    assert retried.errors == 0
    assert retried.created == 0
    assert getattr(retried, "updated" if operation == "update" else "deleted") == 1


@pytest.mark.parametrize("table,item", [("exams", EXAM), ("homework", HOMEWORK)])
async def test_confirmed_missing_event_can_be_recreated(db, monkeypatch, table, item):
    monkeypatch.setattr(settings, "calendar_map", {STUDENT.name: "School Alice"})
    script = AsyncMock(return_value=calendar_mod._MISSING_EVENT)
    monkeypatch.setattr(calendar_mod, "_run_applescript", script)
    await db.upsert_student(STUDENT)
    await getattr(db, "upsert_exam" if table == "exams" else "upsert_homework")(STUDENT.key, item)
    await db.set_calendar_uid(table, item.id, "MISSING")
    await db.commit()
    assert (await sync_to_calendar(db)).errors == 1
    script.return_value = "NEW-UID"
    assert (await sync_to_calendar(db)).created == 1


async def test_force_calendar_sync_preserves_uid_after_failed_deletion(db, monkeypatch):
    monkeypatch.setattr(settings, "db_path", db._db_path)
    monkeypatch.setattr(settings, "calendar_map", {STUDENT.name: "School Alice"})
    monkeypatch.setattr(
        calendar_mod, "_delete_event", AsyncMock(side_effect=RuntimeError("temporary"))
    )
    monkeypatch.setattr(cli, "sync_to_calendar", AsyncMock(return_value=CalendarSyncResult()))
    await db.upsert_student(STUDENT)
    await db.upsert_exam(STUDENT.key, EXAM)
    await db.set_calendar_uid("exams", EXAM.id, "EXISTING")
    await db.commit()
    await cli.cmd_calendar()
    cursor = await db.db.execute("SELECT calendar_uid FROM exams WHERE id=?", (EXAM.id,))
    assert (await cursor.fetchone())[0] == "EXISTING"


@pytest.mark.parametrize("cancelled", [False, True])
async def test_applescript_deadline_and_cancellation_kill_and_reap_process(monkeypatch, cancelled):
    monkeypatch.setattr(settings, "calendar_timeout_seconds", 0.01 if not cancelled else 30)
    started = asyncio.Event()
    calls = 0

    async def communicate():
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await asyncio.Future()
        return b"", b""

    process = MagicMock(communicate=AsyncMock(side_effect=communicate))
    monkeypatch.setattr(
        calendar_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    task = asyncio.create_task(calendar_mod._run_applescript("fixture"))
    await started.wait()
    if cancelled:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancelled else TimeoutError):
        await task
    process.kill.assert_called_once()
    assert process.communicate.await_count == 2


@patch("vulcan_notify.calendar.settings")
@patch("vulcan_notify.calendar._run_applescript")
async def test_sync_deletes_soft_deleted_events(
    mock_applescript: AsyncMock,
    mock_settings: AsyncMock,
    db: Database,
) -> None:
    mock_settings.calendar_map = {"Alice Smith": "School Alice"}
    mock_settings.calendar_reminder_hours = 24
    mock_applescript.return_value = ""

    await db.upsert_student(STUDENT)
    await db.upsert_exam(STUDENT.key, EXAM)
    await db.set_calendar_uid("exams", EXAM.id, "TO-DELETE-UID")
    await db.commit()

    # Soft-delete by marking with a different set of IDs
    await db.mark_missing(STUDENT.key, "exams", {999})
    await db.commit()

    result = await sync_to_calendar(db)

    assert result.deleted == 1

    # UID should be cleared
    cursor = await db.db.execute("SELECT calendar_uid FROM exams WHERE id = ?", (EXAM.id,))
    assert (await cursor.fetchone())[0] is None


@patch("vulcan_notify.calendar.settings")
async def test_sync_skips_unmapped_students(
    mock_settings: AsyncMock,
    db: Database,
) -> None:
    mock_settings.calendar_map = {}  # no mapping

    result = await sync_to_calendar(db)

    assert result == CalendarSyncResult()


@patch("vulcan_notify.calendar.settings")
@patch("vulcan_notify.calendar._run_applescript")
async def test_sync_skips_student_without_mapping(
    mock_applescript: AsyncMock,
    mock_settings: AsyncMock,
    db: Database,
) -> None:
    mock_settings.calendar_map = {"Other Student": "Other Calendar"}
    mock_settings.calendar_reminder_hours = 24

    await db.upsert_student(STUDENT)
    await db.upsert_exam(STUDENT.key, EXAM)
    await db.commit()

    result = await sync_to_calendar(db)

    assert result.created == 0
    assert "Alice Smith" in result.skipped_students
    mock_applescript.assert_not_called()


@patch("vulcan_notify.calendar.settings")
@patch("vulcan_notify.calendar._run_applescript")
async def test_sync_handles_applescript_error(
    mock_applescript: AsyncMock,
    mock_settings: AsyncMock,
    db: Database,
) -> None:
    mock_settings.calendar_map = {"Alice Smith": "School Alice"}
    mock_settings.calendar_reminder_hours = 24
    mock_applescript.side_effect = RuntimeError("AppleScript failed")

    await db.upsert_student(STUDENT)
    await db.upsert_exam(STUDENT.key, EXAM)
    await db.commit()

    result = await sync_to_calendar(db)

    assert result.errors == 1
    assert result.created == 0
