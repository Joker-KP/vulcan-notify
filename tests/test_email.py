"""SMTP digests: current changes, durable retries, privacy and optional AI."""

import asyncio
import smtplib
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from vulcan_notify import __main__ as cli
from vulcan_notify import email
from vulcan_notify.config import Settings
from vulcan_notify.db import Database
from vulcan_notify.differ import Change
from vulcan_notify.models import Message, Student
from vulcan_notify.sync import FullSyncResult, SyncResult, SyncSessionExpiredError

STUDENT = Student("fixture-key", "Test Student", "3A", "Test School", 1, "mailbox-key")
MESSAGE = Message(
    1,
    "fixture-message-key",
    "Test Teacher",
    "Trip details",
    "2026-10-02T09:00:00",
    "Test Student",
    True,
    False,
    "<p>Private message body<br>Second line</p>",
)


@pytest.fixture
def email_config(monkeypatch):
    config = Settings(
        _env_file=None,
        email_enabled=True,
        smtp_host="smtp.example.org",
        email_from="school@example.org",
        email_to=["parent@example.org"],
        smtp_username="test-user",
        smtp_password="test-password",
        llm_api_key=None,
        quiet_hours_tz="Europe/Warsaw",
    )
    monkeypatch.setattr(email, "settings", config)
    return config


@pytest.fixture
def smtp(monkeypatch):
    connection = MagicMock()
    connection.send_message.return_value = {}
    for name in ["SMTP", "SMTP_SSL"]:
        monkeypatch.setattr(email.smtplib, name, MagicMock(return_value=connection))
    return connection


def changed_result():
    change = Change("updated", "grade", STUDENT.name, "Math: 3 → 4", "Test, weight 2")
    return FullSyncResult([SyncResult(STUDENT, new_grades=[change])])


async def test_disabled_output_does_not_access_db_or_smtp(monkeypatch):
    monkeypatch.setattr(email, "settings", Settings(_env_file=None, email_enabled=False))
    db = MagicMock()
    await email.publish_email(changed_result(), db)
    assert await email.drain_email_outbox(db) == (0, 0)
    assert not db.mock_calls


async def test_first_baseline_and_unchanged_sync_do_not_email(db, email_config, smtp):
    result = changed_result()
    result.new_messages = [MESSAGE]
    result.student_results[0].is_first_sync = True
    result.is_first_message_sync = True
    await email.publish_email(result, db)
    await email.publish_email(FullSyncResult([SyncResult(STUDENT)]), db)
    assert not smtp.send_message.called
    assert await db.list_email_outbox() == []


def test_digest_covers_every_student_change_and_excludes_messages():
    result = changed_result()
    result.new_messages = [MESSAGE]
    other = replace(STUDENT, key="other-key", name="Other Student")
    result.student_results.append(
        SyncResult(
            other,
            new_exams=[
                Change("new", "exam", other.name, "Science quiz", "2026-10-05"),
            ],
        )
    )
    result.student_results[0].new_substitutions = [
        Change("new", kind, STUDENT.name, kind, "2026-10-03")
        for kind in ["substitution", "cancellation", "addition"]
    ]
    result.student_results[0].new_attendance = [
        Change("new", "attendance", STUDENT.name, "Absent", "Lesson 3"),
    ]
    result.student_results[0].new_homework = [
        Change("new", "homework", STUDENT.name, "Read chapter", "Due 2026-10-04"),
    ]
    body, count = email.format_summary(result)
    assert count == 7
    assert "Math: 3 → 4" in body
    assert "Test Student (3A, Test School)" in body
    assert "Other Student (3A, Test School)" in body
    assert "Trip details" not in body
    assert "Private message body" not in body
    for kind in ["substitution", "cancellation", "addition", "attendance", "homework", "exam"]:
        assert f"[{kind}/new]" in body


def test_message_bodies_opt_in_and_partial_failure_notice(email_config):
    email_config.email_include_message_bodies = True
    result = changed_result()
    result.message_failure = "Sensitive upstream failure details"
    body = email.format_message(MESSAGE, email_config)
    assert "Private message body\nSecond line" in body
    assert "<p>" not in body
    assert "Skrzynka: Test Student" in body
    assert "Załączniki: tak" in body
    summary, _ = email.format_summary(result)
    assert "Some sections failed" in summary
    assert "Sensitive upstream failure details" not in summary


