"""Integration tests for the sync pipeline."""

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from vulcan_notify.client import SessionExpiredError, VulcanFetchError
from vulcan_notify.db import Database
from vulcan_notify.models import (
    AttendanceEntry,
    ClassificationPeriod,
    DashboardData,
    Exam,
    Grade,
    Homework,
    Lesson,
    Message,
    Student,
)
from vulcan_notify.sync import SyncSessionExpiredError, sync_all, sync_messages, sync_student

STUDENT_A = Student(
    key="KEYA",
    name="Jan",
    class_name="3A",
    school="Szkola",
    diary_id=1001,
    mailbox_key="aaa",
)
STUDENT_B = Student(
    key="KEYB",
    name="Anna",
    class_name="5B",
    school="Szkola",
    diary_id=1002,
    mailbox_key="bbb",
)

PERIOD = ClassificationPeriod(id=1, number=2, date_from="2026-02-01", date_to="2026-08-31")

GRADE = Grade(
    column_id=100,
    value="5",
    date="15.03.2026",
    subject="Math",
    column_name="Sprawdzian 1",
    category="Biezace",
    weight=2,
    teacher="Nowak A.",
    changed_since_login=False,
)

EXAM = Exam(id=10001, date="2026-03-16", subject="Przyroda", type=2)
HOMEWORK = Homework(id=10002, date="2026-03-16", subject="Plastyka")


def _make_mock_client(
    students: list[Student] | None = None,
    grades: list[Grade] | None = None,
    exams: list[Exam] | None = None,
    homework: list[Homework] | None = None,
) -> AsyncMock:
    client = AsyncMock()
    client.student_portal_url = MagicMock(
        side_effect=lambda student: f"https://uczen.eduvulcan.pl/example/App/{student.key}"
    )
    client.get_students = AsyncMock(return_value=[STUDENT_A] if students is None else students)
    client.get_periods = AsyncMock(return_value=[PERIOD])
    grade_list = [] if grades is None else grades
    client.get_grades = AsyncMock(return_value=grade_list)
    client.get_grades_and_summaries = AsyncMock(return_value=(grade_list, []))
    client.get_attendance = AsyncMock(return_value=[])
    client.get_exams = AsyncMock(return_value=[] if exams is None else exams)
    client.get_homework = AsyncMock(return_value=[] if homework is None else homework)
    client.get_schedule = AsyncMock(return_value=[])
    client.get_remarks = AsyncMock(return_value=[])
    client.get_completed_lessons = AsyncMock(return_value=[])
    client.get_dashboard = AsyncMock(return_value=DashboardData(unread_messages=5))
    client.get_messages = AsyncMock(return_value=[])
    client.get_message_detail = AsyncMock(return_value=None)
    client.get_exam_detail = AsyncMock(return_value=None)
    client.get_homework_detail = AsyncMock(return_value=None)
    client.close = AsyncMock()
    return client


async def test_first_sync_stores_baseline(db: Database) -> None:
    """First sync should store data but report no changes."""
    client = _make_mock_client(grades=[GRADE], exams=[EXAM])
    result = await sync_student(client, db, STUDENT_A)

    assert result.is_first_sync is True
    assert result.portal_url == "https://uczen.eduvulcan.pl/example/App/KEYA"
    assert result.has_changes is False

    # Data should be stored
    rows = await db.get_grades_for_student("KEYA")
    assert len(rows) == 1
    assert rows[0]["value"] == "5"


async def test_second_sync_no_changes(db: Database) -> None:
    """Second sync with same data should report no changes."""
    client = _make_mock_client(grades=[GRADE])

    # First sync (baseline)
    await sync_student(client, db, STUDENT_A)

    # Second sync (same data)
    result = await sync_student(client, db, STUDENT_A)

    assert result.is_first_sync is False
    assert result.has_changes is False


async def test_new_grade_detected_on_second_sync(db: Database) -> None:
    """New grade appearing after baseline should be detected."""
    # First sync with no grades
    client = _make_mock_client(grades=[])
    await sync_student(client, db, STUDENT_A)

    # Second sync with a new grade
    client = _make_mock_client(grades=[GRADE])
    result = await sync_student(client, db, STUDENT_A)

    assert result.is_first_sync is False
    assert len(result.new_grades) == 1
    assert result.new_grades[0].change_type == "new"
    assert "5" in result.new_grades[0].title


