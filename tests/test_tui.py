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


@pytest.fixture
async def remarks_app(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from vulcan_notify.models import Remark, Student
    from vulcan_notify.tui import VulcanTuiApp

    monkeypatch.setattr(cli.settings, "db_path", tmp_path / "remarks-tui.db")
    app = VulcanTuiApp()
    await app.db.connect()
    try:
        for key, name in (("A", "Anna"), ("B", "Jan"), ("C", "No entries")):
            await app.db.upsert_student(Student(key, name, "3A", "Example School", 1, "mailbox"))
        await app.db.upsert_remark(
            "A", Remark(1, "2000-01-01", "Uwaga", 2, "Teacher A", "<p>Older note</p>", 2)
        )
        await app.db.upsert_remark(
            "B",
            Remark(
                1,
                "2000-01-02T08:00:00+01:00",
                "Pochwała",
                1,
                "Teacher [b]B[/b]",
                "<p>Help &amp; support</p><p>[bold]Literal content[/bold]</p>",
                1,
                0,
                "https://uczen.eduvulcan.pl/example/App/B/pochwalyUwagi",
            ),
        )
        await app.db.upsert_remark(
            "B", Remark(2, "2000-01-03", "Removed", 1, "Teacher", "Hidden", 1)
        )
        await app.db.mark_missing_remarks("B", {1})
        await app.db.commit()
    finally:
        await app.db.close()
    return app


async def test_tui_remarks_navigation_preview_and_detail(remarks_app):
    from textual.widgets import DataTable, Static, TabbedContent

    from vulcan_notify.tui import DetailScreen, MainScreen

    app = remarks_app
    async with app.run_test(size=(120, 35)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("6")
        await app.workers.wait_for_complete()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, MainScreen)
        assert screen.query_one(TabbedContent).active == "tab-remarks"
        table = screen.query_one("#remarks-table", DataTable)
        assert table.row_count == 2  # Soft-deleted records are hidden.
        assert [row["student_name"] for row in screen._data["remarks"]] == ["Jan", "Anna"]
        assert [str(value) for value in table.get_row_at(0)] == [
            "2000-01-02",
            "Jan",
            "Pochwała",
            "Teacher [b]B[/b]",
            "0.0",
            "Help & support [bold]Literal content[/bold]",
        ]
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        assert app.screen._body == "Help & support\n[bold]Literal content[/bold]"
        fields = dict(app.screen._fields)
        assert fields["Student"] == "Jan"
        assert fields["Points"] == "0.0"
        assert fields["Date"] == "2000-01-02T08:00:00+01:00"
        assert fields["Vulcan"].endswith("/App/B/pochwalyUwagi")
        # Textual markup in upstream content and metadata remains literal.
        assert (
            "[bold]Literal content[/bold]"
            in app.screen.query_one(".content", Static).render().plain
        )
        assert "Teacher [b]B[/b]" in app.screen.query_one(".metadata", Static).render().plain
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is screen


async def test_tui_remarks_sorting_student_filter_and_cached_navigation(remarks_app):
    from textual.widgets import DataTable

    app = remarks_app
    async with app.run_test(size=(120, 35)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("6")
        await app.workers.wait_for_complete()
        await pilot.pause()
        screen = app.screen
        table = screen.query_one("#remarks-table", DataTable)
        await pilot.press("o")  # Student ascending.
        assert [row["student_name"] for row in screen._data["remarks"]] == ["Anna", "Jan"]
        await pilot.press("O")
        assert [row["student_name"] for row in screen._data["remarks"]] == ["Jan", "Anna"]
        for name, count in (("Anna", 1), ("Jan", 1), ("No entries", 0), (None, 2)):
            await pilot.press("s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert table.row_count == count
            if name and count:
                assert screen._data["remarks"][0]["student_name"] == name
        await pilot.press("1", "s", "6")  # Change filter elsewhere, revisit loaded tab.
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert table.row_count == 1
        assert screen._data["remarks"][0]["student_name"] == "Anna"
        assert screen._sort_state["remarks"] == (1, True)


async def test_tui_remarks_empty_database_and_help(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Static

    from vulcan_notify.tui import HelpScreen, VulcanTuiApp

    monkeypatch.setattr(cli.settings, "db_path", tmp_path / "empty-tui.db")
    app = VulcanTuiApp()
    async with app.run_test(size=(100, 35)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("6")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen.query_one("#remarks-table", DataTable).row_count == 0
        await pilot.press("?")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        assert "6=Remarks" in app.screen.query_one("#help-panel", Static).content


async def test_tui_completed_lessons_detail_sort_filter_and_navigation(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Static, TabbedContent

    from tests.test_completed_lessons import LESSON
    from tests.test_sync import STUDENT_A, STUDENT_B
    from vulcan_notify.tui import DetailScreen, MainScreen, VulcanTuiApp

    monkeypatch.setattr(cli.settings, "db_path", tmp_path / "completed-tui.db")
    app = VulcanTuiApp()
    await app.db.connect()
    try:
        for student in (STUDENT_A, STUDENT_B):
            await app.db.upsert_student(student)
            await app.db.upsert_completed_lesson(student.key, LESSON)
        await app.db.mark_missing_completed_lessons(
            STUDENT_B.key, set(), "2026-10-01", "2026-10-04"
        )
        await app.db.commit()
    finally:
        await app.db.close()
    async with app.run_test(size=(150, 35)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("7")
        await app.workers.wait_for_complete()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, MainScreen)
        assert screen.query_one(TabbedContent).active == "tab-completed_lessons"
        table = screen.query_one("#completed_lessons-table", DataTable)
        assert table.row_count == 1
        assert [str(cell) for cell in table.get_row_at(0)] == [
            "2026-10-02",
            "Jan",
            "1",
            "Math",
            "Example Teacher",
            "Fractions & numbers",
        ]
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        assert app.screen._body == "Fractions & numbers"
        fields = dict(app.screen._fields)
        assert fields["Thematic block"] == "Numbers"
        assert "Example collection" in fields["Collections"]
        assert "Resource" in fields["Resources"]
        assert fields["Vulcan"].endswith("/realizacjaZajec")
        await pilot.press("escape", "o", "O")
        assert screen._sort_state["completed_lessons"] == (1, True)
        await pilot.press("s")  # Anna's soft-deleted entry is hidden.
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert table.row_count == 0
        await pilot.press("1", "s", "7")  # Filter changed elsewhere applies on return.
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert table.row_count == 1
        assert screen._data["completed_lessons"][0]["student_name"] == "Jan"
        await pilot.press("?")
        await pilot.pause()
        assert "7=Completed lessons" in app.screen.query_one("#help-panel", Static).content