@pytest.mark.parametrize(
    "student_baseline,message_baseline,count", [(True, False, 1), (False, True, 1)]
)
async def test_student_and_account_baselines_are_independent(
    db,
    email_config,
    smtp,
    student_baseline,
    message_baseline,
    count,
):
    result = changed_result()
    result.new_messages = [MESSAGE]
    result.student_results[0].is_first_sync = student_baseline
    result.is_first_message_sync = message_baseline
    await email.publish_email(result, db)
    assert smtp.send_message.call_count == count
    subject = str(smtp.send_message.call_args.args[0]["Subject"])
    assert subject.startswith("[Nowa wiadomość]" if student_baseline else "eduVULCAN:")


async def test_durable_per_recipient_retry_and_deduplication(db, email_config, smtp, caplog):
    email_config.email_to = ["parent@example.org", "second@example.org", "parent@example.org"]
    result = changed_result()
    smtp.send_message.side_effect = [
        {},
        smtplib.SMTPException("second@example.org test-password private body"),
    ]
    await email.publish_email(result, db)
    pending = await db.list_email_outbox()
    assert len(pending) == 1
    assert pending[0]["recipient"] == "second@example.org"
    message_id = pending[0]["message_id"]
    assert "second@example.org" not in caplog.text
    assert "test-password" not in caplog.text
    assert "private body" not in caplog.text
    cursor = await db.db.execute(
        "SELECT attempts, last_error FROM email_outbox WHERE sent_at IS NULL"
    )
    assert await cursor.fetchone() == (1, "SMTPException")

    # Simulate a later process using the same persistent database.
    await db.close()
    await db.connect()
    smtp.send_message.side_effect = None
    smtp.reset_mock()
    await email.publish_email(FullSyncResult([SyncResult(STUDENT)]), db)
    smtp.send_message.assert_called_once()
    sent = smtp.send_message.call_args.args[0]
    assert sent["Message-ID"] == message_id
    assert sent["To"] == "second@example.org"
    assert smtp.send_message.call_args.kwargs["to_addrs"] == ["second@example.org"]
    assert await db.list_email_outbox() == []

    # Re-delivery of this exact run cannot resend either recipient.
    await email.publish_email(result, db)
    assert smtp.send_message.call_count == 1
    # A later run may legitimately report the same change again (e.g. a grade reversal).
    await email.publish_email(changed_result(), db)
    assert smtp.send_message.call_count == 3
    cursor = await db.db.execute("SELECT body, subject, sender, recipient FROM email_outbox")
    assert all(tuple(row) == (None, None, None, None) for row in await cursor.fetchall())


@pytest.mark.parametrize("security", ["starttls", "ssl", "none"])
async def test_smtp_transport_headers_and_utf8(db, email_config, smtp, security):
    email_config.smtp_security = security
    email_config.smtp_port = 465 if security == "ssl" else 587
    await email.publish_email(changed_result(), db)
    factory = email.smtplib.SMTP_SSL if security == "ssl" else email.smtplib.SMTP
    assert factory.call_args.args == ("smtp.example.org", email_config.smtp_port)
    assert factory.call_args.kwargs["timeout"] == 30
    smtp.login.assert_called_once_with("test-user", "test-password")
    assert smtp.starttls.call_count == (1 if security == "starttls" else 0)
    if security in ("ssl", "starttls"):
        context = (
            factory.call_args.kwargs["context"]
            if security == "ssl"
            else smtp.starttls.call_args.kwargs["context"]
        )
        assert context.check_hostname
    message = smtp.send_message.call_args.args[0]
    assert message["Subject"] == "eduVULCAN: 1 change(s)"
    assert message["From"] == "school@example.org"
    assert message["To"] == "parent@example.org"
    assert "Math: 3 → 4" in message.get_content()
    assert "Trip details" not in message.get_content()
    assert "Private message body" not in message.get_content()
    assert message["Message-ID"] and message["Date"]


async def test_anonymous_relay_and_quit_failure_after_acceptance(db, email_config, smtp):
    email_config.smtp_username = None
    email_config.smtp_password = None
    smtp.quit.side_effect = OSError("connection closed")
    smtp.close.side_effect = OSError("already closed")
    await email.publish_email(changed_result(), db)
    smtp.login.assert_not_called()
    assert await db.list_email_outbox() == []


