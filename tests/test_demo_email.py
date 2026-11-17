"""Synthetic sync uses real baselines, persistence and SMTP retry semantics."""

import sqlite3
from collections import Counter
from email import policy
from email.parser import BytesParser
from unittest.mock import MagicMock

import pytest

from vulcan_notify import demo_email, email
from vulcan_notify.config import Settings
from vulcan_notify.db import Database
from vulcan_notify.sync import sync_student


@pytest.fixture
def demo_config(monkeypatch, tmp_path):
    config = Settings(
        _env_file=None,
        db_path=tmp_path / "production.db",
        email_enabled=True,
        smtp_host="smtp.example.org",
        email_from="Test <demo@example.org>",
        email_to=["parent@example.org", "other@example.org"],
        email_ai_summary=False,
        llm_api_key=None,
    )
    monkeypatch.setattr(demo_email, "settings", config)
    monkeypatch.setattr(email, "settings", config)
    return config


async def test_baseline_then_eighteen_changes_and_unchanged_repeat(db):
    client = demo_email.DemoClient(seed=42)
    baseline = await sync_student(client, db, client.student)
    assert baseline.is_first_sync
    assert not baseline.has_changes
    assert not baseline.has_failures
    client.baseline = False
    result = await sync_student(client, db, client.student)
    assert not result.has_failures
    assert Counter(c.item_type for c in result.all_changes) == demo_email.EXPECTED_COUNTS
    assert len(await db.get_grades_for_student(client.student.key)) == 3
    assert len(await db.get_attendance_for_student(client.student.key)) == 3
    assert len(await db.get_exams_for_student(client.student.key)) == 3
    assert len(await db.get_homework_for_student(client.student.key)) == 3
    lessons = await db.get_lessons_for_student(client.student.key)
    assert len(lessons) == 6
    assert sum(bool(row["is_extra"]) for row in lessons) == 3
    assert sum(row["sub_teacher"] is not None for row in lessons) == 3
    assert {
        change.change_type
        for change in result.new_substitutions
        if change.item_type == "substitution"
    } == {"updated"}
    assert all(row["description"] for row in await db.get_exams_for_student(client.student.key))
    assert all(row["content"] for row in await db.get_homework_for_student(client.student.key))
    repeated = await sync_student(client, db, client.student)
    assert not repeated.has_changes
    assert not repeated.has_failures


async def test_preview_does_not_queue_or_send(tmp_path, monkeypatch, demo_config):
    sender = MagicMock()
    monkeypatch.setattr(email, "_send_email", sender)
    demo_config.email_enabled = False
    path = tmp_path / "demo.db"
    assert await demo_email.run_demo(path, seed=42) == 0
    body = path.with_suffix(".txt").read_text(encoding="utf-8")
    assert body.count("(3)") == 6
    html = path.with_suffix(".html").read_text(encoding="utf-8")
    assert html.count("<li ") == 18
    assert "Otwórz plan zajęć" in html
    assert "Uczeń TESTOWY" in body
    sender.assert_not_called()
    db = Database(path)
    await db.connect()
    try:
        assert await db.list_email_outbox() == []
        assert await db.get_state(demo_email.DEMO_MARKER) == "1"
    finally:
        await db.close()


async def test_filtered_preview_keeps_all_synchronized_entities(tmp_path, demo_config, capsys):
    demo_config.email_digest_groups = {"grade": False, "homework": False}
    path = tmp_path / "demo.db"
    assert await demo_email.run_demo(path, seed=42) == 0
    html = path.with_suffix(".html").read_text(encoding="utf-8")
    assert "Oceny" not in html and "Zadania domowe" not in html
    assert html.count("<li ") == 12
    output = capsys.readouterr().out
    assert "Zapisano 18 zmian" in output and "uwzględniono 12 zmian" in output
    db = Database(path)
    await db.connect()
    try:
        student = (await db.get_all_students())[0]
        assert len(await db.get_grades_for_student(student["key"])) == 3
        assert len(await db.get_homework_for_student(student["key"])) == 3
        assert await db.list_email_outbox() == []
    finally:
        await db.close()


