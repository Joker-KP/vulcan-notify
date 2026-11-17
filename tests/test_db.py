"""Tests for the database schema and operations."""

from vulcan_notify.db import Database
from vulcan_notify.models import AttendanceEntry, Exam, Grade, Homework, Student


async def test_schema_creation(db: Database) -> None:
    """Verify all tables are created."""
    cursor = await db.db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in await cursor.fetchall()]
    assert "students" in tables
    assert "grades" in tables
    assert "attendance" in tables
    assert "exams" in tables
    assert "homework" in tables
    assert "messages" in tables
    assert "sync_state" in tables


async def test_upsert_student(db: Database) -> None:
    student = Student(
        key="KEY1",
        name="Jan",
        class_name="3A",
        school="Szkola",
        diary_id=1001,
        mailbox_key="aaa",
    )
    await db.upsert_student(student)
    await db.upsert_student(student)  # idempotent

    cursor = await db.db.execute("SELECT COUNT(*) FROM students")
    count = (await cursor.fetchone())[0]
    assert count == 1


async def test_upsert_student_updates_name(db: Database) -> None:
    student = Student(
        key="KEY1",
        name="Jan",
        class_name="3A",
        school="Szkola",
        diary_id=1001,
        mailbox_key="aaa",
    )
    await db.upsert_student(student)

    student.name = "Jan Updated"
    await db.upsert_student(student)

    cursor = await db.db.execute("SELECT name FROM students WHERE key = 'KEY1'")
    row = await cursor.fetchone()
    assert row[0] == "Jan Updated"


async def test_upsert_grade_idempotent(db: Database) -> None:
    student = Student(
        key="KEY1",
        name="Jan",
        class_name="3A",
        school="Szkola",
        diary_id=1001,
        mailbox_key="aaa",
    )
    await db.upsert_student(student)

    grade = Grade(
        column_id=100,
        value="5",
        date="15.03.2026",
        subject="Math",
        column_name="Test 1",
        category="Biezace",
        weight=1,
        teacher="Nowak",
        changed_since_login=False,
    )
    await db.upsert_grade("KEY1", grade)
    await db.upsert_grade("KEY1", grade)

    cursor = await db.db.execute("SELECT COUNT(*) FROM grades")
    count = (await cursor.fetchone())[0]
    assert count == 1


async def test_upsert_grade_updates_value(db: Database) -> None:
    student = Student(
        key="KEY1",
        name="Jan",
        class_name="3A",
        school="Szkola",
        diary_id=1001,
        mailbox_key="aaa",
    )
    await db.upsert_student(student)

    grade = Grade(
        column_id=100,
        value="4",
        date="15.03.2026",
        subject="Math",
        column_name="Test 1",
        category="Biezace",
        weight=1,
        teacher="Nowak",
        changed_since_login=False,
    )
    await db.upsert_grade("KEY1", grade)

    grade.value = "5"
    await db.upsert_grade("KEY1", grade)

    rows = await db.get_grades_for_student("KEY1")
    assert len(rows) == 1
    assert rows[0]["value"] == "5"


async def test_get_grades_for_student(db: Database) -> None:
    student = Student(
        key="KEY1",
        name="Jan",
        class_name="3A",
        school="Szkola",
        diary_id=1001,
        mailbox_key="aaa",
    )
    await db.upsert_student(student)

    for i in range(3):
        grade = Grade(
            column_id=100 + i,
            value=str(3 + i),
            date="15.03.2026",
            subject="Math",
            column_name=f"Test {i}",
            category="Biezace",
            weight=1,
            teacher="Nowak",
            changed_since_login=False,
        )
        await db.upsert_grade("KEY1", grade)

    rows = await db.get_grades_for_student("KEY1")
    assert len(rows) == 3

    # Different student has no grades
    rows2 = await db.get_grades_for_student("KEY2")
    assert len(rows2) == 0


async def test_upsert_attendance(db: Database) -> None:
    student = Student(
        key="KEY1",
        name="Jan",
        class_name="3A",
        school="Szkola",
        diary_id=1001,
        mailbox_key="aaa",
    )
    await db.upsert_student(student)

    entry = AttendanceEntry(
        lesson_number=3,
        category=2,
        date="2026-03-14",
        subject="Math",
        teacher="Nowak",
        time_from="09:50",
        time_to="10:35",
    )
    await db.upsert_attendance("KEY1", entry)
    await db.upsert_attendance("KEY1", entry)  # idempotent

    rows = await db.get_attendance_for_student("KEY1")
    assert len(rows) == 1
    assert rows[0]["category"] == 2