@pytest.mark.parametrize(
    "sender,name",
    [
        ("school@example.org", ""),
        ("Some One <school@example.org>", "Some One"),
        ('"One, Some" <school@example.org>', "One, Some"),
        ("Łukasz Żółć <school@example.org>", "Łukasz Żółć"),
    ],
)
async def test_sender_name_is_preserved_in_header_and_removed_from_envelope(
    db,
    email_config,
    smtp,
    sender,
    name,
):
    config = Settings(_env_file=None, **(email_config.model_dump() | {"email_from": sender}))
    email_config.email_from = config.email_from
    result = changed_result()
    await email.queue_summary(result, db)
    assert (await db.list_email_outbox())[0]["sender"] == sender
    # A configuration change must not alter the original queued sender on retry.
    email_config.email_from = "New Sender <other@example.org>"
    await email.drain_email_outbox(db)
    message = smtp.send_message.call_args.args[0]
    assert message["From"].addresses[0].display_name == name
    assert message["From"].addresses[0].addr_spec == "school@example.org"
    assert smtp.send_message.call_args.kwargs["from_addr"] == "school@example.org"
    # Verify names survive actual MIME header encoding, including non-ASCII names.
    parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
    assert parsed["From"].addresses[0].display_name == name


@pytest.mark.parametrize(
    "failure", [TimeoutError(), smtplib.SMTPAuthenticationError(535, b"secret")]
)
async def test_transport_failure_keeps_digest(db, email_config, smtp, failure):
    email.smtplib.SMTP.side_effect = failure
    await email.publish_email(changed_result(), db)
    assert len(await db.list_email_outbox()) == 1


async def test_refused_recipient_return_keeps_digest(db, email_config, smtp):
    smtp.send_message.return_value = {"parent@example.org": (550, b"Rejected")}
    await email.publish_email(changed_result(), db)
    assert len(await db.list_email_outbox()) == 1


async def test_ai_replaces_plain_body_once_and_retry_reuses_it(db, email_config, smtp, monkeypatch):
    email_config.email_ai_summary = True
    email_config.llm_api_key = "synthetic-key"
    ai = AsyncMock(return_value="AI: Grade improved; trip details available.")
    monkeypatch.setattr(email, "summarize", ai)
    smtp.send_message.side_effect = TimeoutError()
    result = changed_result()
    await email.publish_email(result, db)
    ai.assert_awaited_once()
    assert "Math: 3 → 4" in ai.call_args.args[0]
    assert "Private message body" not in ai.call_args.args[0]
    assert (await db.list_email_outbox())[0]["body"].startswith("AI:")
    smtp.send_message.side_effect = None
    await email.publish_email(result, db)
    assert ai.await_count == 1
    assert smtp.send_message.call_args.args[0].get_content().startswith("AI:")


@pytest.mark.parametrize("replacement", [None, "", "   ", TimeoutError(), RuntimeError("secret")])
async def test_ai_failure_uses_already_persisted_plain_digest(
    db,
    email_config,
    smtp,
    monkeypatch,
    replacement,
):
    email_config.email_ai_summary = True
    email_config.llm_api_key = "synthetic-key"

    async def ai(body, config):
        # The digest is durable before cloud preparation starts.
        assert (await db.list_email_outbox())[0]["body"] == body
        if isinstance(replacement, Exception):
            raise replacement
        return replacement

    monkeypatch.setattr(email, "summarize", ai)
    await email.publish_email(changed_result(), db)
    assert "Math: 3 → 4" in smtp.send_message.call_args.args[0].get_content()
    assert await db.list_email_outbox() == []


