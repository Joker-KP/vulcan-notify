"""Completed lessons: recorded contract, durable sync and non-email outputs."""

import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from tests.test_sync import STUDENT_A, STUDENT_B, _make_mock_client
from vulcan_notify import api, email, mqtt
from vulcan_notify import sync as sync_module
from vulcan_notify.client import SessionExpiredError, VulcanClient, VulcanFetchError
from vulcan_notify.config import Settings
from vulcan_notify.db import Database
from vulcan_notify.differ import Change, diff_completed_lessons
from vulcan_notify.display import format_compact_sync, format_full_sync
from vulcan_notify.models import CompletedLesson
from vulcan_notify.sync import FullSyncResult, SyncResult, SyncSessionExpiredError, sync_all

LESSON = CompletedLesson(
    id=1,
    date="2026-10-02T08:00:00+02:00",
    lesson_number=1,
    subject="Math",
    teacher="Example Teacher",
    topic="<p>Fractions &amp; numbers</p>",
    thematic_block="Numbers",
    online="https://example.org/class",
    collections=[{"title": "Example collection", "items": [1]}],
    has_collections=True,
    resources={"example": ["Resource"]},
    url="https://uczen.eduvulcan.pl/example/App/KEYA/realizacjaZajec",
)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    clock = MagicMock(wraps=datetime)
    clock.now.return_value = datetime(2026, 10, 3, 12, tzinfo=sync_module.settings.timezone)
    monkeypatch.setattr(sync_module, "datetime", clock)


async def test_recorded_contract_and_encoded_request():
    recorded = json.loads(Path("tests/fixtures/eduvulcan/RealizacjaZajec13.json").read_text())
    client = VulcanClient({"base_url": "https://uczen.eduvulcan.pl/example", "cookies": []})
    client._request = AsyncMock(return_value=recorded["responses"][1])
    student = replace(STUDENT_A, key="key/with+reserved=")
    lessons = await client.get_completed_lessons(
        student, "2026-10-01T00:00:00Z", "2026-10-03T23:59:59Z"
    )
    assert len(lessons) == 1  # Identical duplicates in the sanitized fixture collapse.
    assert lessons[0].topic == "SYNTHETIC"
    assert lessons[0].resources is None and lessons[0].collections == []
    assert lessons[0].url.endswith("/App/key%2Fwith%2Breserved%3D/realizacjaZajec")
    client._request.assert_awaited_once_with(
        "/api/RealizacjaZajec13?key=key%2Fwith%2Breserved%3D"
        "&dataOd=2026-10-01T00%3A00%3A00Z&dataDo=2026-10-03T23%3A59%3A59Z&status=1"
    )
    populated = dict(
        recorded["responses"][1][0],
        kolekcjePoLekcji=LESSON.collections,
        existsKolekcjePoLekcji=True,
        zasoby=LESSON.resources,
        online=LESSON.online,
    )
    client._request.return_value = [populated]
    lesson = (await client.get_completed_lessons(STUDENT_A, "from", "to"))[0]
    assert lesson.collections == LESSON.collections and lesson.resources == LESSON.resources
    assert lesson.has_collections and lesson.online == LESSON.online
    client._request.return_value = []
    assert await client.get_completed_lessons(STUDENT_A, "from", "to") == []


@pytest.mark.parametrize("payload", [None, {}, [None], [{"id": 1}]])
async def test_invalid_payload_fails(payload):
    client = VulcanClient({"base_url": "https://uczen.eduvulcan.pl/example", "cookies": []})
    client._request = AsyncMock(return_value=payload)
    with pytest.raises(VulcanFetchError):
        await client.get_completed_lessons(STUDENT_A, "from", "to")


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", True),
        ("nrLekcji", 1.5),
        ("nrLekcji", float("inf")),
        ("data", "invalid"),
        ("tematOpis", None),
        ("kolekcjePoLekcji", {}),
        ("existsKolekcjePoLekcji", 1),
    ],
)
async def test_invalid_fields_fail(field, value):
    recorded = json.loads(Path("tests/fixtures/eduvulcan/RealizacjaZajec13.json").read_text())
    payload = dict(recorded["responses"][1][0], **{field: value})
    client = VulcanClient({"base_url": "https://uczen.eduvulcan.pl/example", "cookies": []})
    client._request = AsyncMock(return_value=[payload])
    with pytest.raises(VulcanFetchError):
        await client.get_completed_lessons(STUDENT_A, "from", "to")


