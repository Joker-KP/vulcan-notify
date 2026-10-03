"""Integration tests for the sync pipeline."""

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from vulcan_notify.client import VulcanFetchError
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
from vulcan_notify.sync import sync_all, sync_messages, sync_student

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
