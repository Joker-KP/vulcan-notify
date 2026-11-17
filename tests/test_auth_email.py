"""Authentication failure alerts: exhaustion, recovery, durable retries and privacy."""

import smtplib
from unittest.mock import AsyncMock, MagicMock

import pytest

from vulcan_notify import __main__ as cli
from vulcan_notify import email
from vulcan_notify.auth import InvalidSessionError
from vulcan_notify.client import SessionExpiredError
from vulcan_notify.config import Settings
from vulcan_notify.differ import Change
from vulcan_notify.models import Student
from vulcan_notify.sync import FullSyncResult, SyncResult, SyncSessionExpiredError


@pytest.fixture
def config(tmp_path, monkeypatch):
    config = Settings(
        _env_file=None,
        db_path=tmp_path / "notify.db",
        session_file=tmp_path / "session.json",
        email_enabled=True,
        smtp_host="smtp.example.org",
        email_from="notify@example.org",
        email_to=["parent@example.org"],
        email_ai_summary=True,
        llm_api_key="synthetic-key",
    )
    monkeypatch.setattr(cli, "settings", config)
    monkeypatch.setattr(email, "settings", config)
    monkeypatch.setattr(email, "summarize", AsyncMock(side_effect=AssertionError("no AI")))
    return config


@pytest.fixture
def smtp(monkeypatch):
    connection = MagicMock()
    connection.send_message.return_value = {}
    monkeypatch.setattr(email.smtplib, "SMTP", MagicMock(return_value=connection))
    monkeypatch.setattr(email.smtplib, "SMTP_SSL", MagicMock(return_value=connection))
    return connection


@pytest.mark.parametrize("reason", email._AUTH_FAILURE_REASONS)
def test_alert_has_shared_layout_and_complete_recovery_in_both_alternatives(config, reason):
    plain, html = email.format_auth_failure(reason, config)
    assert '<html lang="pl">' in html
    assert "background:#f1f5f9" in html
    for content in (plain, html):
        for instruction in (
            "docker compose stop vulcan-sync",
            "docker compose --profile auth up vulcan-auth",
            "ssh -N -L 6080:127.0.0.1:6080 USER@HOST",
            "http://127.0.0.1:6080/vnc.html",
            "Dziennik",
            "5 minut",
            "/app/data/session.json",
            "/app/data/chromium-profile",
            "docker compose --profile auth stop vulcan-auth",
            "docker compose start vulcan-sync",
            "uv run vulcan-notify auth",
            "uprawnienia",
        ):
            assert instruction in content
    assert "Wykryto:" in plain
    assert "USER@HOST" in plain  # Decoded angle brackets/entities never damage commands.


@pytest.mark.parametrize(
    "session_error", [FileNotFoundError, InvalidSessionError, PermissionError, None]
)
@pytest.mark.parametrize("login_error", [RuntimeError, PermissionError])
async def test_exhausted_recovery_sends_alert_without_error_details(
    config, smtp, monkeypatch, caplog, capsys, session_error, login_error
):
    monkeypatch.setattr(
        cli, "load_session", MagicMock(side_effect=session_error, return_value={"fixture": True})
    )
    monkeypatch.setattr(cli, "test_session", AsyncMock(return_value=False))
    monkeypatch.setattr(cli, "_get_credentials", MagicMock(return_value=("fixture", "fixture")))
    automatic = AsyncMock(side_effect=login_error("password=cookie=private-value"))
    interactive = AsyncMock()
    monkeypatch.setattr(cli, "auto_login", automatic)
    monkeypatch.setattr(cli, "login_and_save_session", interactive)
    with pytest.raises(SystemExit, match="1"):
        await cli._ensure_session()
    automatic.assert_awaited_once()
    interactive.assert_not_awaited()
    smtp.send_message.assert_called_once()
    message = smtp.send_message.call_args.args[0]
    assert str(message["Subject"]) == "[eduVulcan] Błąd logowania — wymagana nowa sesja"
    assert message.get_body(("plain",)) and message.get_body(("html",))
    assert "private-value" not in message.as_string() + caplog.text + capsys.readouterr().out
    email.summarize.assert_not_awaited()


