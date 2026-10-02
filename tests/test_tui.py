"""TUI startup and optional dependency error reporting."""

import builtins

import pytest

from vulcan_notify import __main__ as cli


@pytest.mark.parametrize(
    "error",
    [
        ImportError("broken application import"),
        ModuleNotFoundError("missing dependency", name="other"),
        ModuleNotFoundError("incompatible textual", name="textual.missing_module"),
    ],
)
async def test_tui_does_not_report_unrelated_import_errors_as_missing_textual(monkeypatch, error):
    original = builtins.__import__

    def import_module(name, *args, **kwargs):
        if name == "vulcan_notify.tui":
            raise error
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_module)
    with pytest.raises(type(error), match=str(error)):
        await cli.cmd_tui()


async def test_tui_explains_how_to_install_missing_textual(monkeypatch, capsys):
    original = builtins.__import__

    def import_module(name, *args, **kwargs):
        if name == "vulcan_notify.tui":
            raise ModuleNotFoundError("missing textual", name="textual")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_module)
    with pytest.raises(SystemExit) as exc:
        await cli.cmd_tui()
    assert exc.value.code == 1
    assert "uv sync --extra tui" in capsys.readouterr().out


async def test_tui_starts_and_opens_html_message_from_local_database(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import DataTable

    from vulcan_notify.models import Message
    from vulcan_notify.tui import DetailScreen, MainScreen, VulcanTuiApp

    monkeypatch.setattr(cli.settings, "db_path", tmp_path / "tui.db")
    app = VulcanTuiApp()
    await app.db.connect()
    try:
        await app.db.upsert_message(
            Message(
                id=1,
                subject="Example message",
                sender="Example Teacher",
                date="2000-01-01",
                content="<p>Example &amp; text</p>",
                api_global_key="example-message",
                mailbox="Example",
                has_attachments=False,
                is_read=True,
            )
        )
        await app.db.commit()
    finally:
        await app.db.close()
    async with app.run_test(size=(100, 35)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
        table = app.screen.query_one("#messages-table", DataTable)
        assert table.row_count == 1
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        assert app.screen._body == "Example & text"
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