async def test_upsert_exam(db: Database) -> None:
    student = Student(
        key="KEY1",
        name="Jan",
        class_name="3A",
        school="Szkola",
        diary_id=1001,
        mailbox_key="aaa",
    )
    await db.upsert_student(student)

    exam = Exam(id=10001, date="2026-03-16", subject="Przyroda", type=2)
    await db.upsert_exam("KEY1", exam)
    await db.upsert_exam("KEY1", exam)  # idempotent

    cursor = await db.db.execute("SELECT COUNT(*) FROM exams")
    count = (await cursor.fetchone())[0]
    assert count == 1


async def test_upsert_homework(db: Database) -> None:
    student = Student(
        key="KEY1",
        name="Jan",
        class_name="3A",
        school="Szkola",
        diary_id=1001,
        mailbox_key="aaa",
    )
    await db.upsert_student(student)

    hw = Homework(id=10002, date="2026-03-16", subject="Plastyka")
    await db.upsert_homework("KEY1", hw)
    await db.upsert_homework("KEY1", hw)  # idempotent

    cursor = await db.db.execute("SELECT COUNT(*) FROM homework")
    count = (await cursor.fetchone())[0]
    assert count == 1


async def test_homework_soft_deletion_is_window_scoped_and_accepts_empty_results(db):
    await db.upsert_student(_student("A"))
    await db.upsert_student(_student("B"))
    for task in (
        Homework(1, "2026-10-08T21:59:59Z", "Before"),
        Homework(2, "2026-10-08T22:00:00Z", "Start"),
        Homework(3, "2026-10-09T21:59:59.999Z", "End"),
        Homework(4, "2026-10-09T22:00:00Z", "After"),
    ):
        await db.upsert_homework("A", task)
    await db.upsert_homework("B", Homework(5, "2026-10-09", "Other student"))
    bounds = ("2026-10-08T22:00:00Z", "2026-10-09T21:59:59.999Z")
    assert await db.mark_missing_homework_in_window("A", {3}, *bounds) == 1
    assert {row["id"] for row in await db.get_homework_for_student("A")} == {1, 3, 4}
    assert await db.mark_missing_homework_in_window("A", set(), *bounds) == 1
    assert {row["id"] for row in await db.get_homework_for_student("A")} == {1, 4}
    assert len(await db.get_homework_for_student("B")) == 1
    # A restored known ID remains known to the differ and does not re-notify.
    await db.upsert_homework("A", Homework(2, "2026-10-09", "Restored"))
    assert await db.get_homework_ids_for_student("A") == {1, 2, 3, 4}


async def test_sync_state(db: Database) -> None:
    assert await db.get_state("last_sync") is None

    await db.set_state("last_sync", "2026-03-15T12:00:00")
    assert await db.get_state("last_sync") == "2026-03-15T12:00:00"

    await db.set_state("last_sync", "2026-03-15T13:00:00")
    assert await db.get_state("last_sync") == "2026-03-15T13:00:00"


def _student(key: str, name: str = "Solomiia", class_name: str = "4E") -> Student:
    return Student(
        key=key,
        name=name,
        class_name=class_name,
        school="Szkola",
        diary_id=1001,
        mailbox_key="aaa",
    )


async def _active_map(db: Database) -> dict[str, int]:
    cursor = await db.db.execute("SELECT key, active FROM students")
    return {row[0]: row[1] for row in await cursor.fetchall()}


async def test_new_student_is_active_by_default(db: Database) -> None:
    await db.upsert_student(_student("KEY1"))
    assert await _active_map(db) == {"KEY1": 1}


async def test_deactivate_students_except_retires_previous_year(db: Database) -> None:
    """A September rollover leaves last year's key behind; it should go inactive."""
    await db.upsert_student(_student("OLD", class_name="4E"))
    await db.upsert_student(_student("NEW", class_name="5E"))

    retired = await db.deactivate_students_except({"NEW"})

    assert retired == 1
    assert await _active_map(db) == {"OLD": 0, "NEW": 1}


async def test_deactivate_students_except_is_idempotent(db: Database) -> None:
    await db.upsert_student(_student("OLD"))
    await db.upsert_student(_student("NEW"))

    assert await db.deactivate_students_except({"NEW"}) == 1
    # Second run has nothing left to retire.
    assert await db.deactivate_students_except({"NEW"}) == 0


async def test_deactivate_students_except_ignores_empty_set(db: Database) -> None:
    """A failed roster fetch must not deactivate the whole table."""
    await db.upsert_student(_student("KEY1"))
    await db.upsert_student(_student("KEY2"))

    retired = await db.deactivate_students_except(set())

    assert retired == 0
    assert await _active_map(db) == {"KEY1": 1, "KEY2": 1}