@pytest.mark.parametrize("valid_saved_session", [False, True])
async def test_successful_session_reuse_or_automatic_login_sends_no_alert(
    config, smtp, monkeypatch, valid_saved_session
):
    session = {"fixture": True}
    monkeypatch.setattr(cli, "load_session", MagicMock(return_value=session))
    monkeypatch.setattr(cli, "test_session", AsyncMock(return_value=valid_saved_session))
    monkeypatch.setattr(cli, "_get_credentials", MagicMock(return_value=("fixture", "fixture")))
    automatic = AsyncMock(return_value=session)
    monkeypatch.setattr(cli, "auto_login", automatic)
    assert await cli._ensure_session() == session
    assert automatic.await_count == (0 if valid_saved_session else 1)
    smtp.send_message.assert_not_called()


async def test_missing_credentials_alert_explains_profile_was_not_attempted(
    config, smtp, monkeypatch
):
    monkeypatch.setattr(cli, "load_session", MagicMock(side_effect=FileNotFoundError))
    monkeypatch.setattr(cli, "_get_credentials", MagicMock(return_value=None))
    automatic = AsyncMock()
    monkeypatch.setattr(cli, "auto_login", automatic)
    with pytest.raises(SystemExit, match="1"):
        await cli._ensure_session()
    automatic.assert_not_awaited()
    assert "profil Chromium nie był uruchamiany" in (
        smtp.send_message.call_args.args[0].get_body(("plain",)).get_content()
    )


async def test_alert_is_deduplicated_across_restarts_then_rearmed_after_recovery(
    db, config, smtp, monkeypatch
):
    config.email_to = ["first@example.org", "second@example.org", "first@example.org"]
    for _ in range(2):
        await email.publish_auth_failure(db, "recovery_failed")
        await db.close()
        await db.connect()
    assert smtp.send_message.call_count == 2
    assert await db.list_email_outbox() == []
    first_identity = await db.get_state(email.AUTH_FAILURE_STATE)

    # The normal CLI sync resets the persisted outage even when there are no changes.
    result = FullSyncResult([SyncResult(Student("key", "Student", "1A", "School", 1, ""))])
    monkeypatch.setattr(cli, "Database", MagicMock(return_value=db))
    monkeypatch.setattr(cli, "_ensure_session", AsyncMock(return_value={}))
    monkeypatch.setattr(cli, "VulcanClient", MagicMock(return_value=MagicMock(close=AsyncMock())))
    monkeypatch.setattr(cli, "sync_all", AsyncMock(return_value=result))
    monkeypatch.setattr(cli, "_sync_calendar", AsyncMock())
    monkeypatch.setattr(cli, "publish_changes", AsyncMock())
    await cli.cmd_sync()
    await db.connect()
    assert not await db.get_state(email.AUTH_FAILURE_STATE)
    await email.publish_auth_failure(db, "recovery_failed")
    assert smtp.send_message.call_count == 4
    assert await db.get_state(email.AUTH_FAILURE_STATE) != first_identity


async def test_smtp_failure_retries_stored_alert_without_new_email_or_ai(db, config, smtp, caplog):
    smtp.send_message.side_effect = smtplib.SMTPException("private-value")
    await email.publish_auth_failure(db, "recovery_failed")
    first = (await db.list_email_outbox())[0]
    await db.close()
    await db.connect()
    await email.publish_auth_failure(db, "credentials_missing")
    pending = await db.list_email_outbox()
    assert len(pending) == 1
    cursor = await db.db.execute("SELECT attempts FROM email_outbox")
    assert (await cursor.fetchone())[0] == 2
    assert pending[0]["body"] == first["body"]
    assert pending[0]["html_body"] == first["html_body"]
    assert pending[0]["message_id"] == first["message_id"]
    assert "private-value" not in caplog.text
    smtp.send_message.side_effect = None
    assert await email.drain_email_outbox(db) == (1, 0)
    assert smtp.send_message.call_args.args[0]["Message-ID"] == first["message_id"]
    email.summarize.assert_not_awaited()


