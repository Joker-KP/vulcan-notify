"""SMTP digests: current changes, durable retries, privacy and optional AI."""

import asyncio
import smtplib
from dataclasses import replace
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
    return FullSyncResult([SyncResult(STUDENT, new_grades=[change])], [MESSAGE])


async def test_disabled_output_does_not_access_db_or_smtp(monkeypatch):
    monkeypatch.setattr(email, "settings", Settings(_env_file=None, email_enabled=False))
    db = MagicMock()
    await email.publish_email(changed_result(), db)
    assert await email.drain_email_outbox(db) == (0, 0)
    assert not db.mock_calls


async def test_first_baseline_and_unchanged_sync_do_not_email(db, email_config, smtp):
    result = changed_result()
    result.student_results[0].is_first_sync = True
    result.is_first_message_sync = True
    await email.publish_email(result, db)
    await email.publish_email(FullSyncResult([SyncResult(STUDENT)]), db)
    assert not smtp.send_message.called
    assert await db.list_email_outbox() == []


def test_digest_covers_every_change_and_preserves_student_and_mailbox_scope(email_config):
    result = changed_result()
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
    body, count = email.format_summary(result, email_config)
    assert count == 8
    assert "Math: 3 → 4" in body
    assert "Test Student (3A, Test School)" in body
    assert "Other Student (3A, Test School)" in body
    assert "Mailbox: Test Student" in body
    assert "Attachments: yes" in body
    assert "Private message body" not in body
    for kind in ["substitution", "cancellation", "addition", "attendance", "homework", "exam"]:
        assert f"[{kind}/new]" in body


def test_message_bodies_opt_in_and_partial_failure_notice(email_config):
    email_config.email_include_message_bodies = True
    result = changed_result()
    result.message_failure = "Sensitive upstream failure details"
    body, _ = email.format_summary(result, email_config)
    assert "Private message body\nSecond line" in body
    assert "<p>" not in body
    assert "Some sections failed" in body
    assert "Sensitive upstream failure details" not in body


@pytest.mark.parametrize(
    "student_baseline,message_baseline,count", [(True, False, 1), (False, True, 1)]
)
def test_student_and_account_baselines_are_independent(
    email_config,
    student_baseline,
    message_baseline,
    count,
):
    result = changed_result()
    result.student_results[0].is_first_sync = student_baseline
    result.is_first_message_sync = message_baseline
    assert email.format_summary(result, email_config)[1] == count


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
    assert message["Subject"] == "eduVULCAN: 2 change(s)"
    assert message["From"] == "school@example.org"
    assert message["To"] == "parent@example.org"
    assert "Math: 3 → 4" in message.get_content()
    assert "Trip details" in message.get_content()
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
    from email import policy
    from email.parser import BytesParser

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