async def test_upsert_student_reactivates_retired_key(db: Database) -> None:
    await db.upsert_student(_student("KEY1"))
    await db.deactivate_students_except({"OTHER"})
    assert (await _active_map(db))["KEY1"] == 0

    await db.upsert_student(_student("KEY1"))

    assert (await _active_map(db))["KEY1"] == 1


async def test_deactivating_preserves_history(db: Database) -> None:
    """Retiring a key must not touch the grades hanging off it."""
    await db.upsert_student(_student("OLD"))
    await db.upsert_grade(
        "OLD",
        Grade(
            column_id=100,
            value="5",
            date="10.05.2026",
            subject="Matematyka",
            column_name="Sprawdzian",
            category="Biezace",
            weight=3.0,
            teacher="Kowalska",
            changed_since_login=False,
        ),
    )

    await db.deactivate_students_except({"NEW"})

    cursor = await db.db.execute("SELECT COUNT(*) FROM grades WHERE student_key = 'OLD'")
    assert (await cursor.fetchone())[0] == 1


async def test_migration_adds_active_column(tmp_path) -> None:
    """An existing DB predating the active flag gets it, defaulting to active."""
    import aiosqlite

    db_path = tmp_path / "preactive.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "CREATE TABLE students ("
            "key TEXT PRIMARY KEY, name TEXT NOT NULL, class_name TEXT NOT NULL, "
            "school TEXT NOT NULL, diary_id INTEGER NOT NULL, mailbox_key TEXT, "
            "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        await conn.execute(
            "INSERT INTO students (key, name, class_name, school, diary_id) "
            "VALUES ('OLD', 'Solomiia', '4E', 'Szkola', 1001)"
        )
        await conn.commit()

    db = Database(db_path)
    await db.connect()

    cursor = await db.db.execute("PRAGMA table_info(students)")
    columns = {row[1] for row in await cursor.fetchall()}
    assert "active" in columns

    cursor = await db.db.execute("SELECT active FROM students WHERE key = 'OLD'")
    assert (await cursor.fetchone())[0] == 1

    await db.close()


async def test_homework_detail_migration_preserves_history_and_calendar_uid(tmp_path) -> None:
    import aiosqlite

    from vulcan_notify.db import SCHEMA

    old_schema = SCHEMA.replace(
        "    links TEXT,\n    response_required INTEGER,\n"
        "    response_deadline TEXT,\n    detail_fetched_at TIMESTAMP,\n    details TEXT,\n",
        "",
    )
    path = tmp_path / "old-homework.db"
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(old_schema)
        await conn.execute(
            "INSERT INTO students (key, name, class_name, school, diary_id) "
            "VALUES ('OLD', 'Test Student', '3A', 'Test School', 1)"
        )
        await conn.execute(
            "INSERT INTO homework (id, student_key, date, subject, content, teacher, "
            "deleted_at, calendar_uid) VALUES "
            "(1, 'OLD', '2026-10-01', 'Math', 'Stored description', 'Test Teacher', "
            "'2026-10-02', 'calendar-uid')"
        )
        await conn.commit()
    database = Database(path)
    await database.connect()
    cursor = await database.db.execute(
        "SELECT content, teacher, deleted_at, calendar_uid, links, response_required, "
        "response_deadline, detail_fetched_at FROM homework WHERE id = 1"
    )
    assert await cursor.fetchone() == (
        "Stored description",
        "Test Teacher",
        "2026-10-02",
        "calendar-uid",
        None,
        None,
        None,
        None,
    )
    # Reopening is idempotent. Old records are eligible for silent detail backfill.
    await database.close()
    await database.connect()
    assert await database.get_homework_missing_detail("OLD") == {1}
    await database.upsert_homework(
        "OLD",
        Homework(
            1,
            "2026-10-01",
            "Math",
            content="",
            links=[],
            response_required=False,
            details={"opis": ""},
        ),
        detail_fetched=True,
    )
    row = (await database.get_homework_for_student("OLD"))[0]
    assert row["content"] == ""
    assert row["teacher"] is None
    assert row["links"] == []
    assert row["response_required"] is False
    assert await database.get_homework_missing_detail("OLD") == set()
    cursor = await database.db.execute("SELECT calendar_uid FROM homework WHERE id = 1")
    assert (await cursor.fetchone())[0] == "calendar-uid"
    await database.close()


