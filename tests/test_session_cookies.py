"""Exported browser cookies must produce the same scoped HTTP request."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from vulcan_notify import auth
from vulcan_notify.client import VulcanClient


def cookie(value, **fields):
    return {
        "name": "session",
        "value": value,
        "domain": "uczen.eduvulcan.pl",
        "path": "/district",
        **fields,
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_other_tenant_cannot_overwrite_current_cookie(reverse):
    cookies = [cookie("current"), cookie("other", path="/another")]
    if reverse:
        cookies.reverse()
    state = {"cookies": cookies}
    url = "https://uczen.eduvulcan.pl/district/api/Context"
    assert auth.cookie_header_for_url(state, url) == "session=current"
    assert auth.cookies_for_url(state, url) == {"session": "current"}


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/district", True),
        ("/district/", True),
        ("/district/api/Context", True),
        ("/district-other/api/Context", False),
        ("/other/district/api/Context", False),
    ],
)
def test_cookie_path_requires_a_path_boundary(path, expected):
    header = auth.cookie_header_for_url(
        {"cookies": [cookie("current")]}, f"https://uczen.eduvulcan.pl{path}"
    )
    assert bool(header) is expected


@pytest.mark.parametrize(
    "domain,expected",
    [
        ("uczen.eduvulcan.pl", True),
        (".eduvulcan.pl", True),
        ("eduvulcan.pl", False),
        ("wiadomosci.eduvulcan.pl", False),
        (".vulcan.pl", False),
        ("UCZEN.EDUVULCAN.PL", True),
    ],
)
def test_browser_host_only_and_domain_cookies(domain, expected):
    header = auth.cookie_header_for_url(
        {"cookies": [cookie("current", domain=domain)]},
        "https://uczen.eduvulcan.pl/district/api/Context",
    )
    assert bool(header) is expected


def test_expiry_secure_and_legacy_cookie_metadata(monkeypatch):
    monkeypatch.setattr(auth.time, "time", lambda: 100)
    state = {
        "cookies": [
            cookie("expired", expires=99, name="expired"),
            cookie("boundary", expires=100, name="boundary"),
            cookie("session", expires=-1, name="session"),
            cookie("future", expires=101, name="future"),
            cookie("secure", secure=True, name="secure"),
            {"name": "legacy", "value": "legacy", "domain": "uczen.eduvulcan.pl"},
        ]
    }
    assert (
        auth.cookie_header_for_url(state, "https://uczen.eduvulcan.pl/district/api/Context")
        == "session=session; future=future; secure=secure; legacy=legacy"
    )
    assert (
        auth.cookie_header_for_url(state, "http://uczen.eduvulcan.pl/district/api/Context")
        == "session=session; future=future; legacy=legacy"
    )


def test_same_name_cookies_keep_path_order_and_stable_export_order():
    state = {
        "cookies": [
            cookie("root", path="/"),
            cookie("tenant"),
            cookie("api-host", path="/district/api"),
            cookie("api-domain", path="/district/api", domain=".eduvulcan.pl"),
        ]
    }
    url = "https://uczen.eduvulcan.pl/district/api/Context"
    assert auth.cookie_header_for_url(state, url) == (
        "session=api-host; session=api-domain; session=tenant; session=root"
    )
    assert auth.cookies_for_url(state, url) == {"session": "api-host"}


async def test_client_uses_the_exact_endpoint_path_for_cookies():
    client = VulcanClient(
        {
            "base_url": "https://uczen.eduvulcan.pl/district",
            "cookies": [
                cookie("context", path="/district/api/Context"),
                cookie("grades", path="/district/api/Oceny"),
            ],
        }
    )
    client._fetch = AsyncMock(return_value={})
    await client._request("/api/Context")
    client._fetch.assert_awaited_once_with(
        "https://uczen.eduvulcan.pl/district/api/Context",
        extra_headers={"Cookie": "session=context"},
    )


async def test_message_requests_preserve_scoped_same_name_cookies():
    client = VulcanClient(
        {
            "base_url": "https://uczen.eduvulcan.pl/district",
            "cookies": [
                cookie("student"),
                cookie("shared", domain=".eduvulcan.pl", path="/"),
                cookie("messages", domain="wiadomosci.eduvulcan.pl"),
            ],
        }
    )
    client._fetch = AsyncMock(return_value={})
    url = "https://wiadomosci.eduvulcan.pl/district/api/Odebrane"
    await client._request_url(url)
    client._fetch.assert_awaited_once_with(
        url, extra_headers={"Cookie": "session=messages; session=shared", "Referer": url}
    )


@pytest.mark.parametrize("outcome", [False, "unavailable"])
async def test_auth_does_not_replace_session_when_exported_api_access_fails(
    tmp_path, monkeypatch, outcome
):
    path = tmp_path / "session.json"
    path.write_text("previous private state")
    context = MagicMock(cookies=AsyncMock(return_value=[cookie("current")]))
    monkeypatch.setattr(auth, "_visit_messages_application", AsyncMock())
    validation = AsyncMock(return_value=False)
    expected_error = auth.InvalidSessionError
    if outcome == "unavailable":
        validation.side_effect = auth.SessionValidationError("HTTP 400")
        expected_error = auth.SessionValidationError
    monkeypatch.setattr(auth, "test_session", validation)
    with pytest.raises(expected_error):
        await auth._save_current_session(
            context, MagicMock(), path, "https://uczen.eduvulcan.pl/district/App/profile"
        )
    assert path.read_text() == "previous private state"
    assert list(tmp_path.iterdir()) == [path]


async def test_auth_validates_and_saves_the_same_exported_cookies(tmp_path, monkeypatch):
    path = tmp_path / "session.json"
    context = MagicMock(cookies=AsyncMock(return_value=[cookie("current")]))
    monkeypatch.setattr(auth, "_visit_messages_application", AsyncMock())

    async def validate(state):
        assert not path.exists()
        assert state["cookies"] == [cookie("current")]
        return True

    validation = AsyncMock(side_effect=validate)
    monkeypatch.setattr(auth, "test_session", validation)
    state = await auth._save_current_session(
        context, MagicMock(), path, "https://uczen.eduvulcan.pl/district/App/profile"
    )
    validation.assert_awaited_once_with(state)
    assert auth.load_session(path) == state
    assert path.stat().st_mode & 0o777 == 0o600
