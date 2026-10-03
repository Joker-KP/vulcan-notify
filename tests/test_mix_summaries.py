"""Mixed summaries: independent AI profiles, conditional sections and SMTP retries."""

import asyncio
import json
import smtplib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from vulcan_notify import __main__ as cli
from vulcan_notify import email
from vulcan_notify.config import Settings
from vulcan_notify.db import Database
from vulcan_notify.models import CompletedLesson, Message, Student


@pytest.fixture
def mix_config(monkeypatch, tmp_path):
    config = Settings(
        _env_file=None,
        email_enabled=True,
        smtp_host="smtp.example.org",
        email_from="school@example.org",
        email_to=["parent@example.org"],
        llm_api_key="synthetic-key",
        llm_lessons_days=3,
        session_file=tmp_path / "session.json",
        # mix is explicitly requested, independent of automatic digest AI/context.
        email_ai_summary=False,
        llm_include_lessons=False,
        email_digest_groups={"grade": False},
    )
    monkeypatch.setattr(cli, "settings", config)
    monkeypatch.setattr(email, "settings", config)
    return config


async def seed_sources(db, *, messages=True, lessons=True):
    now = datetime.now(UTC).isoformat()
    if messages:
        await db.upsert_message(
            Message(
                1,
                "message-key",
                "Teacher",
                "Trip",
                now,
                "Mailbox",
                False,
                False,
                "Private message input",
            )
        )
    if lessons:
        await db.upsert_student(Student("profile-a", "Jan", "3A", "School", 1, "mailbox-a"))
        await db.upsert_completed_lesson(
            "profile-a", CompletedLesson(1, now, 1, "Math", "Teacher", "Private lesson input")
        )
    await db.commit()


@pytest.mark.parametrize(
    "messages,lessons,message_result,lesson_result,section_count",
    [
        (True, True, "Message AI\nSecond line", "Lesson AI", 2),
        (True, False, "Message AI", None, 1),
        (False, True, None, "Lesson AI", 1),
        (True, True, None, "Lesson AI", 1),
        (True, True, "Message AI", " \n ", 1),
        (True, True, None, None, 0),
        (False, False, None, None, 0),
    ],
)
async def test_mix_uses_independent_profiles_and_only_nonempty_results(
    db, mix_config, monkeypatch, messages, lessons, message_result, lesson_result, section_count
):
    await seed_sources(db, messages=messages, lessons=lessons)
    message_query = AsyncMock(wraps=db.get_recent_messages)
    monkeypatch.setattr(db, "get_recent_messages", message_query)

    async def ai(text, config, *, profile):
        assert config is mix_config
        if profile == "messages":
            assert "Private message input" in text and "Private lesson input" not in text
            return message_result
        assert profile == "lessons"
        assert "Private lesson input" in text and "Private message input" not in text
        assert "ostatnie 14 dni" in text
        return lesson_result

    generate = AsyncMock(side_effect=ai)
    monkeypatch.setattr(cli, "summarize", generate)
    send = MagicMock()
    monkeypatch.setattr(email, "_send_email", send)
    if section_count:
        await cli._summarize_mix(db, 14)
        send.assert_called_once()
        row = send.call_args.args[0]
        html = row["html_body"]
        assert html.count("<section ") == section_count
        assert '<body style="margin:0;padding:24px 12px;background:#f1f5f9' in html
        assert (">Wiadomości</h2>" in html) == bool(message_result)
        has_lessons = bool(lesson_result and lesson_result.strip())
        assert (">Przeprowadzone zajęcia</h2>" in html) == has_lessons
        if section_count == 2:
            assert html.index(">Wiadomości</h2>") < html.index(">Przeprowadzone zajęcia</h2>")
        assert row["subject"] == "[eduVulcan] Podsumowanie (ostatnie 14 dni)"
        assert row["recipient"] == "parent@example.org"
        for body in (row["body"], html):
            assert "Ostatnie 14 dni" in body
            assert "Private message input" not in body and "Private lesson input" not in body
    else:
        with pytest.raises(SystemExit) as error:
            await cli._summarize_mix(db, 14)
        assert error.value.code == 1
        send.assert_not_called()
    assert generate.await_count == int(messages) + int(lessons)
    message_query.assert_awaited_once_with(days=14)
    assert await db.list_email_outbox() == []


