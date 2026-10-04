"""Local auth decisions; these mocks do not validate live eduVULCAN login."""

import asyncio
import json
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from vulcan_notify import __main__ as cli
from vulcan_notify import auth


@pytest.fixture(autouse=True)
def disable_live_email(monkeypatch):
    # Authentication unit tests must never use the developer's SMTP configuration.
    monkeypatch.setattr(cli.settings, "email_enabled", False)


async def test_profile_picker_selects_first_and_dismisses_overlays(monkeypatch):
    monkeypatch.setenv("VULCAN_STUDENT", "old preference")
    page = MagicMock()
    overlays = MagicMock()
    overlays.first.count = AsyncMock(return_value=1)
    overlays.first.click = AsyncMock()
    profiles = MagicMock()
    profiles.count = AsyncMock(return_value=2)
    profiles.first.click = AsyncMock()
    page.locator.side_effect = lambda selector: profiles if selector.startswith("a[") else overlays

    assert await auth._open_student_profile(page)
    profiles.first.click.assert_awaited_once_with(timeout=30000)
    assert overlays.first.click.await_count == 2


async def test_profile_picker_missing_profiles_returns_false():
    page = MagicMock()
    page.locator.return_value.count = AsyncMock(return_value=0)
    page.locator.return_value.first.count = AsyncMock(return_value=0)
    assert await auth._open_student_profile(page) is False


async def test_cookie_import_preserves_session_file(tmp_path):
    path = tmp_path / "session.json"
    content = json.dumps({"cookies": [{"name": "test", "value": "fixture"}]})
    path.write_text(content)
    context = AsyncMock()
    await auth._seed_context_from_session(context, path)
    context.add_cookies.assert_awaited_once_with([{"name": "test", "value": "fixture"}])
    assert path.read_text() == content


