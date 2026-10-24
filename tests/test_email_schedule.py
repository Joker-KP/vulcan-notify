"""Schedule email windows persist across syncs, SMTP failures and process restarts."""

import hashlib
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from vulcan_notify import email
from vulcan_notify.config import Settings
from vulcan_notify.db import Database
from vulcan_notify.differ import Change
from vulcan_notify.email_digest import FAILURE_NOTICE
from vulcan_notify.models import Lesson, Message, Remark, Student
from vulcan_notify.sync import FullSyncResult, SyncResult

STUDENT = Student("student-a", "Student A", "3A", "School", 1, "mailbox-a")
RECIPIENT = "parent@example.org"
RECIPIENT_KEY = hashlib.sha256(RECIPIENT.encode()).hexdigest()
PORTAL = "https://uczen.eduvulcan.pl/test/App/student-a"


@pytest.fixture
def delivery(monkeypatch):
    config = Settings(
        _env_file=None,
        email_enabled=True,
        smtp_host="smtp.example.org",
        email_from="school@example.org",
        email_to=[RECIPIENT],
        llm_api_key=None,
    )
    clock = SimpleNamespace(now=datetime(2026, 10, 24, 6, tzinfo=UTC))
    send = Mock()
    monkeypatch.setattr(email, "settings", config)
    monkeypatch.setattr(email, "_utc_now", lambda: clock.now)
    monkeypatch.setattr(email, "_send_email", send)
    return config, clock, send


def schedule_result(title="First substitution", kind="substitution", student=STUDENT, raw=None):
    return FullSyncResult(
        [
            SyncResult(
                student,
                new_substitutions=[Change("new", kind, student.name, title, "Details", raw=raw)],
                portal_url=PORTAL,
            )
        ]
    )


def bodies(send):
    return [call.args[0]["body"] for call in send.call_args_list]


async def test_first_email_then_fixed_window_and_flush_without_new_changes(db, delivery):
    _, clock, send = delivery
    start = clock.now
    first = schedule_result()
    await email.publish_email(first, db)
    assert send.call_count == 1
    assert "First substitution" in bodies(send)[0]
    assert await db.get_schedule_email_state(RECIPIENT_KEY) == (
        (start + timedelta(hours=6)).isoformat(),
        None,
    )

    clock.now = start + timedelta(hours=1)
    second = schedule_result("Second substitution")
    await email.publish_email(second, db)
    await email.publish_email(second, db)  # Same interrupted sync delivered twice.
    clock.now = start + timedelta(hours=5, minutes=59, seconds=59)
    await email.publish_email(schedule_result("Cancelled: PE", "cancellation"), db)
    assert send.call_count == 1
    assert len(await db.get_schedule_email_events(RECIPIENT_KEY)) == 2

    clock.now = start + timedelta(hours=6)
    await email.publish_email(FullSyncResult([]), db)
    assert send.call_count == 2
    assert "Second substitution" in bodies(send)[1] and "PE" in bodies(send)[1]
    assert "First substitution" not in bodies(send)[1]
    assert (
        "Zastępstwa, Anulowane zajęcia (sumarycznie 2 zmiany)" in send.call_args.args[0]["subject"]
    )
    assert await db.get_schedule_email_events(RECIPIENT_KEY) == []
    assert await db.get_schedule_email_state(RECIPIENT_KEY) == (
        (start + timedelta(hours=12)).isoformat(),
        None,
    )
    await email.publish_email(first, db)  # Sent events retain ingestion receipts, too.
    await email.publish_email(second, db)
    clock.now = start + timedelta(hours=12)
    await email.publish_email(FullSyncResult([]), db)
    assert send.call_count == 2  # No empty email and no rearming without delivery.
    await email.publish_email(schedule_result("After idle window"), db)
    assert send.call_count == 3