async def test_homework_details_reach_notifications_and_storage(db: Database) -> None:
    from vulcan_notify.config import Settings
    from vulcan_notify.email_digest import render_summary
    from vulcan_notify.mqtt import build_payload
    from vulcan_notify.sync import FullSyncResult

    await sync_student(_make_mock_client(), db, STUDENT_A)
    client = _make_mock_client(homework=[replace(HOMEWORK)])
    links = ["https://example.org/material", {"label": "Ćwiczenia", "url": "https://example.org/1"}]
    client.get_homework_detail.return_value = {
        "opis": '<p>Przeczytaj rozdział.</p><a href="https://example.org/2">Lektura</a>',
        "nauczycielImieNazwisko": "Anna Nowak",
        "linki": links,
        "odpowiedzWymagana": True,
        "terminOdpowiedzi": "2026-03-16T18:30:00+01:00",
    }
    result = await sync_student(client, db, STUDENT_A)
    change = result.new_homework[0]
    payload = build_payload(change)
    _, plain, html, count = render_summary(
        FullSyncResult([result]), Settings(_env_file=None, tz="Europe/Warsaw")
    )
    assert count == 1
    for output in (change.body, plain, html):
        assert "Przeczytaj rozdział." in output
        assert "Anna Nowak" in output
        assert "https://example.org/1" in output
        assert "https://example.org/2" in output
        assert "Odpowiedź wymagana: Tak" in output
        assert "Termin odpowiedzi:" in output
    assert "2026-03-16 18:30" in plain
    assert payload["links"] == links
    assert payload["response_required"] is True
    assert payload["response_deadline"] == "2026-03-16T18:30:00+01:00"
    row = (await db.get_homework_for_student(STUDENT_A.key))[0]
    for field in (
        "content",
        "teacher",
        "links",
        "response_required",
        "response_deadline",
        "details",
    ):
        assert row[field] == payload[field]
    assert await db.get_homework_for_student(STUDENT_B.key) == []

    # The list omits detail fields; a restart must retain them without re-notifying.
    await db.close()
    await db.connect()
    client = _make_mock_client(homework=[replace(HOMEWORK)])
    repeated = await sync_student(client, db, STUDENT_A)
    assert repeated.new_homework == []
    client.get_homework_detail.assert_not_awaited()
    assert (await db.get_homework_for_student(STUDENT_A.key))[0] == row


@pytest.mark.parametrize("initial", [True, False])
async def test_homework_detail_backfill_is_silent_and_accepts_empty_description(db, initial):
    if not initial:
        await sync_student(_make_mock_client(), db, STUDENT_A)
        # Simulate a task persisted by a previous version, already containing an empty description.
        await db.upsert_homework(STUDENT_A.key, replace(HOMEWORK, content=""))
        await db.commit()
    client = _make_mock_client(homework=[replace(HOMEWORK)])
    client.get_homework_detail.return_value = {
        "opis": "",
        "nauczycielImieNazwisko": "Anna Nowak",
        "linki": [],
        "odpowiedzWymagana": False,
    }
    result = await sync_student(client, db, STUDENT_A)
    assert result.new_homework == []
    row = (await db.get_homework_for_student(STUDENT_A.key))[0]
    assert row["content"] == ""
    assert row["teacher"] == "Anna Nowak"
    assert row["response_required"] is False
    assert row["links"] == []
    assert row["response_deadline"] is None
    assert await db.get_homework_missing_detail(STUDENT_A.key) == set()