async def test_ai_timeout_is_bounded(db, email_config, smtp, monkeypatch):
    email_config.email_ai_summary = True
    email_config.llm_api_key = "synthetic-key"
    email_config.email_ai_timeout_seconds = 0.01

    async def never_finishes(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(email, "summarize", never_finishes)
    await email.publish_email(changed_result(), db)
    assert "Math: 3 → 4" in smtp.send_message.call_args.args[0].get_content()


async def test_interrupted_ai_preparation_preserves_plain_digest(
    db, email_config, smtp, monkeypatch
):
    email_config.email_ai_summary = True
    email_config.llm_api_key = "synthetic-key"
    monkeypatch.setattr(email, "summarize", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await email.queue_summary(changed_result(), db)
    assert "Math: 3 → 4" in (await db.list_email_outbox())[0]["body"]
    assert await email.drain_email_outbox(db) == (1, 0)


@pytest.mark.parametrize("enabled,key", [(False, "synthetic-key"), (True, None)])
async def test_ai_requires_explicit_opt_in_and_key(
    db, email_config, smtp, monkeypatch, enabled, key
):
    email_config.email_ai_summary = enabled
    email_config.llm_api_key = key
    ai = AsyncMock()
    monkeypatch.setattr(email, "summarize", ai)
    await email.publish_email(changed_result(), db)
    ai.assert_not_awaited()


@pytest.mark.parametrize(
    "overrides",
    [
        {"smtp_host": ""},
        {"email_to": []},
        {"email_from": "invalid"},
        {"email_from": "Name <invalid>"},
        {"email_from": "Name <school@example.org"},
        {"email_from": "Name <school@example.org> trailing junk"},
        {"email_from": "One <school@example.org>, Two <other@example.org>"},
        {"email_from": "Group: school@example.org;"},
        {"email_from": "Name\r\nBcc: other@example.org <school@example.org>"},
        {"email_from": "Name\x00 <school@example.org>"},
        {"email_to": ["parent@example.org\nBcc: other@example.org"]},
        {"email_subject_prefix": "School\r\nBcc: other@example.org"},
        {"email_message_subject_prefix": "Message\r\nBcc: other@example.org"},
        {"smtp_security": "invalid"},
        {"smtp_port": 0},
        {"smtp_timeout_seconds": 0},
        {"smtp_username": "user", "smtp_password": None},
    ],
)
def test_invalid_email_configuration_fails_without_echoing_secrets(overrides):
    values = dict(
        email_enabled=True,
        smtp_host="smtp.example.org",
        email_from="school@example.org",
        email_to=["parent@example.org"],
        smtp_password="private-secret",
        smtp_username="user",
    )
    values.update(overrides)
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, **values)
    assert "private-secret" not in str(error.value)


def test_smtp_dotenv_configuration(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "EMAIL_ENABLED=true\nSMTP_HOST=smtp.example.org\n"
        'EMAIL_FROM="Some One <school@example.org>"\n'
        'EMAIL_TO=["parent@example.org","second@example.org"]\nSMTP_SECURITY=ssl\nSMTP_PORT=465\n'
    )
    config = Settings(_env_file=env)
    assert config.email_from == "Some One <school@example.org>"
    assert config.email_to == ["parent@example.org", "second@example.org"]
    assert config.smtp_security == "ssl"


async def test_additive_schema_preserves_existing_database(tmp_path):
    import aiosqlite

    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as connection:
        await connection.executescript(
            "CREATE TABLE sync_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT);"
            "INSERT INTO sync_state VALUES ('last_sync:fixture-key', 'existing-baseline', NULL);"
        )
        await connection.commit()
    database = Database(path)
    await database.connect()
    try:
        assert await database.get_state("last_sync:fixture-key") == "existing-baseline"
        assert await database.list_email_outbox() == []
    finally:
        await database.close()


async def test_email_retry_cli_requires_no_upstream_auth(db, email_config, smtp, monkeypatch):
    await email.queue_summary(changed_result(), db)
    monkeypatch.setattr(cli, "settings", email_config)
    monkeypatch.setattr(cli, "Database", MagicMock(return_value=db))
    auth = AsyncMock(side_effect=AssertionError("must not authenticate"))
    monkeypatch.setattr(cli, "_ensure_session", auth)
    await cli.cmd_email_retry()
    auth.assert_not_awaited()
    smtp.send_message.assert_called_once()


async def test_cli_delivers_partial_and_retried_results_to_email(monkeypatch):
    partial = changed_result()
    complete = changed_result()
    database = MagicMock(connect=AsyncMock(), close=AsyncMock())
    client = MagicMock(close=AsyncMock())
    monkeypatch.setattr(cli, "_ensure_session", AsyncMock(return_value={}))
    monkeypatch.setattr(cli, "VulcanClient", MagicMock(return_value=client))
    monkeypatch.setattr(cli, "Database", MagicMock(return_value=database))
    monkeypatch.setattr(cli, "_get_credentials", MagicMock(return_value=("fixture", "fixture")))
    monkeypatch.setattr(cli, "_recover_session", AsyncMock(return_value={}))
    monkeypatch.setattr(cli, "_sync_calendar", AsyncMock())
    monkeypatch.setattr(cli, "format_compact_sync", MagicMock(return_value="fixture"))
    monkeypatch.setattr(
        cli,
        "sync_all",
        AsyncMock(
            side_effect=[
                SyncSessionExpiredError("expired", partial),
                complete,
            ]
        ),
    )
    deliver = AsyncMock()
    mqtt = AsyncMock()
    monkeypatch.setattr(cli, "publish_email", deliver)
    monkeypatch.setattr(cli, "publish_changes", mqtt)
    await cli.cmd_sync()
    assert [call.args[0] for call in deliver.await_args_list] == [partial, complete]
    assert [call.args[0] for call in mqtt.await_args_list] == [partial, complete]


async def test_smtp_failure_does_not_prevent_mqtt_or_fail_sync(
    db,
    email_config,
    smtp,
    monkeypatch,
):
    smtp.send_message.side_effect = smtplib.SMTPException("private details")
    result = changed_result()
    monkeypatch.setattr(cli, "_ensure_session", AsyncMock(return_value={}))
    monkeypatch.setattr(cli, "VulcanClient", MagicMock(return_value=MagicMock(close=AsyncMock())))
    monkeypatch.setattr(cli, "Database", MagicMock(return_value=db))
    monkeypatch.setattr(cli, "sync_all", AsyncMock(return_value=result))
    monkeypatch.setattr(cli, "_sync_calendar", AsyncMock())
    monkeypatch.setattr(cli, "format_compact_sync", MagicMock(return_value="fixture"))
    mqtt = AsyncMock()
    monkeypatch.setattr(cli, "publish_changes", mqtt)
    await cli.cmd_sync()
    mqtt.assert_awaited_once_with(result, db)
    # cmd_sync closes the DB; reopen to inspect the durable failure.
    await db.connect()
    assert len(await db.list_email_outbox()) == 1


async def test_each_new_message_has_own_subject_and_body_separate_from_digest(
    db,
    email_config,
    smtp,
):
    email_config.email_include_message_bodies = True
    result = changed_result()
    first = replace(MESSAGE, subject="Zebranie rodziców")
    second = replace(
        MESSAGE,
        id=2,
        api_global_key="other-message-key",
        subject="Plan wycieczki",
        content="<p>Different private content</p>",
        mailbox="Other Student",
    )
    result.new_messages = [first, second]
    await email.publish_email(result, db)
    sent = {str(call.args[0]["Subject"]): call.args[0] for call in smtp.send_message.call_args_list}
    assert set(sent) == {
        "[Nowa wiadomość] Zebranie rodziców",
        "[Nowa wiadomość] Plan wycieczki",
        "eduVULCAN: 1 change(s)",
    }
    first_body = sent["[Nowa wiadomość] Zebranie rodziców"].get_body(("plain",)).get_content()
    assert "Private message body" in first_body
    assert "Autor: Test Teacher" in first_body and "Skrzynka: Test Student" in first_body
    assert "Different private content" not in first_body
    second_body = sent["[Nowa wiadomość] Plan wycieczki"].get_body(("plain",)).get_content()
    assert "Different private content" in second_body
    assert "Skrzynka: Other Student" in second_body
    assert "Math: 3 → 4" not in first_body + second_body
    assert "Zebranie rodziców" not in first_body
    assert "Plan wycieczki" not in second_body
    digest = sent["eduVULCAN: 1 change(s)"].get_content()
    assert "Math: 3 → 4" in digest
    assert "Zebranie rodziców" not in digest and "Plan wycieczki" not in digest


async def test_messages_only_send_no_digest_or_ai(db, email_config, smtp, monkeypatch):
    email_config.email_ai_summary = True
    email_config.llm_api_key = "synthetic-key"
    ai = AsyncMock()
    monkeypatch.setattr(email, "summarize", ai)
    await email.publish_email(FullSyncResult([], [MESSAGE]), db)
    smtp.send_message.assert_called_once()
    sent = smtp.send_message.call_args.args[0]
    assert sent["Subject"] == "[Nowa wiadomość] Trip details"
    assert "Private message body" not in sent.get_body(("plain",)).get_content()
    ai.assert_not_awaited()


async def test_custom_message_prefix_and_upstream_line_breaks(db, email_config, smtp):
    email_config.email_message_subject_prefix = "[Szkoła]"
    message = replace(MESSAGE, subject="Zebranie\r\nrodziców <klasa 3A>")
    await email.publish_email(FullSyncResult([], [message]), db)
    sent = smtp.send_message.call_args.args[0]
    assert sent["Subject"] == "[Szkoła] Zebranie rodziców <klasa 3A>"
    assert "Bcc" not in sent


async def test_message_retry_deduplicates_across_runs_and_recipients(db, email_config, smtp):
    email_config.email_to = ["parent@example.org", "second@example.org"]
    smtp.send_message.side_effect = [{}, TimeoutError()]
    await email.publish_email(FullSyncResult([], [MESSAGE]), db)
    pending = await db.list_email_outbox()
    assert len(pending) == 1
    assert pending[0]["recipient"] == "second@example.org"
    assert pending[0]["subject"] == "[Nowa wiadomość] Trip details"
    original_message_id = pending[0]["message_id"]
    await db.close()
    await db.connect()
    smtp.send_message.side_effect = None
    smtp.reset_mock()
    # A different run rediscovering the same upstream message must not re-enqueue it.
    await email.publish_email(FullSyncResult([], [MESSAGE]), db)
    smtp.send_message.assert_called_once()
    assert smtp.send_message.call_args.args[0]["Message-ID"] == original_message_id
    assert smtp.send_message.call_args.kwargs["to_addrs"] == ["second@example.org"]
    await email.publish_email(FullSyncResult([], [MESSAGE]), db)
    assert smtp.send_message.call_count == 1
    assert await db.list_email_outbox() == []


async def test_individual_messages_are_durable_before_ai_and_excluded_from_ai_input(
    db,
    email_config,
    smtp,
    monkeypatch,
):
    email_config.email_ai_summary = True
    email_config.llm_api_key = "synthetic-key"
    email_config.email_include_message_bodies = True
    result = changed_result()
    result.new_messages = [MESSAGE]

    async def ai(body, config):
        pending = await db.list_email_outbox()
        assert len(pending) == 2
        assert "Trip details" not in body and "Private message body" not in body
        assert any("Private message body" in row["body"] for row in pending)
        return "AI: Grade improved."

    monkeypatch.setattr(email, "summarize", ai)
    await email.publish_email(result, db)
    sent = {str(call.args[0]["Subject"]): call.args[0] for call in smtp.send_message.call_args_list}
    assert sent["eduVULCAN: 1 change(s)"].get_content().startswith("AI: Grade improved.")
    assert (
        "Private message body"
        in sent["[Nowa wiadomość] Trip details"].get_body(("plain",)).get_content()
    )


@pytest.mark.parametrize("include_body", [False, True])
async def test_message_footer_has_html_button_and_plain_link(db, email_config, smtp, include_body):
    email_config.email_include_message_bodies = include_body
    url = "https://wiadomosci.eduvulcan.pl/testdistrict/App/odebrane"
    message = replace(
        MESSAGE,
        mailbox_url=url,
        subject="Zebranie <klasa> & rodzice",
        sender="Teacher & Parent",
    )
    result = changed_result()
    result.new_messages = [message]
    await email.publish_email(result, db)
    sent = smtp.send_message.call_args_list[0].args[0]
    # Inspect serialized MIME, as an email client would receive it.
    parsed = BytesParser(policy=policy.default).parsebytes(sent.as_bytes())
    assert parsed.get_content_type() == "multipart/alternative"
    plain = parsed.get_body(preferencelist=("plain",)).get_content()
    html = parsed.get_body(preferencelist=("html",)).get_content()
    assert plain.rstrip().endswith(f"Otwórz skrzynkę wiadomości:\n{url}")
    assert f'href="{url}"' in html
    assert html.count("Otwórz skrzynkę wiadomości") == 1
    assert "margin-top:24px" in html and "border-radius:6px" in html
    assert "Autor: <strong>Teacher &amp; Parent</strong>" in html
    assert "Zebranie" not in html and "Zebranie" not in plain
    assert parsed["Subject"] == "[Nowa wiadomość] Zebranie <klasa> & rodzice"
    assert "<klasa>" not in html
    assert html.index("Skrzynka:") < html.index("Otwórz skrzynkę wiadomości")
    assert "Data: 2026-10-02 11:00 (piątek)" in plain
    assert "Data: 2026-10-02 11:00 (piątek)" in html
    assert ("Private message body" in plain) is include_body
    assert ("Private message body" in html) is include_body
    digest = smtp.send_message.call_args_list[1].args[0]
    assert not digest.is_multipart()
    assert url not in digest.get_content()
    cursor = await db.db.execute("SELECT body, html_body FROM email_outbox")
    assert all(tuple(row) == (None, None) for row in await cursor.fetchall())


async def test_mailbox_link_and_html_are_preserved_across_restarts_and_retries(
    db,
    email_config,
    smtp,
):
    url = "https://wiadomosci.eduvulcan.pl/originaldistrict/App/odebrane"
    message = replace(MESSAGE, mailbox_url=url)
    smtp.send_message.side_effect = TimeoutError()
    await email.publish_email(FullSyncResult([], [message]), db)
    pending = (await db.list_email_outbox())[0]
    assert url in pending["body"] and url in pending["html_body"]
    await db.close()
    await db.connect()
    smtp.send_message.side_effect = None
    updated = replace(
        message, mailbox_url="https://wiadomosci.eduvulcan.pl/otherdistrict/App/odebrane"
    )
    await email.publish_email(FullSyncResult([], [updated]), db)
    sent = smtp.send_message.call_args.args[0]
    assert sent["Message-ID"] == pending["message_id"]
    assert url in sent.get_body(preferencelist=("plain",)).get_content()
    assert url in sent.get_body(preferencelist=("html",)).get_content()
    assert "otherdistrict" not in sent.as_string()


async def test_html_footer_escapes_body_and_url(db, email_config, smtp):
    email_config.email_include_message_bodies = True
    message = replace(
        MESSAGE,
        mailbox_url='https://wiadomosci.eduvulcan.pl/testdistrict/App/odebrane?x="&y=1',
        content="&lt;script&gt;alert('test')&lt;/script&gt;",
    )
    await email.publish_email(FullSyncResult([], [message]), db)
    html = smtp.send_message.call_args.args[0].get_body(preferencelist=("html",)).get_content()
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert (
        'href="https://wiadomosci.eduvulcan.pl/testdistrict/App/odebrane?x=&quot;&amp;y=1"' in html
    )


async def test_migration_preserves_existing_plain_email_queue(tmp_path, email_config, smtp):
    import aiosqlite

    path = tmp_path / "legacy-email.db"
    async with aiosqlite.connect(path) as connection:
        await connection.executescript(
            "CREATE TABLE email_outbox (delivery_key TEXT PRIMARY KEY, sender TEXT, "
            "recipient TEXT, subject TEXT, body TEXT, message_id TEXT NOT NULL, "
            "date_header TEXT NOT NULL, enqueued_at TEXT DEFAULT CURRENT_TIMESTAMP, "
            "sent_at TEXT, attempts INTEGER DEFAULT 0, last_error TEXT);"
        )
        await connection.execute(
            "INSERT INTO email_outbox "
            "(delivery_key, sender, recipient, subject, body, message_id, date_header) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-key",
                "school@example.org",
                "parent@example.org",
                "Legacy subject",
                "Existing queued content",
                "<legacy-id@example.org>",
                "Fri, 02 Oct 2026 09:00:00 +0000",
            ),
        )
        await connection.commit()
    database = Database(path)
    await database.connect()
    try:
        pending = await database.list_email_outbox()
        assert len(pending) == 1
        assert pending[0]["body"] == "Existing queued content"
        assert pending[0]["html_body"] == ""
        assert await email.drain_email_outbox(database) == (1, 0)
        sent = smtp.send_message.call_args.args[0]
        assert sent["Subject"] == "Legacy subject"
        assert sent["Message-ID"] == "<legacy-id@example.org>"
        assert not sent.is_multipart()
        # Repeat initialization to check migration remains idempotent.
        await database.close()
        await database.connect()
        assert await database.list_email_outbox() == []
    finally:
        await database.close()