async def test_mix_timeout_still_sends_other_section(db, mix_config, monkeypatch):
    await seed_sources(db)
    mix_config.email_ai_timeout_seconds = 0.01

    async def ai(text, config, *, profile):
        if profile == "messages":
            await asyncio.sleep(1)
        return "Lesson AI"

    monkeypatch.setattr(cli, "summarize", ai)
    send = MagicMock()
    monkeypatch.setattr(email, "_send_email", send)
    await cli._summarize_mix(db, 7)
    html = send.call_args.args[0]["html_body"]
    assert ">Wiadomości</h2>" not in html
    assert ">Przeprowadzone zajęcia</h2>" in html


async def test_mix_retry_survives_restart_without_repeating_ai(db, mix_config, monkeypatch):
    await seed_sources(db)
    mix_config.session_file.write_text(
        json.dumps({"base_url": "https://uczen.eduvulcan.pl/example", "cookies": []}),
        encoding="utf-8",
    )
    generate = AsyncMock(
        side_effect=["Message AI <script>alert(1)</script> &\nNext line", "Lesson AI"]
    )
    monkeypatch.setattr(cli, "summarize", generate)
    send = MagicMock(side_effect=smtplib.SMTPException("synthetic failure"))
    monkeypatch.setattr(email, "_send_email", send)
    with pytest.raises(SystemExit):
        await cli._summarize_mix(db, 7)
    saved = (await db.list_email_outbox())[0]
    assert "<script>" not in saved["html_body"]
    assert "&lt;script&gt;" in saved["html_body"]
    assert "&amp;" in saved["html_body"]
    assert "<br>\nNext line" in saved["html_body"]
    assert "Message AI <script>alert(1)</script> &\nNext line" in saved["body"]
    assert "https://wiadomosci.eduvulcan.pl/example/App/odebrane" in saved["body"]
    assert generate.await_count == 2
    await db.close()
    mix_config.session_file.unlink()

    restarted = Database(db._db_path)
    await restarted.connect()
    try:
        send.side_effect = None
        assert await email.drain_email_outbox(restarted) == (1, 0)
        retried = send.call_args.args[0]
        for key in ("body", "html_body", "subject", "message_id"):
            assert retried[key] == saved[key]
        assert await restarted.list_email_outbox() == []
        assert generate.await_count == 2
    finally:
        await restarted.close()


@pytest.mark.parametrize("day_args,expected_days", [([], 7), (["--days", "14"], 14)])
def test_main_routes_mix_with_shared_days(mix_config, monkeypatch, day_args, expected_days):
    database = AsyncMock()
    monkeypatch.setattr(cli, "Database", lambda path: database)
    mixed = AsyncMock()
    monkeypatch.setattr(cli, "_summarize_mix", mixed)
    monkeypatch.setattr(cli.sys, "argv", ["vulcan-notify", "summarize", "--type", "mix", *day_args])
    cli.main()
    mixed.assert_awaited_once_with(database, expected_days)
    database.close.assert_awaited_once()


@pytest.mark.parametrize("invalid", ["email_disabled", "missing_key", "zero_days", "negative_days"])
async def test_mix_validates_before_ai_or_database(mix_config, monkeypatch, invalid):
    days = 7
    if invalid == "email_disabled":
        mix_config.email_enabled = False
    elif invalid == "missing_key":
        mix_config.llm_api_key = None
    else:
        days = 0 if invalid == "zero_days" else -1
    database = MagicMock()
    monkeypatch.setattr(cli, "Database", database)
    generate = AsyncMock()
    monkeypatch.setattr(cli, "summarize", generate)
    with pytest.raises(SystemExit):
        await cli.cmd_summarize("mix", days=days)
    database.assert_not_called()
    generate.assert_not_awaited()


def test_help_lists_mix_requirements(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "argv", ["vulcan-notify", "summarize", "--help"])
    cli.main()
    output = capsys.readouterr().out
    assert "sync|messages|lessons|mix" in output
    assert "EMAIL_ENABLED=true" in output


async def test_standalone_and_mix_share_messages_input(db, mix_config, monkeypatch):
    await seed_sources(db, lessons=False)
    generate = AsyncMock(return_value="Message AI")
    monkeypatch.setattr(cli, "summarize", generate)
    monkeypatch.setattr(email, "_send_email", MagicMock())
    await cli._summarize_messages(db, 7)
    standalone = generate.call_args
    await cli._summarize_mix(db, 7)
    assert generate.call_args == standalone


@pytest.mark.parametrize("messages,lessons", [(None, None), (" \n", " ")])
async def test_queue_omits_empty_results(db, mix_config, messages, lessons):
    await email.queue_mix_summary(db, messages, lessons, 7)
    assert await db.list_email_outbox() == []