async def test_demo_send_without_included_groups_never_queues_or_sends(
    tmp_path, monkeypatch, demo_config
):
    demo_config.email_digest_groups = dict.fromkeys(demo_email.EXPECTED_COUNTS, False)
    sender = MagicMock()
    monkeypatch.setattr(email, "_send_email", sender)
    path = tmp_path / "demo.db"
    with pytest.raises(ValueError, match="EMAIL_DIGEST_GROUPS"):
        await demo_email.run_demo(path, send=True, seed=42)
    sender.assert_not_called()
    db = Database(path)
    await db.connect()
    try:
        assert await db.list_email_outbox() == []
    finally:
        await db.close()


async def test_send_uses_outbox_and_saves_the_actual_message(tmp_path, monkeypatch, demo_config):
    sender = MagicMock()
    monkeypatch.setattr(email, "_send_email", sender)
    path = tmp_path / "demo.db"
    assert await demo_email.run_demo(path, send=True, seed=42) == 0
    assert sender.call_count == 2
    message = BytesParser(policy=policy.default).parsebytes(path.with_suffix(".eml").read_bytes())
    assert message["Subject"] == (
        f"{demo_config.email_subject_prefix} Oceny, Frekwencja, Zastępstwa, "
        "Dodatkowe zajęcia, Sprawdziany, Zadania domowe (sumarycznie 18 zmian)"
    )
    assert message.get_body(("plain",)).get_content() == path.with_suffix(".txt").read_text(
        encoding="utf-8"
    )
    assert message.get_body(("html",)).get_content() == path.with_suffix(".html").read_text(
        encoding="utf-8"
    )
    assert message["Message-ID"] == sender.call_args_list[0].args[0]["message_id"]
    db = Database(path)
    await db.connect()
    try:
        assert await db.list_email_outbox() == []
        assert len(await db.get_all_students()) == 1
    finally:
        await db.close()


async def test_failed_send_can_retry_without_new_data(tmp_path, monkeypatch, demo_config):
    sender = MagicMock(side_effect=ConnectionError("fixture SMTP outage"))
    monkeypatch.setattr(email, "_send_email", sender)
    path = tmp_path / "demo.db"
    assert await demo_email.run_demo(path, send=True, seed=42) == 1
    identities = [call.args[0]["message_id"] for call in sender.call_args_list]
    with pytest.raises(ValueError, match="--retry"):
        await demo_email.run_demo(path, send=True)
    sender.reset_mock()
    sender.side_effect = None
    assert await demo_email.run_demo(path, retry=True) == 0
    assert [call.args[0]["message_id"] for call in sender.call_args_list] == identities
    db = Database(path)
    await db.connect()
    try:
        assert await db.list_email_outbox() == []
        assert len(await db.get_all_students()) == 1
    finally:
        await db.close()


async def test_refuses_production_database_before_modification(tmp_path, demo_config):
    path = tmp_path / "existing.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE valuable_history (content TEXT)")
        connection.execute("INSERT INTO valuable_history VALUES ('fixture')")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="bazą demonstracyjną"):
        await demo_email.run_demo(path, send=True)
    assert path.read_bytes() == before
    assert not path.with_suffix(".txt").exists()
    with pytest.raises(ValueError, match="DB_PATH"):
        await demo_email.run_demo(demo_config.db_path)
    assert not demo_config.db_path.exists()


async def test_disabled_email_rejects_send_before_creating_database(tmp_path, demo_config):
    demo_config.email_enabled = False
    path = tmp_path / "demo.db"
    with pytest.raises(ValueError, match="EMAIL_ENABLED"):
        await demo_email.run_demo(path, send=True)
    assert not path.exists()