async def test_conflicting_duplicate_ids_fail():
    recorded = json.loads(Path("tests/fixtures/eduvulcan/RealizacjaZajec13.json").read_text())
    item = recorded["responses"][1][0]
    client = VulcanClient({"base_url": "https://uczen.eduvulcan.pl/example", "cookies": []})
    client._request = AsyncMock(return_value=[item, dict(item, tematOpis="Different")])
    with pytest.raises(VulcanFetchError):
        await client.get_completed_lessons(STUDENT_A, "from", "to")


@pytest.mark.parametrize("upgrade", [False, True])
async def test_baseline_new_updated_and_idempotency(db, upgrade):
    if upgrade:
        await db.set_state(f"last_sync:{STUDENT_A.key}", "2026-10-01")
    client = _make_mock_client()
    client.get_completed_lessons.return_value = [LESSON]
    baseline = await sync_all(client, db)
    assert not baseline.has_failures
    assert baseline.student_results[0].is_first_completed_lessons_sync
    assert not baseline.student_results[0].completed_lesson_changes
    assert await db.get_state(f"last_sync:{STUDENT_A.key}:completed_lessons")
    assert await db.get_state(f"last_success:{STUDENT_A.key}:completed_lessons")
    start, end = client.get_completed_lessons.call_args.args[1:]
    assert start == "2026-07-04T22:00:00.000Z" and end == "2026-10-03T21:59:59.999Z"
    changed = replace(LESSON, topic="Updated topic")
    new = replace(LESSON, id=2)
    client.get_completed_lessons.return_value = [changed, new, new]
    result = await sync_all(client, db)
    sr = result.student_results[0]
    assert [change.change_type for change in sr.completed_lesson_changes] == ["updated", "new"]
    assert sr.all_changes == sr.completed_lesson_changes and sr.has_changes
    assert "Zajęcia zrealizowane" in format_full_sync(result)
    assert "2 completed lessons" in format_compact_sync(result)
    assert len(await db.get_completed_lessons_for_student(STUDENT_A.key)) == 2
    assert not (await sync_all(client, db)).student_results[0].completed_lesson_changes


async def test_failed_initial_fetch_and_persistence_baseline_silently_on_recovery(db, monkeypatch):
    client = _make_mock_client()
    client.get_completed_lessons.side_effect = VulcanFetchError("HTTP 500")
    failed = await sync_all(client, db)
    assert failed.has_failures and "completed_lessons" in failed.student_results[0].failed_sections
    assert await db.get_state(f"last_sync:{STUDENT_A.key}:completed_lessons") is None
    assert await db.get_state(f"last_success:{STUDENT_A.key}:completed_lessons") is None
    client.get_completed_lessons.side_effect = None
    client.get_completed_lessons.return_value = [LESSON]
    original = db.upsert_completed_lesson
    monkeypatch.setattr(db, "upsert_completed_lesson", AsyncMock(side_effect=OSError("disk")))
    assert (await sync_all(client, db)).has_failures
    assert await db.get_state(f"last_sync:{STUDENT_A.key}:completed_lessons") is None
    monkeypatch.setattr(db, "upsert_completed_lesson", original)
    recovered = await sync_all(client, db)
    assert recovered.student_results[0].is_first_completed_lessons_sync
    assert not recovered.student_results[0].completed_lesson_changes