async def test_persistent_context_can_force_headed_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("VULCAN_BROWSER_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("VULCAN_BROWSER_HEADLESS", "true")
    playwright = MagicMock()
    playwright.chromium.launch_persistent_context = AsyncMock()
    await auth._launch_browser_context(playwright, force_headless=False)
    kwargs = playwright.chromium.launch_persistent_context.call_args.kwargs
    assert kwargs["user_data_dir"] == str(tmp_path / "profile")
    assert kwargs["headless"] is False
    assert kwargs["timezone_id"] == auth.settings.tz


@pytest.mark.parametrize("interactive", [False, True])
async def test_browser_reuse_precedes_credentials_or_manual_login(monkeypatch, interactive):
    context = MagicMock()
    context.close = AsyncMock()
    page = AsyncMock()
    monkeypatch.setattr(auth, "_browser_profile_lock", lambda: nullcontext())
    monkeypatch.setattr(auth, "_cleanup_chromium_singleton_locks", MagicMock())
    monkeypatch.setattr(auth, "async_playwright", MagicMock())
    launch = AsyncMock(return_value=context)
    monkeypatch.setattr(auth, "_launch_browser_context", launch)
    monkeypatch.setattr(auth, "_attach_navigation_tracking", MagicMock())
    monkeypatch.setattr(auth, "_seed_context_from_session", AsyncMock())
    monkeypatch.setattr(auth, "_get_page", MagicMock(return_value=page))
    reuse = AsyncMock(return_value=True)
    monkeypatch.setattr(auth, "_try_reuse_browser_session", reuse)
    monkeypatch.setattr(auth, "_save_current_session", AsyncMock(return_value={"reused": True}))

    if interactive:
        result = await auth.login_and_save_session(Path("unused-session.json"))
        assert launch.call_args.kwargs["force_headless"] is False
    else:
        result = await auth.auto_login(Path("unused-session.json"), "fixture", "fixture")
    assert result == {"reused": True}
    reuse.assert_awaited_once()
    page.goto.assert_not_awaited()
    context.close.assert_awaited_once()


async def test_session_validation_has_bounded_timeout(monkeypatch):
    response = MagicMock(status=200, headers={"content-type": "application/json"})
    response.text = AsyncMock(return_value='{"uczniowie": []}')
    client = MagicMock()
    client.get.return_value.__aenter__ = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(auth.aiohttp, "ClientSession", factory)
    assert await auth.test_session({"base_url": "https://example.test", "cookies": []})
    timeout = factory.call_args.kwargs["timeout"]
    assert (timeout.total, timeout.connect) == (30, 10)


def mock_session_responses(monkeypatch, *outcomes):
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    requests = []
    for outcome in outcomes:
        request = MagicMock()
        request.__aenter__ = (
            AsyncMock(side_effect=outcome)
            if isinstance(outcome, Exception)
            else AsyncMock(return_value=outcome)
        )
        requests.append(request)
    client.get.side_effect = requests
    monkeypatch.setattr(auth.aiohttp, "ClientSession", MagicMock(return_value=client))
    monkeypatch.setattr(auth.asyncio, "sleep", AsyncMock())
    return client


def session_response(status=200, content_type="application/json", body='{"uczniowie": []}'):
    response = MagicMock(status=status, headers={"content-type": content_type})
    response.text = AsyncMock(return_value=body)
    return response


@pytest.mark.parametrize(
    "first",
    [
        TimeoutError("private detail"),
        auth.aiohttp.ClientConnectionError("private detail"),
        500,
        429,
    ],
)
async def test_session_validation_retries_transient_failure_then_reuses_session(monkeypatch, first):
    if isinstance(first, int):
        first = session_response(first, "text/html", "upstream fault")
    client = mock_session_responses(monkeypatch, first, session_response())
    assert await auth.test_session({"base_url": "https://example.test", "cookies": []})
    assert client.get.call_count == 2


@pytest.mark.parametrize("failure", ["timeout", "server", "malformed", "unexpected_json"])
async def test_unavailable_validation_is_not_session_expiry(monkeypatch, caplog, failure):
    outcome = {
        "timeout": TimeoutError("private detail"),
        "server": session_response(503, "text/html", "private detail"),
        "malformed": session_response(body="private detail"),
        "unexpected_json": session_response(body="null"),
    }[failure]
    client = mock_session_responses(monkeypatch, outcome, outcome)
    with pytest.raises(auth.SessionValidationError):
        await auth.test_session({"base_url": "https://example.test", "cookies": []})
    assert client.get.call_count == 2
    assert "private detail" not in caplog.text


@pytest.mark.parametrize("status", [400, 404, 429, 500, 502, 503])
async def test_validation_logs_http_status_without_response_body(monkeypatch, caplog, status):
    response = session_response(status, "text/html", "password=cookie=private-detail")
    mock_session_responses(monkeypatch, response, response)
    with pytest.raises(auth.SessionValidationError) as failure:
        await auth.test_session({"base_url": "https://example.test", "cookies": []})
    assert f"HTTP {status}" in caplog.text
    assert f"HTTP {status}" in str(failure.value)
    assert "private-detail" not in caplog.text + str(failure.value)


@pytest.mark.parametrize(
    "body,json_type", [("null", "NoneType"), ("[]", "list"), ('"private-detail"', "str")]
)
async def test_validation_logs_json_type_without_json_content(monkeypatch, caplog, body, json_type):
    response = session_response(body=body)
    mock_session_responses(monkeypatch, response, response)
    with pytest.raises(auth.SessionValidationError) as failure:
        await auth.test_session({"base_url": "https://example.test", "cookies": []})
    assert f"JSON {json_type}" in caplog.text
    assert f"JSON {json_type}" in str(failure.value)
    assert "private-detail" not in caplog.text + str(failure.value)


@pytest.mark.parametrize(
    "status,content_type", [(401, "text/html"), (403, "text/html"), (200, "text/html")]
)
async def test_confirmed_session_expiry_does_not_retry(monkeypatch, status, content_type):
    client = mock_session_responses(monkeypatch, session_response(status, content_type))
    assert not await auth.test_session({"base_url": "https://example.test", "cookies": []})
    assert client.get.call_count == 1


@pytest.mark.parametrize("content_type", ["application/json", "text/html"])
async def test_context_conflict_requests_recovery_without_http_retry(
    monkeypatch, caplog, content_type
):
    client = mock_session_responses(
        monkeypatch, session_response(409, content_type, "private-detail")
    )
    assert not await auth.test_session({"base_url": "https://example.test", "cookies": []})
    assert client.get.call_count == 1
    assert "HTTP 409" in caplog.text
    assert "private-detail" not in caplog.text


async def test_cli_recovers_saved_session_after_context_conflict(monkeypatch):
    saved = {"base_url": "https://example.test", "cookies": []}
    mock_session_responses(monkeypatch, session_response(409))
    monkeypatch.setattr(cli, "load_session", MagicMock(return_value=saved))
    monkeypatch.setattr(cli, "_get_credentials", MagicMock(return_value=("fixture", "fixture")))
    automatic = AsyncMock(return_value={"recovered": True})
    monkeypatch.setattr(cli, "auto_login", automatic)
    monkeypatch.setattr(cli, "_email_auth_status", AsyncMock())

    assert await cli._ensure_session() == {"recovered": True}
    automatic.assert_awaited_once()
    cli._email_auth_status.assert_not_awaited()


async def test_repeated_context_conflict_stops_after_one_sync_recovery(db, monkeypatch):
    from vulcan_notify.client import VulcanClient

    state = {"base_url": "https://example.test", "cookies": []}
    clients = []
    for _ in range(2):
        client = VulcanClient(state)
        client._jitter = AsyncMock()
        http = MagicMock(closed=False, close=AsyncMock())
        http.get.return_value.__aenter__ = AsyncMock(return_value=session_response(409))
        client._http = http
        clients.append(client)

    monkeypatch.setattr(cli, "_ensure_session", AsyncMock(return_value=state))
    monkeypatch.setattr(cli, "Database", MagicMock(return_value=db))
    monkeypatch.setattr(cli, "VulcanClient", MagicMock(side_effect=clients))
    monkeypatch.setattr(cli, "_get_credentials", MagicMock(return_value=("fixture", "fixture")))
    recovery = AsyncMock(return_value=state)
    alert = AsyncMock()
    monkeypatch.setattr(cli, "_recover_session", recovery)
    monkeypatch.setattr(cli, "publish_auth_failure", alert)
    monkeypatch.setattr(cli, "publish_email", AsyncMock())
    monkeypatch.setattr(cli, "publish_changes", AsyncMock())
    monkeypatch.setattr(cli, "_sync_calendar", AsyncMock())

    with pytest.raises(SystemExit, match="1"):
        await cli.cmd_sync()
    recovery.assert_awaited_once_with("fixture", "fixture")
    alert.assert_awaited_once_with(db, "session_expired")
    for client in clients:
        client._http.get.assert_called_once()
        client._http.close.assert_awaited_once()


@pytest.mark.parametrize("command", ["_ensure_session", "cmd_test"])
async def test_unavailable_session_check_preserves_state_without_auth_alert(
    tmp_path, monkeypatch, capsys, command
):
    path = tmp_path / "session.json"
    original = '{"base_url": "https://example.test", "cookies": []}'
    path.write_text(original)
    monkeypatch.setattr(cli.settings, "session_file", path)
    monkeypatch.setattr(cli, "test_session", AsyncMock(side_effect=auth.SessionValidationError()))
    automatic = AsyncMock()
    alert = AsyncMock()
    monkeypatch.setattr(cli, "auto_login", automatic)
    monkeypatch.setattr(cli, "_email_auth_status", alert)
    monkeypatch.setattr(cli, "_get_credentials", MagicMock(return_value=("fixture", "fixture")))

    with pytest.raises(SystemExit, match="1"):
        await getattr(cli, command)()

    assert "validation unavailable" in capsys.readouterr().out
    assert path.read_text() == original
    automatic.assert_not_awaited()
    alert.assert_not_awaited()


@pytest.mark.parametrize("click_timeout", [False, True])
async def test_login_accepts_portal_redirect_without_observing_post(click_timeout):
    page = MagicMock(url=auth.EDUVULCAN_LOGIN_URL)
    button = MagicMock(click=AsyncMock())
    if click_timeout:
        button.click.side_effect = PlaywrightTimeoutError("fixture navigation timeout")

    async def redirect(predicate, **kwargs):
        assert not predicate(auth.EDUVULCAN_LOGIN_URL)
        assert not predicate("https://dziennik-logowanie.vulcan.net.pl/fixture")
        assert not predicate("https://unrelated.test/")
        page.url = auth.EDUVULCAN_HOME_URL
        assert predicate(page.url)

    page.wait_for_url = AsyncMock(side_effect=redirect)
    await auth._submit_login_form(page, button, asyncio.Event())
    button.click.assert_awaited_once()
    page.expect_response.assert_not_called()


async def test_login_rejection_does_not_become_success():
    page = MagicMock(url=auth.EDUVULCAN_LOGIN_URL)
    page.wait_for_url = AsyncMock(side_effect=PlaywrightTimeoutError("private detail"))
    button = MagicMock(click=AsyncMock())
    with pytest.raises(RuntimeError, match="did not reach"):
        await auth._submit_login_form(page, button, asyncio.Event())
    button.click.assert_awaited_once()


async def test_login_recognizes_student_journal_in_another_tab():
    complete = asyncio.Event()
    page = MagicMock(url=auth.EDUVULCAN_LOGIN_URL)

    async def redirect(*args, **kwargs):
        complete.set()
        raise PlaywrightTimeoutError("original tab did not navigate")

    page.wait_for_url = AsyncMock(side_effect=redirect)
    await auth._submit_login_form(page, MagicMock(click=AsyncMock()), complete)


async def test_auto_login_continues_from_portal_to_journal_without_login_post(
    monkeypatch, tmp_path
):
    context = MagicMock(close=AsyncMock())
    page = MagicMock(url=auth.EDUVULCAN_LOGIN_URL)
    page.goto = AsyncMock()
    field = MagicMock(
        count=AsyncMock(return_value=1), wait_for=AsyncMock(), fill=AsyncMock(), click=AsyncMock()
    )
    page.locator.return_value = field
    tracking = {}

    def attach(ctx, complete, dashboard):
        tracking.update(complete=complete, dashboard=dashboard)

    async def redirect(predicate, **kwargs):
        page.url = auth.EDUVULCAN_HOME_URL
        assert predicate(page.url)

    async def open_profile(*args):
        tracking["dashboard"]["url"] = "https://uczen.eduvulcan.pl/fixture/App/profile/tablica"
        tracking["complete"].set()
        return True

    page.wait_for_url = AsyncMock(side_effect=redirect)
    monkeypatch.setattr(auth, "_browser_profile_lock", lambda: nullcontext())
    monkeypatch.setattr(auth, "_cleanup_chromium_singleton_locks", MagicMock())
    monkeypatch.setattr(auth, "async_playwright", MagicMock())
    monkeypatch.setattr(auth, "_launch_browser_context", AsyncMock(return_value=context))
    monkeypatch.setattr(auth, "_attach_navigation_tracking", attach)
    monkeypatch.setattr(auth, "_seed_context_from_session", AsyncMock())
    monkeypatch.setattr(auth, "_get_page", MagicMock(return_value=page))
    monkeypatch.setattr(auth, "_try_reuse_browser_session", AsyncMock(return_value=False))
    monkeypatch.setattr(auth, "_accept_cookie_banner", AsyncMock())
    monkeypatch.setattr(auth, "_wait_for_captcha", AsyncMock())
    monkeypatch.setattr(auth, "_get_login_delay_seconds", lambda: 0)
    monkeypatch.setattr(auth.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(auth, "_open_student_profile", AsyncMock(side_effect=open_profile))
    save = AsyncMock(return_value={"recovered": True})
    monkeypatch.setattr(auth, "_save_current_session", save)

    assert await auth.auto_login(tmp_path / "session.json", "fixture", "fixture") == {
        "recovered": True
    }
    page.expect_response.assert_not_called()
    assert page.goto.call_args.args[0] == auth.JOURNAL_ACCESS_URL
    assert save.call_args.args[3] == tracking["dashboard"]["url"]
    context.close.assert_awaited_once()


async def test_valid_http_session_never_launches_browser(monkeypatch):
    session = {"fixture": True}
    monkeypatch.setattr(cli, "load_session", MagicMock(return_value=session))
    monkeypatch.setattr(cli, "test_session", AsyncMock(return_value=True))
    automatic = AsyncMock()
    interactive = AsyncMock()
    monkeypatch.setattr(cli, "auto_login", automatic)
    monkeypatch.setattr(cli, "login_and_save_session", interactive)
    assert await cli._ensure_session() == session
    automatic.assert_not_awaited()
    interactive.assert_not_awaited()


async def test_missing_credentials_requires_explicit_manual_auth(monkeypatch):
    monkeypatch.setattr(cli, "load_session", MagicMock(side_effect=FileNotFoundError))
    monkeypatch.setattr(cli, "_get_credentials", MagicMock(return_value=None))
    automatic = AsyncMock()
    interactive = AsyncMock()
    monkeypatch.setattr(cli, "auto_login", automatic)
    monkeypatch.setattr(cli, "login_and_save_session", interactive)
    with pytest.raises(SystemExit, match="1"):
        await cli._ensure_session()
    automatic.assert_not_awaited()
    interactive.assert_not_awaited()


def test_profile_lock_excludes_other_auth_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("VULCAN_BROWSER_LOCK_FILE", str(tmp_path / "profile.lock"))
    with (
        auth._browser_profile_lock(),
        pytest.raises(RuntimeError, match="currently being used"),
        auth._browser_profile_lock(timeout=0),
    ):
        pass
    with auth._browser_profile_lock(timeout=0):
        pass


async def test_cli_publishes_partial_changes_before_reauth_retry(monkeypatch):
    from vulcan_notify.models import Student
    from vulcan_notify.sync import FullSyncResult, SyncResult, SyncSessionExpiredError

    partial = FullSyncResult([])
    completed = FullSyncResult([SyncResult(Student("fixture", "Student", "1A", "School", 1, ""))])
    client = MagicMock(close=AsyncMock())
    database = MagicMock(connect=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(cli, "_ensure_session", AsyncMock(return_value={}))
    monkeypatch.setattr(cli, "VulcanClient", MagicMock(return_value=client))
    monkeypatch.setattr(cli, "Database", MagicMock(return_value=database))
    monkeypatch.setattr(cli, "_get_credentials", MagicMock(return_value=("fixture", "fixture")))
    monkeypatch.setattr(cli, "_sync_calendar", AsyncMock())
    monkeypatch.setattr(cli, "format_compact_sync", MagicMock(return_value="fixture"))
    events = []

    async def publish(result, db):
        events.append(("publish", result))

    async def recover(*args):
        events.append(("recover", None))
        return {}

    monkeypatch.setattr(cli, "publish_changes", publish)
    monkeypatch.setattr(cli, "auto_login", recover)
    monkeypatch.setattr(
        cli,
        "sync_all",
        AsyncMock(
            side_effect=[
                SyncSessionExpiredError("expired", partial),
                completed,
            ]
        ),
    )
    await cli.cmd_sync()
    assert events == [("publish", partial), ("recover", None), ("publish", completed)]


async def test_failed_automatic_recovery_requests_explicit_interactive_auth(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_session", MagicMock(side_effect=FileNotFoundError))
    monkeypatch.setattr(cli, "_get_credentials", MagicMock(return_value=("fixture", "fixture")))
    monkeypatch.setattr(cli, "auto_login", AsyncMock(side_effect=RuntimeError("private detail")))
    interactive = AsyncMock()
    monkeypatch.setattr(cli, "login_and_save_session", interactive)
    with pytest.raises(SystemExit, match="1"):
        await cli._ensure_session()
    output = capsys.readouterr().out
    assert "vulcan-auth" in output
    assert "private detail" not in output
    interactive.assert_not_awaited()


async def test_degraded_sync_publishes_successful_changes_then_exits_nonzero(monkeypatch):
    from vulcan_notify.models import Student
    from vulcan_notify.sync import FullSyncResult, SyncResult

    result = FullSyncResult(
        [
            SyncResult(
                Student("fixture", "Student", "1A", "School", 1, ""),
                failed_sections={"grades": "fixture failure"},
            ),
        ]
    )
    monkeypatch.setattr(cli, "_ensure_session", AsyncMock(return_value={}))
    monkeypatch.setattr(cli, "VulcanClient", MagicMock(return_value=MagicMock(close=AsyncMock())))
    monkeypatch.setattr(
        cli, "Database", MagicMock(return_value=MagicMock(connect=AsyncMock(), close=AsyncMock()))
    )
    monkeypatch.setattr(cli, "sync_all", AsyncMock(return_value=result))
    monkeypatch.setattr(cli, "_sync_calendar", AsyncMock())
    publish = AsyncMock()
    monkeypatch.setattr(cli, "publish_changes", publish)
    with pytest.raises(SystemExit, match="1"):
        await cli.cmd_sync()
    publish.assert_awaited_once()


@pytest.mark.parametrize(
    "content",
    [
        '{"private":',
        "[]",
        "{}",
        '{"base_url": 1, "cookies": []}',
        '{"base_url": "https://example.test", "cookies": {}}',
        '{"base_url": "https://example.test", "cookies": [{"name": "private-value"}]}',
        '{"base_url": "https://example.test", "cookies": [null]}',
        '{"base_url": "https://[invalid", "cookies": []}',
    ],
)
def test_invalid_session_files_have_safe_errors(tmp_path, content):
    path = tmp_path / "session.json"
    path.write_text(content)
    with pytest.raises(auth.InvalidSessionError) as failure:
        auth.load_session(path)
    assert "private-value" not in str(failure.value)
    assert path.read_text() == content


def test_atomic_session_replacement_is_private_and_loadable(tmp_path):
    path = tmp_path / "session.json"
    path.write_text("old state")
    path.chmod(0o644)
    session = {
        "base_url": "https://example.test",
        "cookies": [{"name": "fixture", "value": "fixture", "domain": "example.test"}],
    }
    auth._write_session(path, session)
    assert auth.load_session(path) == session
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("operation", ["replace", "fsync"])
def test_failed_atomic_session_write_preserves_previous_file(tmp_path, monkeypatch, operation):
    path = tmp_path / "session.json"
    original = '{"base_url": "https://example.test", "cookies": []}'
    path.write_text(original)
    monkeypatch.setattr(auth.os, operation, MagicMock(side_effect=OSError("fixture failure")))
    with pytest.raises(OSError):
        auth._write_session(path, {"base_url": "https://example.test", "cookies": []})
    assert path.read_text() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("credentials", [None, ("fixture", "fixture")])
async def test_corrupt_session_uses_existing_recovery_policy(
    tmp_path, monkeypatch, credentials, capsys
):
    path = tmp_path / "session.json"
    path.write_text('{"private-value":')
    monkeypatch.setattr(cli.settings, "session_file", path)
    monkeypatch.setattr(cli, "_get_credentials", MagicMock(return_value=credentials))
    recovery = AsyncMock(return_value={"recovered": True})
    monkeypatch.setattr(cli, "_recover_session", recovery)
    interactive = AsyncMock()
    monkeypatch.setattr(cli, "login_and_save_session", interactive)
    if credentials:
        assert await cli._ensure_session() == {"recovered": True}
        recovery.assert_awaited_once_with(*credentials)
    else:
        with pytest.raises(SystemExit, match="1"):
            await cli._ensure_session()
        recovery.assert_not_awaited()
    assert "private-value" not in capsys.readouterr().out
    interactive.assert_not_awaited()
    assert path.read_text() == '{"private-value":'


async def test_test_command_handles_corrupt_session_without_traceback(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "session.json"
    path.write_text("private-value")
    monkeypatch.setattr(cli.settings, "session_file", path)
    with pytest.raises(SystemExit, match="1"):
        await cli.cmd_test()
    output = capsys.readouterr().out
    assert "vulcan-notify auth" in output
    assert "private-value" not in output