async def test_markdown_sections_and_readable_plaintext(db, mix_config):
    await email.queue_mix_summary(
        db,
        "### Organizacja\n\n- **Zebranie** w piątek\n- *Wycieczka* jutro\n\n"
        "[Szczegóły](https://school.example.org/trip)",
        "## Matematyka\n\n1. Ułamki\n2. Geometria\n\n"
        "| Przedmiot | Temat |\n| --- | --- |\n| Polski | Czytanie |",
        7,
    )
    row = (await db.list_email_outbox())[0]
    html, plain = row["html_body"], row["body"]
    assert row["subject"] == "[eduVulcan] Podsumowanie tygodnia"
    assert "Ostatnie 7 dni" in html and "Ostatnie 7 dni" in plain
    assert html.count("<h2 ") == 2
    assert "Organizacja</h3>" in html and "Matematyka</h3>" in html
    assert "<strong>Zebranie</strong>" in html and "<em>Wycieczka</em>" in html
    assert "<ul " in html and "<ol " in html and "<table " in html
    assert 'href="https://school.example.org/trip"' in html
    assert "border-collapse:collapse" in html
    assert "###" not in plain and "**Zebranie**" not in plain
    assert "- Zebranie w piątek" in plain and "1. Ułamki" in plain
    assert "Szczegóły (https://school.example.org/trip)" in plain
    assert "Polski\t Czytanie" in plain


async def test_markdown_cannot_activate_html_links_or_remote_images(db, mix_config):
    await email.queue_mix_summary(
        db,
        '<script>alert(1)</script>\n\n<img src="https://bad.example.org/raw" onerror="alert(1)">'
        "\n\n[Click](javascript:alert(1))\n\n![Remote](https://bad.example.org/image)"
        "\n\n[Safe](https://school.example.org)",
        None,
        7,
    )
    html = (await db.list_email_outbox())[0]["html_body"]
    assert "<script>" not in html and "<img " not in html
    assert 'href="javascript:' not in html
    assert 'href="https://school.example.org"' in html


async def test_section_footers_use_saved_tenant_and_active_profile_keys(db, mix_config):
    mix_config.session_file.write_text(
        json.dumps({"base_url": "https://uczen.eduvulcan.pl/example", "cookies": []}),
        encoding="utf-8",
    )
    for key, class_name in (("key/a+b=", "3A"), ("key-b", "3B"), ("retired", "2A")):
        await db.upsert_student(Student(key, "Jan", class_name, "School", 1, "mailbox"))
    await db.deactivate_students_except({"key/a+b=", "key-b"})
    await email.queue_mix_summary(db, "Message AI", "Lesson AI", 7)
    row = (await db.list_email_outbox())[0]
    messages_section, lessons_section = row["html_body"].split("</section>")[:2]
    inbox = "https://wiadomosci.eduvulcan.pl/example/App/odebrane"
    assert inbox in messages_section and inbox not in lessons_section
    for key in ("key%2Fa%2Bb%3D", "key-b"):
        url = f"https://uczen.eduvulcan.pl/example/App/{key}/realizacjaZajec"
        assert url in lessons_section and url not in messages_section
        assert url in row["body"]
    assert lessons_section.count(">Otwórz przeprowadzone zajęcia</a>") == 2
    assert (
        "Jan" not in lessons_section and "3A" not in lessons_section and "3B" not in lessons_section
    )
    for section in (messages_section, lessons_section):
        assert 'style="color:#1d4ed8"' in section
        assert "display:inline-block" not in section and "background:#2563eb" not in section
    assert "retired" not in row["html_body"] and "2A" not in row["html_body"]
    assert inbox in row["body"]


@pytest.mark.parametrize(
    "session",
    [
        None,
        "{invalid",
        '{"base_url": "https://uczen.eduvulcan.pl/example"}',
        '{"base_url": "https://uczen.eduvulcan.pl/example?token=synthetic-secret", "cookies": []}',
    ],
)
async def test_missing_or_invalid_session_keeps_summary_without_inventing_links(
    db, mix_config, session
):
    if session is not None:
        mix_config.session_file.write_text(session, encoding="utf-8")
    await email.queue_mix_summary(db, "Message AI", "Lesson AI", 7)
    row = (await db.list_email_outbox())[0]
    assert "Message AI" in row["body"] and "Lesson AI" in row["body"]
    assert "Otwórz" not in row["html_body"]
    assert "synthetic-secret" not in row["html_body"]