async def test_migration_drops_legacy_tables(tmp_path) -> None:
    """Verify old schema tables are dropped during migration."""
    import aiosqlite

    db_path = tmp_path / "legacy.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("CREATE TABLE seen_items (item_type TEXT, item_id TEXT, item_hash TEXT)")
        await conn.execute("CREATE TABLE poll_state (key TEXT, value TEXT)")
        await conn.commit()

    db = Database(db_path)
    await db.connect()

    cursor = await db.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('seen_items', 'poll_state')"
    )
    legacy = await cursor.fetchall()
    assert len(legacy) == 0

    # New tables should exist
    cursor = await db.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='students'"
    )
    assert await cursor.fetchone() is not None

    await db.close()


async def test_attendance_migration_preserves_history_and_distinguishes_activities(tmp_path):
    import sqlite3
    from dataclasses import replace

    from vulcan_notify.db import SCHEMA

    old_schema = SCHEMA.replace(
        "    source TEXT NOT NULL DEFAULT 'oddzialy',\n    entry_key TEXT NOT NULL DEFAULT '',\n",
        "",
    ).replace(
        "PRIMARY KEY (student_key, date, lesson_number, source, entry_key)",
        "PRIMARY KEY (student_key, date, lesson_number)",
    )
    path = tmp_path / "old-attendance.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(old_schema)
        conn.execute(
            "INSERT INTO attendance (student_key, date, lesson_number, category, "
            "subject, first_seen) VALUES ('A', '2026-10-09', 0, 2, 'Math', '2026-10-09 "
            "08:00:00')"
        )
    database = Database(path)
    await database.connect()
    try:
        original = (await database.get_attendance_for_student("A"))[0]
        assert original["source"] == "oddzialy" and original["entry_key"] == ""
        cursor = await database.db.execute("SELECT first_seen FROM attendance")
        assert (await cursor.fetchone())[0] == "2026-10-09 08:00:00"
        extra = AttendanceEntry(
            0, 2, "2026-10-09", "Math", "Teacher", "08:00", "09:00", "dziennikiZajecInnych"
        )
        for entry in (
            extra,
            replace(extra, time_from="10:00"),
            replace(extra, source="dziennikiSwietlicy"),
        ):
            await database.upsert_attendance("A", entry)
            await database.upsert_attendance("A", entry)
        await database.commit()
        await database.close()
        await database.connect()
        assert len(await database.get_attendance_for_student("A")) == 4
        assert await database.get_attendance_for_student("B") == []
    finally:
        await database.close()


async def test_exam_soft_deletion_uses_only_successful_window_including_empty_list(db):
    await db.upsert_student(_student("A"))
    await db.upsert_student(_student("B"))
    for exam in (
        Exam(1, "2026-09-01", "Math", 1),
        Exam(2, "2026-10-09", "Math", 2),
        Exam(3, "2026-12-01", "Math", 1),
    ):
        await db.upsert_exam("A", exam)
    await db.upsert_exam("B", Exam(4, "2026-10-09", "Math", 1))
    assert await db.mark_missing_exams_in_window("A", set(), "2026-10-01", "2026-10-31") == 1
    assert {row["id"] for row in await db.get_exams_for_student("A")} == {1, 3}
    assert len(await db.get_exams_for_student("B")) == 1
    assert await db.get_exam_ids_for_student("A") == {1, 2, 3}


async def test_exam_detail_migration_preserves_history_and_calendar_uid(tmp_path):
    import sqlite3

    from vulcan_notify.db import SCHEMA

    path = tmp_path / "old-exams.db"
    old_schema = SCHEMA.replace("    details TEXT,\n    detail_fetched_at TIMESTAMP,\n", "")
    with sqlite3.connect(path) as conn:
        conn.executescript(old_schema)
        conn.execute(
            "INSERT INTO exams (id, student_key, date, subject, type, description, "
            "teacher, calendar_uid, deleted_at) VALUES (1, 'A', '2026-10-09', 'Math', 2, "
            "'Old detail', 'Teacher', 'uid', '2026-10-10')"
        )
    database = Database(path)
    await database.connect()
    try:
        cursor = await database.db.execute(
            "SELECT description, teacher, calendar_uid, deleted_at, details, "
            "detail_fetched_at FROM exams"
        )
        assert await cursor.fetchone() == ("Old detail", "Teacher", "uid", "2026-10-10", None, None)
        detail = {"opis": "", "nauczycielImieNazwisko": "Teacher"}
        await database.upsert_exam(
            "A", Exam(1, "2026-10-09", "Math", 2, "", "Teacher", detail), detail_fetched=True
        )
        await database.commit()
        await database.close()
        await database.connect()
        row = (await database.get_exams_for_student("A"))[0]
        assert row["description"] == "" and row["teacher"] == "Teacher" and row["details"] == detail
        cursor = await database.db.execute("SELECT calendar_uid FROM exams")
        assert (await cursor.fetchone())[0] == "uid"
    finally:
        await database.close()