async def test_homework_detail_failure_retries_without_duplicate_notification(db):
    await sync_student(_make_mock_client(), db, STUDENT_A)
    client = _make_mock_client(homework=[replace(HOMEWORK)])
    client.get_homework_detail.side_effect = VulcanFetchError("Synthetic detail failure")
    result = await sync_student(client, db, STUDENT_A)
    assert len(result.new_homework) == 1
    assert result.failed_sections == {}
    assert await db.get_homework_missing_detail(STUDENT_A.key) == {HOMEWORK.id}
    client.get_homework_detail.side_effect = None
    client.get_homework_detail.return_value = {"opis": "Uzupełniony opis", "linki": []}
    result = await sync_student(client, db, STUDENT_A)
    assert result.new_homework == []
    assert (await db.get_homework_for_student(STUDENT_A.key))[0]["content"] == "Uzupełniony opis"
    assert await db.get_homework_missing_detail(STUDENT_A.key) == set()


async def test_homework_detail_expiry_does_not_consume_new_task(db):
    await sync_student(_make_mock_client(), db, STUDENT_A)
    client = _make_mock_client(homework=[replace(HOMEWORK)])
    client.get_homework_detail.side_effect = SessionExpiredError("Synthetic expiry")
    with pytest.raises(SyncSessionExpiredError):
        await sync_student(client, db, STUDENT_A)
    assert await db.get_homework_ids_for_student(STUDENT_A.key) == set()
    client.get_homework_detail.side_effect = None
    client.get_homework_detail.return_value = {"opis": "Opis po odzyskaniu sesji"}
    result = await sync_student(client, db, STUDENT_A)
    assert len(result.new_homework) == 1
    assert "Opis po odzyskaniu sesji" in result.new_homework[0].body


async def test_homework_calendar_upgrade_imports_history_then_notifies_new_ids(db):
    # An installation that previously used the empty dashboard already has its old baseline.
    await db.upsert_student(STUDENT_A)
    await db.set_state("last_sync:KEYA", "2026-10-08T17:00:00Z")
    await db.set_state("last_sync:KEYA:homework", "2026-10-08T17:00:00Z")
    old = replace(HOMEWORK, date="2026-10-09T00:00:00+02:00")
    client = _make_mock_client(homework=[old])
    client.get_homework_detail.return_value = {"opis": "History task", "zalaczniki": []}
    result = await sync_student(client, db, STUDENT_A)
    assert not result.is_first_sync
    assert result.new_homework == []
    assert (await db.get_homework_for_student(STUDENT_A.key))[0]["content"] == "History task"
    assert await db.get_state("last_sync:KEYA:homework_calendar") is not None
    await db.close()
    await db.connect()
    new = replace(old, id=old.id + 1)
    client = _make_mock_client(homework=[replace(old), new])
    client.get_homework_detail.return_value = {"opis": "New task"}
    result = await sync_student(client, db, STUDENT_A)
    assert [change.raw.id for change in result.new_homework] == [new.id]
    assert "New task" in result.new_homework[0].body
    assert (await sync_student(client, db, STUDENT_A)).new_homework == []


async def test_homework_calendar_failure_keeps_baseline_uninitialized_and_history(db):
    await db.upsert_student(STUDENT_A)
    await db.set_state("last_sync:KEYA", "2026-10-08T17:00:00Z")
    await db.upsert_homework(STUDENT_A.key, replace(HOMEWORK))
    client = _make_mock_client()
    client.get_homework.side_effect = VulcanFetchError("Synthetic calendar failure")
    result = await sync_student(client, db, STUDENT_A)
    assert "homework" in result.failed_sections
    assert await db.get_state("last_sync:KEYA:homework_calendar") is None
    assert len(await db.get_homework_for_student(STUDENT_A.key)) == 1
    client.get_homework.side_effect = None
    client.get_homework.return_value = [replace(HOMEWORK, id=HOMEWORK.id + 1)]
    assert (await sync_student(client, db, STUDENT_A)).new_homework == []


async def test_homework_uses_configured_local_days_across_dst(db, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from vulcan_notify import sync
    from vulcan_notify.config import Settings

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 25, 12, tzinfo=ZoneInfo("Europe/Warsaw")).astimezone(tz)

    monkeypatch.setattr(sync, "datetime", Clock)
    monkeypatch.setattr(
        sync,
        "settings",
        Settings(
            _env_file=None, sync_homework_days=1, sync_homework_future_days=1, tz="Europe/Warsaw"
        ),
    )
    client = _make_mock_client()
    await sync_student(client, db, STUDENT_A)
    client.get_homework.assert_awaited_once_with(
        STUDENT_A, "2026-10-23T22:00:00.000Z", "2026-10-26T22:59:59.999Z"
    )