@pytest.mark.parametrize(
    "timestamp,zone,expected",
    [
        ("2026-01-15T09:02:41.183Z", "Europe/Warsaw", "2026-01-15 10:02 (czwartek)"),
        ("2026-07-15T09:02:41+00:00", "Europe/Warsaw", "2026-07-15 11:02 (środa)"),
        ("2026-03-15T11:02:41.183+01:00", "Europe/Warsaw", "2026-03-15 11:02 (niedziela)"),
        ("2026-07-15T11:02:41+02:00", "Europe/Warsaw", "2026-07-15 11:02 (środa)"),
        ("2026-03-29T00:30:00Z", "Europe/Warsaw", "2026-03-29 01:30 (niedziela)"),
        ("2026-03-29T01:30:00Z", "Europe/Warsaw", "2026-03-29 03:30 (niedziela)"),
        ("2026-10-25T00:30:00Z", "Europe/Warsaw", "2026-10-25 02:30 (niedziela)"),
        ("2026-10-25T01:30:00Z", "Europe/Warsaw", "2026-10-25 02:30 (niedziela)"),
        ("2026-01-15T23:30:00Z", "Europe/Warsaw", "2026-01-16 00:30 (piątek)"),
        ("2026-10-02T09:00:00", "Europe/Warsaw", "2026-10-02 11:00 (piątek)"),
        ("2026-07-15T11:02:41+02:00", "UTC", "2026-07-15 09:02 (środa)"),
        ("2026-01-15T09:00:00Z", "America/Los_Angeles", "2026-01-15 01:00 (czwartek)"),
        ("2026-09-29T16:23:00Z", "Europe/Warsaw", "2026-09-29 18:23 (wtorek)"),
    ],
)
def test_message_date_uses_configured_zone_and_minute_precision(
    email_config,
    timestamp,
    zone,
    expected,
):
    email_config.quiet_hours_tz = zone
    body = email.format_message(replace(MESSAGE, date=timestamp), email_config)
    assert f"Data: {expected}\n" in body
    assert "Autor: Test Teacher" in body
    assert "Skrzynka: Test Student" in body
    assert "Załączniki: tak" in body
    assert MESSAGE.subject not in body
    for label in ["New message:", "From:", "Date:", "Mailbox:", "Attachments:", "Temat:"]:
        assert label not in body