async def test_scopes_window_soft_delete_restore_and_empty_baseline(db):
    client = _make_mock_client(students=[STUDENT_A, STUDENT_B])
    await sync_all(client, db)
    client.get_completed_lessons.side_effect = [[LESSON], [replace(LESSON, topic="Other student")]]
    result = await sync_all(client, db)
    assert [len(sr.completed_lesson_changes) for sr in result.student_results] == [1, 1]
    for id_, date in [(3, "2026-07-04T23:59:59+02:00"), (4, "2026-10-04T00:00:00+02:00")]:
        await db.upsert_completed_lesson(STUDENT_A.key, replace(LESSON, id=id_, date=date))
    client.get_completed_lessons.side_effect = [[], [replace(LESSON, topic="Other student")]]
    assert not (await sync_all(client, db)).student_results[0].completed_lesson_changes
    rows = {row["id"]: row for row in await db.get_completed_lessons_for_student(STUDENT_A.key)}
    assert rows[1]["deleted_at"]
    assert rows[3]["deleted_at"] is None and rows[4]["deleted_at"] is None
    assert (await db.get_completed_lessons_for_student(STUDENT_B.key))[0]["deleted_at"] is None
    client.get_completed_lessons.side_effect = [[LESSON], [replace(LESSON, topic="Other student")]]
    restored = await sync_all(client, db)
    assert not any(sr.completed_lesson_changes for sr in restored.student_results)
    restored_rows = await db.get_completed_lessons_for_student(STUDENT_A.key)
    assert next(row for row in restored_rows if row["id"] == LESSON.id)["deleted_at"] is None


async def test_partial_results_survive_session_expiry(db):
    client = _make_mock_client()
    await sync_all(client, db)
    client.get_completed_lessons.return_value = [LESSON]
    client.get_messages.side_effect = SessionExpiredError("expired")
    with pytest.raises(SyncSessionExpiredError) as error:
        await sync_all(client, db)
    assert len(error.value.partial_result.student_results[0].completed_lesson_changes) == 1
    client.get_messages.side_effect = None
    assert not (await sync_all(client, db)).student_results[0].completed_lesson_changes


async def test_failed_commit_rolls_back_rows_and_does_not_initialize_baseline(db, monkeypatch):
    client = _make_mock_client()
    client.get_completed_lessons.return_value = [LESSON]
    original = db.record_section

    async def fail_commit(run_id, section, status, **kwargs):
        if section == "completed_lessons" and status == "ok":
            raise OSError("commit failed")
        return await original(run_id, section, status, **kwargs)

    monkeypatch.setattr(db, "record_section", fail_commit)
    assert (await sync_all(client, db)).has_failures
    assert await db.get_completed_lessons_for_student(STUDENT_A.key) == []
    assert await db.get_state(f"last_sync:{STUDENT_A.key}:completed_lessons") is None
    monkeypatch.setattr(db, "record_section", original)
    recovered = await sync_all(client, db)
    assert not recovered.student_results[0].completed_lesson_changes


async def test_resource_edits_and_failed_fetch_preserve_previous_state(db):
    client = _make_mock_client()
    client.get_completed_lessons.return_value = [LESSON]
    await sync_all(client, db)
    edited = replace(LESSON, resources={"example": ["Changed resource"]})
    client.get_completed_lessons.return_value = [edited]
    result = await sync_all(client, db)
    assert result.student_results[0].completed_lesson_changes[0].change_type == "updated"
    before = await db.get_completed_lessons_for_student(STUDENT_A.key)
    stamp = await db.get_state(f"last_success:{STUDENT_A.key}:completed_lessons")
    client.get_completed_lessons.side_effect = VulcanFetchError("HTTP 500")
    assert (await sync_all(client, db)).has_failures
    assert await db.get_completed_lessons_for_student(STUDENT_A.key) == before
    assert await db.get_state(f"last_success:{STUDENT_A.key}:completed_lessons") == stamp