async def test_changed_grade_detected(db: Database) -> None:
    """Changed grade value should be detected as update."""
    grade_v1 = Grade(
        column_id=100,
        value="4",
        date="15.03.2026",
        subject="Math",
        column_name="Sprawdzian 1",
        category="Biezace",
        weight=2,
        teacher="Nowak A.",
        changed_since_login=False,
    )

    # First sync with grade=4
    client = _make_mock_client(grades=[grade_v1])
    await sync_student(client, db, STUDENT_A)

    # Second sync with grade=5
    client = _make_mock_client(grades=[GRADE])  # GRADE has value="5"
    result = await sync_student(client, db, STUDENT_A)

    assert len(result.new_grades) == 1
    assert result.new_grades[0].change_type == "updated"
    assert "4" in result.new_grades[0].title
    assert "5" in result.new_grades[0].title


async def test_new_exam_detected(db: Database) -> None:
    """New exam should be detected after baseline."""
    client = _make_mock_client(exams=[])
    await sync_student(client, db, STUDENT_A)

    client = _make_mock_client(exams=[EXAM])
    result = await sync_student(client, db, STUDENT_A)

    assert len(result.new_exams) == 1
    assert "Przyroda" in result.new_exams[0].title


async def test_sync_all_multiple_students(db: Database) -> None:
    """sync_all should return separate results per student."""
    client = _make_mock_client(students=[STUDENT_A, STUDENT_B])
    full = await sync_all(client, db)

    assert len(full.student_results) == 2
    assert full.student_results[0].student.name == "Jan"
    assert full.student_results[1].student.name == "Anna"
    assert full.notification_id == "sync:1"


async def test_sync_all_no_students(db: Database) -> None:
    """sync_all with no students returns empty FullSyncResult."""
    client = _make_mock_client(students=[])
    full = await sync_all(client, db)
    assert full.student_results == []


async def test_sync_messages_backfills_legacy_content(db: Database) -> None:
    """Messages stored before detail-fetch was added get content backfilled."""
    # Seed a legacy message row directly: content NULL, api_global_key present.
    legacy = Message(
        id=9001,
        api_global_key="legacy-key",
        sender="Teacher A.",
        subject="Old note",
        date="2026-03-01",
        mailbox="Jan",
        has_attachments=False,
        is_read=True,
    )
    await db.upsert_message(legacy)
    await db.commit()
    # Mark "messages" as not-first-sync so we only exercise the backfill branch.
    await db.set_state("last_sync:messages", "2026-03-01T00:00:00")

    client = _make_mock_client()
    client.get_messages = AsyncMock(return_value=[])  # no new messages
    client.get_message_detail = AsyncMock(return_value="<p>Backfilled body.</p>")

    await sync_messages(client, db)

    client.get_message_detail.assert_awaited_with("legacy-key")
    cursor = await db.db.execute("SELECT content FROM messages WHERE id = 9001")
    row = await cursor.fetchone()
    assert row[0] == "<p>Backfilled body.</p>"


async def test_reauthentication_carries_changes_persisted_before_expiry(db: Database) -> None:
    import pytest

    from vulcan_notify.client import SessionExpiredError
    from vulcan_notify.sync import SyncSessionExpiredError

    await sync_all(_make_mock_client(), db)
    client = _make_mock_client(grades=[GRADE])
    client.get_attendance.side_effect = SessionExpiredError("expired")
    with pytest.raises(SyncSessionExpiredError) as failure:
        await sync_all(client, db)
    partial = failure.value.partial_result
    assert partial.notification_id == "sync:2"
    assert len(partial.student_results[0].new_grades) == 1
    assert len(await db.get_grades_for_student(STUDENT_A.key)) == 1
    retry = await sync_all(_make_mock_client(grades=[GRADE]), db)
    assert retry.notification_id == "sync:3"
    assert retry.student_results[0].new_grades == []