@pytest.mark.parametrize("timestamp", ["", "unrecognized timestamp"])
def test_unrecognized_message_date_is_preserved(email_config, timestamp):
    body = email.format_message(replace(MESSAGE, date=timestamp), email_config)
    assert f"Data: {timestamp}\n" in body


@pytest.mark.parametrize("zone", ["Missing/Timezone", "/invalid-zone"])
def test_unknown_message_timezone_falls_back_to_utc(email_config, zone, caplog):
    email_config.quiet_hours_tz = zone
    message = replace(MESSAGE, date="2026-07-15T11:02:41+02:00")
    body = email.format_message(message, email_config)
    assert "Data: 2026-07-15 09:02 (środa)" in body
    assert "using UTC" in caplog.text


async def test_rich_message_body_and_bold_sender_survive_mime_serialization(db, email_config, smtp):
    email_config.email_include_message_bodies = True
    message = replace(
        MESSAGE,
        date="2026-09-29T16:23:00Z",
        content='<p style="color:blue;margin-bottom:12px">Dzień dobry,</p>'
        "<p><strong>Ważne</strong> informacje.</p><div>A<br>B</div>"
        "<ol><li>Przynieść zeszyt</li><li>Podpisać zgodę</li></ol>",
    )
    await email.publish_email(FullSyncResult([], [message]), db)
    parsed = BytesParser(policy=policy.default).parsebytes(
        smtp.send_message.call_args.args[0].as_bytes()
    )
    html = parsed.get_body(("html",)).get_content()
    plain = parsed.get_body(("plain",)).get_content()
    assert "Autor: <strong>Test Teacher</strong>" in html
    assert "Data: 2026-09-29 18:23 (wtorek)" in html and "(wtorek)" in plain
    assert "<p style=" in html and "color:blue" in html
    assert "<strong>Ważne</strong>" in html
    assert "<div>A<br>B</div>" in html
    assert "<ol><li>Przynieść zeszyt</li><li>Podpisać zgodę</li></ol>" in html
    assert "margin-top:0;margin-bottom:0;" in html
    assert "Dzień dobry,\nWażne informacje." in plain
    assert "A\nB" in plain
    assert "1. Przynieść zeszyt\n2. Podpisać zgodę" in plain