async def test_other_categories_and_individual_notifications_continue_during_silence(db, delivery):
    _, clock, send = delivery
    await email.publish_email(schedule_result(), db)
    clock.now += timedelta(hours=1)
    result = schedule_result("Deferred substitution")
    sr = result.student_results[0]
    for field, kind in (
        ("new_grades", "grade"),
        ("new_attendance", "attendance"),
        ("new_exams", "exam"),
        ("new_homework", "homework"),
        ("new_substitutions", "addition"),
    ):
        getattr(sr, field).append(Change("new", kind, STUDENT.name, f"Immediate {kind}", ""))
    sr.new_substitutions.append(Change("new", "cancellation", STUDENT.name, "Deferred PE", ""))
    sr.new_remarks = [
        Change(
            "new",
            "remark",
            STUDENT.name,
            "Note",
            "",
            raw=Remark(1, "2026-10-24", "Note", 2, "Teacher", "Individual note", 1),
        )
    ]
    result.new_messages = [
        Message(1, "msg-a", "Teacher", "Trip", "2026-10-24", "Inbox", False, False)
    ]
    before = asdict(result)
    await email.publish_email(result, db)
    assert asdict(result) == before  # MQTT/display/calendar receive the original changes.
    assert send.call_count == 4  # First email, message, remark, ordinary digest.
    digest = bodies(send)[-1]
    for kind in ("grade", "attendance", "exam", "homework", "addition"):
        assert f"Immediate {kind}" in digest
    assert "Deferred substitution" not in digest and "Deferred PE" not in digest
    assert "Zastępstwa" not in send.call_args.args[0]["subject"]
    assert len(await db.get_schedule_email_events(RECIPIENT_KEY)) == 2


async def test_first_mixed_digest_stays_combined_and_due_sync_includes_current_changes(
    db, delivery
):
    _, clock, send = delivery
    result = schedule_result()
    result.student_results[0].new_grades = [Change("new", "grade", STUDENT.name, "Grade 5", "")]
    await email.publish_email(result, db)
    assert send.call_count == 1
    assert "Grade 5" in bodies(send)[0] and "First substitution" in bodies(send)[0]
    clock.now += timedelta(hours=1)
    await email.publish_email(schedule_result("Buffered"), db)
    clock.now += timedelta(hours=5)
    await email.publish_email(schedule_result("At boundary", "cancellation"), db)
    assert send.call_count == 2
    assert "Buffered" in bodies(send)[1] and "At boundary" in bodies(send)[1]


async def test_smtp_failure_reserves_one_email_and_starts_window_on_actual_acceptance(db, delivery):
    _, clock, send = delivery
    start = clock.now
    send.side_effect = TimeoutError()
    first = schedule_result()
    await email.publish_email(first, db)
    queued = (await db.list_email_outbox())[0]
    quiet, pending = await db.get_schedule_email_state(RECIPIENT_KEY)
    assert quiet is None and pending == queued["delivery_key"]
    clock.now += timedelta(hours=8)
    await email.publish_email(schedule_result("During SMTP outage"), db)
    assert len(await db.list_email_outbox()) == 1
    assert len(await db.get_schedule_email_events(RECIPIENT_KEY)) == 1
    send.side_effect = None
    assert await email.drain_email_outbox(db) == (1, 0)  # email-retry does not flush the buffer.
    assert send.call_args.args[0]["message_id"] == queued["message_id"]
    assert send.call_args.args[0]["body"] == queued["body"]
    assert await db.get_schedule_email_state(RECIPIENT_KEY) == (
        (start + timedelta(hours=14)).isoformat(),
        None,
    )
    clock.now += timedelta(hours=6)
    await email.publish_email(FullSyncResult([]), db)
    assert "During SMTP outage" in bodies(send)[-1]