async def test_mqtt_structured_events_and_no_email_or_ai(db, monkeypatch):
    config = Settings(
        _env_file=None,
        email_enabled=True,
        smtp_host="smtp.example.org",
        email_from="school@example.org",
        email_to=["parent@example.org"],
        email_ai_summary=True,
        llm_api_key="test-key",
    )
    monkeypatch.setattr(email, "settings", config)
    monkeypatch.setattr(mqtt.settings, "mqtt_enabled", True)
    monkeypatch.setattr(mqtt.settings, "mqtt_topic_prefix", "school")
    changes = await diff_completed_lessons(STUDENT_A, [LESSON], db)
    result = FullSyncResult([SyncResult(STUDENT_A, completed_lesson_changes=changes)])
    ai = AsyncMock(return_value="Summary")
    sender = MagicMock()
    monkeypatch.setattr(email, "summarize", ai)
    monkeypatch.setattr(email, "_send_email", sender)
    await email.publish_email(result, db)
    assert await db.list_email_outbox() == []
    ai.assert_not_awaited()
    sender.assert_not_called()
    await mqtt.enqueue_changes(result, db)
    row = (await db.list_mqtt_outbox())[0]
    assert row["topic"] == "school/jan/completed_lessons/new"
    payload = json.loads(row["payload"])
    assert payload["topic"] == LESSON.topic and payload["teacher"] == LESSON.teacher
    assert payload["collections"] == LESSON.collections and payload["resources"] == LESSON.resources
    assert payload["has_collections"] is True and payload["id"] == LESSON.id
    result.student_results[0].new_grades = [Change("new", "grade", "Jan", "Math: 5", "Test")]
    await email.queue_summary(result, db)
    ai.assert_awaited_once()
    assert "Math: 5" in ai.call_args.args[0]
    assert (
        "Fractions" not in ai.call_args.args[0] and "Example collection" not in ai.call_args.args[0]
    )
    assert "Fractions" not in (await db.list_email_outbox())[0]["html_body"]
    assert (
        mqtt.topic_for(replace(changes[0], change_type="updated"))
        == "school/jan/completed_lessons/updated"
    )


async def test_api_identity_limits_json_deletions_and_freshness(db, monkeypatch):
    monkeypatch.setattr(api.settings, "db_path", db._db_path)
    for student in (STUDENT_A, STUDENT_B):
        await db.upsert_student(student)
        await db.upsert_completed_lesson(student.key, LESSON)
    await db.upsert_completed_lesson(STUDENT_A.key, replace(LESSON, id=2))
    await db.mark_missing_completed_lessons(STUDENT_A.key, {2}, "2026-10-01", "2026-10-04")
    await db.commit()
    result = api._get_completed_lessons(student_key=STUDENT_A.key)
    rows = result[STUDENT_A.key]["completed_lessons"]
    assert [row["id"] for row in rows] == [2]
    assert rows[0]["collections"] == LESSON.collections and rows[0]["resources"] == LESSON.resources
    assert result[STUDENT_A.key]["name"] == STUDENT_A.name
    assert len(api._get_completed_lessons()[STUDENT_B.name]["completed_lessons"]) == 1
    response = await api.handle_completed_lessons(
        make_mocked_request("GET", "/api/completed-lessons?student=Jan&n=1")
    )
    body = json.loads(response.text)
    assert set(body) == {"_meta", "Jan"}
    assert "completed_lessons" in body["_meta"]["sections"]
    assert "/api/completed-lessons" in {r.canonical for r in api.create_app().router.resources()}
    for n in ("0", "1001", "invalid"):
        with pytest.raises(web.HTTPBadRequest):
            await api.handle_completed_lessons(
                make_mocked_request("GET", f"/api/completed-lessons?n={n}")
            )


async def test_schema_upgrade_and_baseline_survive_reopening(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as old:
        old.execute(
            "CREATE TABLE sync_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "updated_at TIMESTAMP)"
        )
        old.execute("INSERT INTO sync_state (key,value) VALUES ('last_sync:KEYA','2026-10-01')")
    for attempt in range(2):
        database = Database(path)
        await database.connect()
        try:
            assert await database.get_state("last_sync:KEYA")
            client = _make_mock_client()
            client.get_completed_lessons.return_value = [LESSON]
            result = await sync_all(client, database)
            assert result.student_results[0].is_first_completed_lessons_sync == (attempt == 0)
            assert not result.student_results[0].completed_lesson_changes
            row = (await database.get_completed_lessons_for_student(STUDENT_A.key))[0]
            assert row["resources"] == LESSON.resources and row["collections"] == LESSON.collections
        finally:
            await database.close()
