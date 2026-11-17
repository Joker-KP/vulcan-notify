"""Praise/notes: parsing, category baseline, student isolation and delivery."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request
from pydantic import ValidationError

from tests.test_sync import STUDENT_A, STUDENT_B, _make_mock_client
from vulcan_notify import api, email, mqtt
from vulcan_notify.client import VulcanClient, VulcanFetchError
from vulcan_notify.config import Settings
from vulcan_notify.db import Database
from vulcan_notify.differ import Change, diff_remarks
from vulcan_notify.display import format_compact_sync, format_full_sync
from vulcan_notify.models import Remark
from vulcan_notify.sync import FullSyncResult, SyncResult, SyncSessionExpiredError, sync_all

REMARK = Remark(
    1,
    "2026-10-02T08:00:00Z",
    "Pochwała",
    1,
    "Test Teacher",
    "<p>Pomoc w zajęciach</p>",
    1,
    2,
    "https://uczen.eduvulcan.pl/example/App/KEYA/pochwalyUwagi",
)
NOTE = replace(REMARK, id=2, category="Uwaga", type=2, content="<p>Brak pracy</p>")


@pytest.mark.parametrize(
    "base", ["https://uczen.eduvulcan.pl/first", "https://uczen.eduvulcan.pl/second/"]
)
async def test_parse_recorded_contract_and_profile_link(base):
    recorded = json.loads(Path("tests/fixtures/eduvulcan/Uwagi.json").read_text())
    client = VulcanClient({"base_url": base, "cookies": []})
    payload = recorded["responses"][0] + [
        {
            "id": 2,
            "data": NOTE.date,
            "kategoria": NOTE.category,
            "typ": 2,
            "autor": NOTE.author,
            "tresc": NOTE.content,
            "rodzaj": 2,
            "liczbaPunktow": -3,
        }
    ]
    client._request = AsyncMock(return_value=payload)
    student = replace(STUDENT_A, key="key/with+reserved=")
    items = await client.get_remarks(student)
    assert len(items) == 2
    assert items[0].points is None
    assert items[1].points == -3
    assert items[1].content == NOTE.content
    assert (items[1].type, items[1].kind) == (2, 2)
    assert items[0].url == f"{base.rstrip('/')}/App/key%2Fwith%2Breserved%3D/pochwalyUwagi"
    client._request.assert_awaited_once_with("/api/Uwagi?key=key%2Fwith%2Breserved%3D")


@pytest.mark.parametrize("payload", [None, {}, {"items": []}, [None], [{"id": 1}]])
async def test_invalid_response_is_a_failure(payload):
    client = VulcanClient({"base_url": "https://uczen.eduvulcan.pl/example", "cookies": []})
    client._request = AsyncMock(return_value=payload)
    with pytest.raises(VulcanFetchError):
        await client.get_remarks(STUDENT_A)


async def test_empty_response_is_valid():
    client = VulcanClient({"base_url": "https://uczen.eduvulcan.pl/example", "cookies": []})
    client._request = AsyncMock(return_value=[])
    assert await client.get_remarks(STUDENT_A) == []


@pytest.mark.parametrize("existing_installation", [False, True])
async def test_baseline_then_new_items_and_updates(db, existing_installation):
    if existing_installation:
        await db.set_state(f"last_sync:{STUDENT_A.key}", "2026-10-01")
    client = _make_mock_client()
    client.get_remarks.return_value = [REMARK]
    baseline = await sync_all(client, db)
    assert baseline.student_results[0].is_first_remarks_sync
    assert not baseline.student_results[0].new_remarks
    assert len(await db.get_remarks_for_student(STUDENT_A.key)) == 1
    assert await db.get_state(f"last_sync:{STUDENT_A.key}:remarks")

    client.get_remarks.return_value = [replace(REMARK, content="Edited"), NOTE, NOTE]
    result = await sync_all(client, db)
    sr = result.student_results[0]
    assert not sr.is_first_remarks_sync
    assert len(sr.new_remarks) == 1
    assert sr.new_remarks[0].raw == NOTE
    assert sr.has_changes and sr.all_changes == sr.new_remarks
    assert "Pochwały i uwagi" in format_full_sync(result)
    assert "1 remarks" in format_compact_sync(result)
    rows = await db.get_remarks_for_student(STUDENT_A.key)
    assert next(row for row in rows if row["id"] == REMARK.id)["content"] == "Edited"
    assert not (await sync_all(client, db)).student_results[0].new_remarks


async def test_failed_fetch_does_not_initialize_baseline(db):
    client = _make_mock_client()
    client.get_remarks.side_effect = VulcanFetchError("HTTP 500")
    result = await sync_all(client, db)
    assert result.has_failures
    assert "remarks" in result.student_results[0].failed_sections
    assert await db.get_state(f"last_sync:{STUDENT_A.key}:remarks") is None
    assert await db.get_state(f"last_success:{STUDENT_A.key}:remarks") is None
    assert (await db.get_last_sync_run())["status"] == "degraded"

    client.get_remarks.side_effect = None
    client.get_remarks.return_value = [REMARK]
    result = await sync_all(client, db)
    assert result.student_results[0].is_first_remarks_sync
    assert not result.student_results[0].new_remarks
    assert await db.get_state(f"last_success:{STUDENT_A.key}:remarks")


async def test_student_scopes_soft_delete_and_restore(db):
    client = _make_mock_client(students=[STUDENT_A, STUDENT_B])
    await sync_all(client, db)  # Successful empty baselines are meaningful too.
    client.get_remarks.side_effect = [[REMARK], [NOTE]]
    result = await sync_all(client, db)
    assert [len(sr.new_remarks) for sr in result.student_results] == [1, 1]
    client.get_remarks.side_effect = [[], []]
    deleted = await sync_all(client, db)
    assert not any(sr.new_remarks for sr in deleted.student_results)
    assert (await db.get_remarks_for_student(STUDENT_A.key))[0]["deleted_at"]
    client.get_remarks.side_effect = [[REMARK], [NOTE]]
    restored = await sync_all(client, db)
    assert not any(sr.new_remarks for sr in restored.student_results)
    assert (await db.get_remarks_for_student(STUDENT_A.key))[0]["deleted_at"] is None


async def test_partial_results_keep_remarks_on_session_expiry(db):
    from vulcan_notify.client import SessionExpiredError

    client = _make_mock_client()
    await sync_all(client, db)
    client.get_remarks.return_value = [REMARK]
    client.get_messages.side_effect = SessionExpiredError("expired")
    with pytest.raises(SyncSessionExpiredError) as error:
        await sync_all(client, db)
    assert len(error.value.partial_result.student_results[0].new_remarks) == 1
    assert len(await db.get_remarks_for_student(STUDENT_A.key)) == 1
    client.get_messages.side_effect = None
    assert not (await sync_all(client, db)).student_results[0].new_remarks


@pytest.fixture
def email_settings(monkeypatch):
    config = Settings(
        _env_file=None,
        email_enabled=True,
        smtp_host="smtp.example.org",
        email_from="school@example.org",
        email_to=["first@example.org", "second@example.org"],
        email_ai_summary=True,
        llm_api_key="test-key",
        email_include_message_bodies=False,
    )
    monkeypatch.setattr(email, "settings", config)
    return config


async def _remark_result(db, student=STUDENT_A, items=None):
    changes = await diff_remarks(student, [REMARK, NOTE] if items is None else items, db)
    return FullSyncResult([SyncResult(student, new_remarks=changes)])


async def test_individual_mail_content_links_no_ai_and_retries(db, email_settings, monkeypatch):
    result = await _remark_result(db)
    ai = AsyncMock()
    monkeypatch.setattr(email, "summarize", ai)
    sender = MagicMock(side_effect=OSError("SMTP unavailable"))
    monkeypatch.setattr(email, "_send_email", sender)
    await email.publish_email(result, db)
    rows = await db.list_email_outbox()
    assert len(rows) == 4  # Two items, two recipients; no digest.
    assert rows[0]["subject"] == "[Uwagi] Jan: Pochwała"
    assert rows[2]["subject"] == "[Uwagi] Jan: Uwaga"
    assert "Uczeń:" not in rows[0]["body"]
    assert "Jan" in rows[0]["subject"]
    assert "Autor: Test Teacher" in rows[0]["body"]
    assert "2026-10-02 10:00 (piątek)" in rows[0]["body"]
    assert "Pomoc w zajęciach" in rows[0]["body"]
    assert REMARK.url in rows[0]["body"] and REMARK.url in rows[0]["html_body"]
    assert "Otwórz pochwały i uwagi" in rows[0]["html_body"]
    assert "background:#f1f5f9" in rows[0]["html_body"]
    assert "max-width:680px" in rows[0]["html_body"]
    assert '<h1 style="margin:0;font-size:26px">Pochwały i uwagi</h1>' in rows[0]["html_body"]
    assert '<h2 style="margin:0;font-size:22px">Jan</h2>' in rows[0]["html_body"]
    assert "wiadomosci.eduvulcan" not in rows[0]["html_body"]
    ai.assert_not_awaited()
    assert email.format_summary(result)[1] == 0

    # Repeated output delivery uses exactly the stored bodies and Message-IDs.
    await email.queue_remarks(result, db)
    assert await db.list_email_outbox() == rows
    sender.side_effect = None
    assert await email.drain_email_outbox(db) == (4, 0)
    assert [call.args[0] for call in sender.call_args_list[-4:]] == rows
    await email.queue_remarks(result, db)
    assert await db.list_email_outbox() == []


async def test_email_student_scoped_identity_and_prefix(db, email_settings):
    email_settings.email_remark_subject_prefix = "[Zachowanie]"
    result = await _remark_result(db, items=[REMARK])
    await email.queue_remarks(result, db)
    other = await _remark_result(db, STUDENT_B, [REMARK])  # Same upstream ID, another child.
    await email.queue_remarks(other, db)
    rows = await db.list_email_outbox()
    assert len(rows) == 4
    assert all(row["subject"].startswith("[Zachowanie]") for row in rows)


async def test_mixed_digest_keeps_notes_out_of_ai_input(db, email_settings, monkeypatch):
    result = await _remark_result(db, items=[REMARK])
    result.student_results[0].new_grades = [
        Change("new", "grade", STUDENT_A.name, "Math: 5", "Sprawdzian")
    ]
    ai = AsyncMock(return_value="Grade summary")
    monkeypatch.setattr(email, "summarize", ai)
    await email.queue_remarks(result, db)
    await email.queue_summary(result, db)
    assert ai.await_count == 1
    prompt = ai.call_args.args[0]
    assert "Math: 5" in prompt
    assert "Pomoc w zajęciach" not in prompt and "Pochwała" not in prompt
    rows = await db.list_email_outbox()
    assert len(rows) == 4  # One note and one grade digest, each to two recipients.
    assert sum(row["body"].startswith("Grade summary\n") for row in rows) == 2


@pytest.mark.parametrize("baseline_field", ["is_first_sync", "is_first_remarks_sync"])
async def test_email_respects_category_and_student_baselines(db, email_settings, baseline_field):
    result = await _remark_result(db)
    setattr(result.student_results[0], baseline_field, True)
    await email.queue_remarks(result, db)
    assert await db.list_email_outbox() == []


async def test_email_sanitizes_original_content_and_headers(db, email_settings):
    item = replace(
        REMARK,
        category="Uwaga\r\nBcc: example",
        content='<script>bad()</script><p>Safe</p><img src="https://example.org/pixel">',
    )
    result = await _remark_result(db, items=[item])
    await email.queue_remarks(result, db)
    row = (await db.list_email_outbox())[0]
    assert "\r" not in row["subject"] and "\n" not in row["subject"]
    assert "<script" not in row["html_body"] and "<img" not in row["html_body"]
    assert "Safe" in row["html_body"]
    assert item.content.startswith("<script>")
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            email_enabled=True,
            smtp_host="example.org",
            email_from="a@example.org",
            email_to=["b@example.org"],
            email_remark_subject_prefix="[Uwagi]\nInjected",
        )


async def test_note_template_preserves_rich_content_and_escapes_student_metadata(
    db, email_settings
):
    email_settings.email_include_message_bodies = False
    student = replace(STUDENT_A, name="Jan & rodzic", school="<b>Test School</b>")
    item = replace(
        REMARK,
        content="<p><strong>Ważne</strong> informacje.</p><ul><li>Punkt 1</li></ul>"
        '<script>bad()</script><img src="https://example.org/pixel">',
    )
    result = await _remark_result(db, student=student, items=[item])
    await email.queue_remarks(result, db)
    row = (await db.list_email_outbox())[0]
    assert "Jan &amp; rodzic" in row["html_body"]
    assert "Test School" in row["html_body"] and "<b>Test School</b>" not in row["html_body"]
    assert "<strong>Ważne</strong>" in row["html_body"]
    assert "<ul><li>Punkt 1</li></ul>" in row["html_body"]
    assert "<script" not in row["html_body"] and "<img" not in row["html_body"]
    assert "Ważne informacje." in row["body"]
    assert "- Punkt 1" in row["body"]


async def test_mqtt_payload_and_outbox(db, monkeypatch):
    monkeypatch.setattr(mqtt.settings, "mqtt_enabled", True)
    monkeypatch.setattr(mqtt.settings, "mqtt_topic_prefix", "school")
    monkeypatch.setattr(mqtt.settings, "display_name_map", {})
    result = await _remark_result(db, items=[NOTE])
    await mqtt.enqueue_changes(result, db)
    row = (await db.list_mqtt_outbox())[0]
    assert row["topic"] == "school/jan/remarks/new"
    payload = json.loads(row["payload"])
    assert payload["id"] == NOTE.id and payload["content"] == NOTE.content
    assert payload["url"] == NOTE.url
    assert payload["student"] == STUDENT_A.name


async def test_api_filters_students_deletions_and_limits(db, monkeypatch):
    monkeypatch.setattr(api.settings, "db_path", db._db_path)
    for student in (STUDENT_A, STUDENT_B):
        await db.upsert_student(student)
        await db.upsert_remark(student.key, REMARK)
    await db.upsert_remark(STUDENT_A.key, NOTE)
    await db.mark_missing_remarks(STUDENT_A.key, {NOTE.id})
    await db.commit()
    result = api._get_remarks("Jan")
    assert set(result) == {"Jan"}
    assert [row["id"] for row in result["Jan"]["remarks"]] == [NOTE.id]
    assert len(api._get_remarks()["Anna"]["remarks"]) == 1
    response = await api.handle_remarks(make_mocked_request("GET", "/api/remarks?student=Jan&n=1"))
    body = json.loads(response.text)
    assert set(body) == {"_meta", "Jan"}
    assert "remarks" in body["_meta"]["sections"]
    assert "/api/remarks" in {
        resource.canonical for resource in api.create_app().router.resources()
    }


async def test_schema_upgrade_preserves_existing_state(tmp_path):
    path = tmp_path / "older.db"
    with sqlite3.connect(path) as old:
        old.execute(
            "CREATE TABLE sync_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "updated_at TIMESTAMP)"
        )
        old.execute("INSERT INTO sync_state (key, value) VALUES ('last_sync:KEYA', '2026-10-01')")
    for _ in range(2):
        database = Database(path)
        await database.connect()
        try:
            assert await database.get_state("last_sync:KEYA") == "2026-10-01"
            await database.upsert_student(STUDENT_A)
            await database.upsert_remark(STUDENT_A.key, REMARK)
            await database.commit()
            assert len(await database.get_remarks_for_student(STUDENT_A.key)) == 1
        finally:
            await database.close()