async def test_recipient_windows_are_independent_and_students_share_one_window(db, delivery):
    config, clock, send = delivery
    other_recipient = "second@example.org"
    config.email_to = [RECIPIENT, other_recipient, RECIPIENT]

    def fail_second(row, settings):
        if row["recipient"] == other_recipient:
            raise TimeoutError()

    send.side_effect = fail_second
    await email.publish_email(schedule_result(), db)
    clock.now += timedelta(hours=1)
    other_student = replace(STUDENT, key="student-b", name=STUDENT.name)
    await email.publish_email(schedule_result("Second student change", student=other_student), db)
    await email.publish_email(schedule_result("First student cancellation", "cancellation"), db)
    assert len(await db.list_email_outbox()) == 1
    send.side_effect = None
    await email.drain_email_outbox(db)
    second_key = hashlib.sha256(other_recipient.encode()).hexdigest()
    assert (await db.get_schedule_email_state(second_key))[0] != (
        await db.get_schedule_email_state(RECIPIENT_KEY)
    )[0]
    clock.now += timedelta(hours=5)
    send.reset_mock()
    await email.publish_email(FullSyncResult([]), db)
    assert send.call_count == 1 and send.call_args.args[0]["recipient"] == RECIPIENT
    assert "Second student change" in bodies(send)[0]
    assert "First student cancellation" in bodies(send)[0]
    clock.now += timedelta(hours=1)
    await email.publish_email(FullSyncResult([]), db)
    assert send.call_count == 2 and send.call_args.args[0]["recipient"] == other_recipient


async def test_restart_retains_snapshots_and_flushes_after_night_even_without_student_result(
    tmp_path, delivery
):
    _, clock, send = delivery
    clock.now = datetime(2026, 10, 24, 18, tzinfo=UTC)  # Window ends during the night.
    path = tmp_path / "persistent.db"
    database = Database(path)
    await database.connect()
    try:
        await email.publish_email(schedule_result(), database)
        clock.now += timedelta(hours=1)
        lesson = Lesson(
            "2026-10-26",
            "2026-10-26T08:00:00",
            "2026-10-26T08:45:00",
            "Math",
            "New teacher",
            "12",
            None,
            1,
            False,
            sub_teacher="Old teacher",
        )
        await email.publish_email(schedule_result(raw=lesson), database)
        await email.publish_email(schedule_result("Cancelled: PE", "cancellation"), database)
    finally:
        await database.close()
    clock.now = datetime(2026, 10, 25, 6, tzinfo=UTC)  # Next morning, across DST fallback.
    database = Database(path)
    await database.connect()
    try:
        await email.publish_email(FullSyncResult([], message_failure="FetchError"), database)
        assert send.call_count == 2
        assert "Math" in bodies(send)[-1] and "Old teacher → New teacher" in bodies(send)[-1]
        assert "PE" in bodies(send)[-1] and PORTAL + "/planZajec" in bodies(send)[-1]
        assert FAILURE_NOTICE in bodies(send)[-1]
        assert await database.get_schedule_email_events(RECIPIENT_KEY) == []
        cursor = await database.db.execute("SELECT payload FROM email_schedule_events")
        assert all(row[0] is None for row in await cursor.fetchall())
    finally:
        await database.close()


async def test_preparation_failure_rolls_back_outbox_and_keeps_committed_buffer(
    db, delivery, monkeypatch
):
    _, clock, send = delivery
    await email.publish_email(schedule_result(), db)
    clock.now += timedelta(hours=1)
    await email.publish_email(schedule_result("Buffered"), db)
    clock.now += timedelta(hours=5)
    with monkeypatch.context() as patch:
        patch.setattr(db, "consume_schedule_email_events", AsyncMock(side_effect=RuntimeError()))
        with pytest.raises(RuntimeError):
            await email.queue_summary(FullSyncResult([]), db)
    await db.commit()  # Even a later unrelated commit cannot persist a partial transfer.
    assert await db.list_email_outbox() == []
    assert (await db.get_schedule_email_state(RECIPIENT_KEY))[1] is None
    assert len(await db.get_schedule_email_events(RECIPIENT_KEY)) == 1
    await email.publish_email(FullSyncResult([]), db)
    assert send.call_count == 2 and "Buffered" in bodies(send)[-1]