@pytest.mark.parametrize(
    "section,method,field",
    [
        ("grades", "get_grades_and_summaries", "new_grades"),
        ("attendance", "get_attendance", "new_attendance"),
        ("exams", "get_exams", "new_exams"),
        ("homework", "get_homework", "new_homework"),
        ("schedule", "get_schedule", "new_substitutions"),
    ],
)
async def test_failed_first_section_baselines_on_recovery(db, section, method, field):
    client = _make_mock_client()
    getattr(client, method).side_effect = VulcanFetchError("temporary failure")
    initial = await sync_all(client, db)
    assert section in initial.student_results[0].failed_sections
    assert await db.get_state(f"last_sync:KEYA:{section}") is None

    historical = {
        "grades": ([GRADE], []),
        "attendance": [AttendanceEntry(1, 2, "2026-03-16", "Math", "T", "", "")],
        "exams": [EXAM],
        "homework": [HOMEWORK],
        "schedule": [
            Lesson(
                "2026-03-16",
                "2026-03-16T08:00:00+01:00",
                "2026-03-16T08:45:00+01:00",
                "Math",
                "T",
                "",
                None,
                1,
                False,
                sub_teacher="Sub",
            )
        ],
    }[section]
    getattr(client, method).side_effect = None
    getattr(client, method).return_value = historical
    recovered = await sync_all(client, db)
    assert getattr(recovered.student_results[0], field) == []
    assert await db.get_state(f"last_sync:KEYA:{section}") is not None
    # Successful categories continue reporting while the failed one baselines.
    if section != "exams":
        client.get_exams.return_value = [EXAM]
    else:
        client.get_grades_and_summaries.return_value = ([GRADE], [])
    subsequent = await sync_all(client, db)
    assert subsequent.student_results[0].has_changes


async def test_standalone_failed_first_section_keeps_baseline_after_reopening(db):
    client = _make_mock_client()
    client.get_attendance.side_effect = VulcanFetchError("temporary failure")
    await sync_student(client, db, STUDENT_A)
    await db.close()
    await db.connect()
    client.get_attendance.side_effect = None
    client.get_attendance.return_value = [AttendanceEntry(1, 2, "2026-03-16", "Math", "T", "", "")]
    assert (await sync_student(client, db, STUDENT_A)).new_attendance == []


@pytest.mark.parametrize("tracked_failure", [False, True])
async def test_baseline_upgrade_preserves_legacy_success_and_tracked_failure(db, tracked_failure):
    await db.upsert_student(STUDENT_A)
    await db.set_state("last_sync:KEYA", "2026-03-01T00:00:00+00:00")
    if tracked_failure:
        run = await db.create_sync_run()
        await db.record_section(run, "grades", "failed", student_key="KEYA")
        await db.record_section(run, "exams", "ok", student_key="KEYA")
    await db.commit()
    result = await sync_student(_make_mock_client(grades=[GRADE], exams=[EXAM]), db, STUDENT_A)
    assert bool(result.new_grades) is not tracked_failure
    assert len(result.new_exams) == 1


async def test_successful_empty_baseline_notifies_later_records(db):
    await sync_all(_make_mock_client(), db)
    result = await sync_all(_make_mock_client(grades=[replace(GRADE, column_id=101)]), db)
    assert len(result.student_results[0].new_grades) == 1


async def test_failed_section_commit_does_not_initialize_baseline(db, monkeypatch):
    record = db.record_section

    async def fail_grades_commit(run_id, section, status, **kwargs):
        if section == "grades" and status == "ok":
            raise RuntimeError("fixture commit failure")
        await record(run_id, section, status, **kwargs)

    monkeypatch.setattr(db, "record_section", fail_grades_commit)
    result = await sync_all(_make_mock_client(grades=[GRADE]), db)
    assert "grades" in result.student_results[0].failed_sections
    assert await db.get_state("last_sync:KEYA:grades") is None
    assert await db.get_grades_for_student("KEYA") == []
    monkeypatch.setattr(db, "record_section", record)
    recovered = await sync_all(_make_mock_client(grades=[GRADE]), db)
    assert recovered.student_results[0].new_grades == []