async def test_email_disabled_does_not_open_database_or_smtp(config, smtp, monkeypatch):
    config.email_enabled = False
    database = MagicMock()
    monkeypatch.setattr(cli, "Database", database)
    await cli._email_auth_status("recovery_failed")
    database.assert_not_called()
    smtp.send_message.assert_not_called()


async def test_database_failure_keeps_auth_failure_exit_and_safe_logs(
    config, smtp, monkeypatch, caplog
):
    monkeypatch.setattr(cli, "auto_login", AsyncMock(side_effect=RuntimeError("private-value")))
    database = MagicMock(
        connect=AsyncMock(side_effect=PermissionError("private-value")), close=AsyncMock()
    )
    monkeypatch.setattr(cli, "Database", MagicMock(return_value=database))
    with pytest.raises(SystemExit, match="1"):
        await cli._recover_session("fixture", "fixture")
    database.close.assert_awaited_once()
    assert "private-value" not in caplog.text
    smtp.send_message.assert_not_called()


@pytest.mark.parametrize("partial_retry", [False, True])
async def test_mid_sync_recovery_that_expires_again_preserves_changes_and_sends_alert(
    db, config, smtp, monkeypatch, partial_retry
):
    student = Student("key", "Student", "1A", "School", 1, "")
    partial = FullSyncResult(
        [SyncResult(student, new_grades=[Change("new", "grade", "Student", "Math: 5", "")])]
    )
    retry_error = (
        SyncSessionExpiredError("private-value", partial)
        if partial_retry
        else SessionExpiredError("private-value")
    )
    monkeypatch.setattr(cli, "Database", MagicMock(return_value=db))
    monkeypatch.setattr(cli, "_ensure_session", AsyncMock(return_value={}))
    monkeypatch.setattr(cli, "VulcanClient", MagicMock(return_value=MagicMock(close=AsyncMock())))
    monkeypatch.setattr(cli, "_get_credentials", MagicMock(return_value=("fixture", "fixture")))
    monkeypatch.setattr(cli, "_recover_session", AsyncMock(return_value={}))
    monkeypatch.setattr(cli, "_sync_calendar", AsyncMock())
    monkeypatch.setattr(cli, "publish_changes", AsyncMock())
    monkeypatch.setattr(cli, "publish_email", AsyncMock())
    monkeypatch.setattr(
        cli, "sync_all", AsyncMock(side_effect=[SessionExpiredError("expired"), retry_error])
    )
    with pytest.raises(SystemExit, match="1"):
        await cli.cmd_sync()
    assert cli.publish_email.await_count == int(partial_retry)
    if partial_retry:
        cli.publish_email.assert_awaited_once_with(partial, db)
        cli.publish_changes.assert_awaited_once_with(partial, db)
    smtp.send_message.assert_called_once()
    assert "Sesja ponownie wygasła" in (
        smtp.send_message.call_args.args[0].get_body(("plain",)).get_content()
    )
    await db.connect()
    assert await db.get_state(email.AUTH_FAILURE_STATE)


async def test_interactive_failure_alert_and_success_reset(db, config, smtp, monkeypatch):
    monkeypatch.setattr(cli, "Database", MagicMock(return_value=db))
    monkeypatch.setattr(
        cli, "login_and_save_session", AsyncMock(side_effect=PermissionError("private-value"))
    )
    with pytest.raises(SystemExit, match="1"):
        await cli.cmd_auth()
    assert "Logowanie interaktywne" in (
        smtp.send_message.call_args.args[0].get_body(("plain",)).get_content()
    )
    monkeypatch.setattr(cli, "login_and_save_session", AsyncMock(return_value={}))
    await cli.cmd_auth()
    await db.connect()
    assert not await db.get_state(email.AUTH_FAILURE_STATE)
    smtp.send_message.assert_called_once()