async def test_zero_disables_wait_and_releases_existing_buffer(db, delivery):
    config, clock, send = delivery
    await email.publish_email(schedule_result(), db)
    clock.now += timedelta(hours=1)
    await email.publish_email(schedule_result("Buffered"), db)
    config.email_schedule_cooldown_hours = 0
    await email.publish_email(FullSyncResult([]), db)
    await email.publish_email(schedule_result("Immediate again"), db)
    assert send.call_count == 3
    assert "Buffered" in bodies(send)[1] and "Immediate again" in bodies(send)[2]


async def test_baselines_disabled_email_and_disabled_groups_do_not_start_windows(db, delivery):
    config, _, send = delivery
    baseline = schedule_result()
    baseline.student_results[0].is_first_sync = True
    await email.publish_email(baseline, db)
    config.email_enabled = False
    await email.publish_email(schedule_result(), db)
    config.email_enabled = True
    config.email_digest_groups = {"substitution": False, "cancellation": False}
    await email.publish_email(schedule_result(), db)
    assert send.call_count == 0
    assert await db.get_schedule_email_events(RECIPIENT_KEY) == []
    assert await db.get_schedule_email_state(RECIPIENT_KEY) == (None, None)


async def test_disabling_buffered_group_drops_it_without_email_or_ai(db, delivery, monkeypatch):
    config, clock, send = delivery
    await email.publish_email(schedule_result(), db)
    clock.now += timedelta(hours=1)
    await email.publish_email(schedule_result("Later excluded"), db)
    config.email_digest_groups = {"substitution": False}
    config.email_ai_summary = True
    config.llm_api_key = "synthetic-key"
    ai = AsyncMock()
    monkeypatch.setattr(email, "summarize", ai)
    clock.now += timedelta(hours=5)
    await email.publish_email(FullSyncResult([]), db)
    assert send.call_count == 1
    ai.assert_not_awaited()
    assert await db.get_schedule_email_events(RECIPIENT_KEY) == []


async def test_ai_sees_only_sent_groups_and_retry_reuses_prepared_batch(db, delivery, monkeypatch):
    config, clock, send = delivery
    config.email_to.append("second@example.org")
    config.email_ai_summary = True
    config.llm_api_key = "synthetic-key"
    ai = AsyncMock(return_value="AI summary")
    monkeypatch.setattr(email, "summarize", ai)
    await email.publish_email(schedule_result(), db)
    assert ai.await_count == 1  # Identical recipient batches share AI.
    clock.now += timedelta(hours=1)
    interim = schedule_result("Deferred")
    interim.student_results[0].new_grades = [
        Change("new", "grade", STUDENT.name, "Immediate grade", "")
    ]
    await email.publish_email(interim, db)
    assert ai.await_count == 2
    assert "Immediate grade" in ai.call_args.args[0] and "Deferred" not in ai.call_args.args[0]
    clock.now += timedelta(hours=5)
    send.side_effect = TimeoutError()
    await email.publish_email(FullSyncResult([]), db)
    assert ai.await_count == 3 and "Deferred" in ai.call_args.args[0]
    assert "Immediate grade" not in ai.call_args.args[0]
    send.side_effect = None
    await email.drain_email_outbox(db)
    assert ai.await_count == 3
    assert all(
        "AI summary" in row["body"] for row in [call.args[0] for call in send.call_args_list]
    )


async def test_repeated_mixed_partial_result_does_not_repeat_ordinary_changes_at_flush(
    db, delivery
):
    _, clock, send = delivery
    await email.publish_email(schedule_result(), db)
    clock.now += timedelta(hours=1)
    result = schedule_result("Deferred")
    result.student_results[0].new_grades = [Change("new", "grade", STUDENT.name, "Grade 5", "")]
    await email.publish_email(result, db)
    assert "Grade 5" in bodies(send)[-1] and "Deferred" not in bodies(send)[-1]
    clock.now += timedelta(hours=5)
    await email.publish_email(result, db)
    assert send.call_count == 3
    assert "Deferred" in bodies(send)[-1] and "Grade 5" not in bodies(send)[-1]
