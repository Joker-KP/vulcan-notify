"""Entry point for vulcan-notify service."""

import asyncio
import logging
import sys
from typing import Any

from vulcan_notify.auth import (
    InvalidSessionError,
    auto_login,
    get_keychain_credentials,
    load_session,
    login_and_save_session,
    test_session,
)
from vulcan_notify.calendar import sync_to_calendar
from vulcan_notify.client import SessionExpiredError, VulcanClient
from vulcan_notify.config import settings
from vulcan_notify.db import Database
from vulcan_notify.display import BOLD, RESET, format_compact_sync, format_full_sync
from vulcan_notify.email import drain_email_outbox, publish_email
from vulcan_notify.mqtt import drain_outbox, publish_changes
from vulcan_notify.summarizer import format_changes_for_llm, summarize
from vulcan_notify.sync import FullSyncResult, SyncSessionExpiredError, sync_all

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper()),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def cmd_auth() -> None:
    """Interactive auth flow - browser login and save session cookies."""
    await login_and_save_session(settings.session_file)


async def cmd_test() -> None:
    """Test if saved session is still valid."""
    try:
        session = load_session(settings.session_file)
    except (FileNotFoundError, InvalidSessionError):
        print("No usable session. Run 'vulcan-notify auth' to authenticate.")
        sys.exit(1)
    valid = await test_session(session)
    if not valid:
        print("Session expired. Run 'vulcan-notify auth' to re-authenticate.")
        sys.exit(1)
    print("Session is valid.")


def _get_credentials() -> tuple[str, str] | None:
    """Resolve credentials from .env or macOS Keychain."""
    if settings.vulcan_login and settings.vulcan_password:
        return (settings.vulcan_login, settings.vulcan_password)
    return get_keychain_credentials()


async def _recover_session(login: str, password: str) -> dict[str, Any]:
    """Attempt automatic recovery, leaving interactive login to an explicit command."""
    try:
        return await auto_login(settings.session_file, login, password)
    except Exception as exc:
        logger.error(
            "Automatic authentication failed (%s); manual login required", type(exc).__name__
        )
        print("Run 'vulcan-notify auth' for interactive recovery.")
        print("Docker: stop vulcan-sync, then docker compose --profile auth up vulcan-auth.")
        sys.exit(1)


async def _ensure_session() -> dict[str, Any]:
    """Load session, auto-reauth if expired and credentials are available."""
    try:
        session = load_session(settings.session_file)
    except FileNotFoundError:
        session = None
    except InvalidSessionError:
        logger.warning("Saved session is invalid; authentication required")
        session = None

    if session and await test_session(session):
        return session

    # Session missing or expired - try auto-login
    creds = _get_credentials()
    if creds:
        logger.info("Session expired, auto-logging in...")
        return await _recover_session(creds[0], creds[1])

    if session is None:
        print("No usable session. Run 'vulcan-notify auth' to authenticate.")
    else:
        print("Session expired. Run 'vulcan-notify auth' to re-authenticate.")
    print(
        "Tip: set VULCAN_LOGIN/VULCAN_PASSWORD in .env, "
        "or store in macOS Keychain (service: vulcan-notify)."
    )
    sys.exit(1)


def _print_calendar_result(cal_result: object) -> None:
    """Print calendar sync summary if anything happened."""
    from vulcan_notify.calendar import CalendarSyncResult

    if not isinstance(cal_result, CalendarSyncResult):
        return
    parts = []
    if cal_result.created:
        parts.append(f"{cal_result.created} created")
    if cal_result.updated:
        parts.append(f"{cal_result.updated} updated")
    if cal_result.deleted:
        parts.append(f"{cal_result.deleted} deleted")
    if cal_result.errors:
        parts.append(f"{cal_result.errors} errors")
    if parts:
        print(f"\nCalendar: {', '.join(parts)}")
    if cal_result.skipped_students:
        for name in cal_result.skipped_students:
            print(f"  Warning: no calendar mapping for {name}")


async def _sync_calendar(db: Database) -> None:
    """Push exams/homework to macOS Calendar if configured."""
    if not settings.calendar_map:
        return
    cal_result = await sync_to_calendar(db)
    _print_calendar_result(cal_result)


async def cmd_heartbeat() -> None:
    """Publish the retained MQTT heartbeat without syncing.

    Called by sync-loop.sh once per poll interval while it is parked in quiet hours.
    The HA sensor on `school/status` carries `expire_after: 2400`, so five silent
    hours dropped the entity to `unavailable` and wiped its attributes -- the
    dashboard tile then had no timestamp at all and read "never synced" rather than
    "5h ago". Ticking here keeps the entity alive and honest: still connected, no new
    data expected yet.
    """
    db = Database(settings.db_path)
    await db.connect()
    try:
        ok, pending = await drain_outbox(db)
        print(f"heartbeat published (delivered={ok}, pending={pending})")
    finally:
        await db.close()


async def cmd_email_retry() -> None:
    """Retry email delivery without contacting eduVULCAN or regenerating summaries."""
    if not settings.email_enabled:
        print("Email is disabled. Set EMAIL_ENABLED=true and configure SMTP in .env.")
        sys.exit(1)
    db = Database(settings.db_path)
    await db.connect()
    try:
        delivered, pending = await drain_email_outbox(db)
        print(f"Email notifications: SMTP accepted={delivered}, pending={pending}")
        if pending:
            sys.exit(1)
    finally:
        await db.close()


async def cmd_sync() -> None:
    """Fetch latest data and show changes since last sync."""
    session = await _ensure_session()
    client = VulcanClient(session)
    db = Database(settings.db_path)
    await db.connect()

    def _print_result(result: FullSyncResult) -> None:
        if sys.stdout.isatty():
            print(format_full_sync(result, settings.message_sender_whitelist))
        else:
            print(format_compact_sync(result, settings.message_sender_whitelist))

    try:
        result = await sync_all(client, db)

        if not result.student_results:
            print("No students found.")
            sys.exit(1)

        _print_result(result)
        await publish_email(result, db)
        await _sync_calendar(db)
        await publish_changes(result, db)
        if result.has_failures:
            sys.exit(1)

    except SessionExpiredError as exc:
        # A retry compares against rows already committed by successful sections.
        # Deliver their changes now so they are not lost from the retried diff.
        if isinstance(exc, SyncSessionExpiredError):
            _print_result(exc.partial_result)
            await publish_email(exc.partial_result, db)
            await _sync_calendar(db)
            await publish_changes(exc.partial_result, db)
        # Try auto-reauth once if it fails mid-sync
        creds = _get_credentials()
        if creds:
            logger.info("Session expired mid-sync, re-authenticating...")
            await client.close()
            session = await _recover_session(creds[0], creds[1])
            client = VulcanClient(session)
            try:
                result = await sync_all(client, db)
            except SyncSessionExpiredError as retry_exc:
                _print_result(retry_exc.partial_result)
                await publish_email(retry_exc.partial_result, db)
                await _sync_calendar(db)
                await publish_changes(retry_exc.partial_result, db)
                print("Session still expired after recovery. Run 'vulcan-notify auth'.")
                sys.exit(1)
            if not result.student_results:
                print("No students found after session recovery.")
                sys.exit(1)
            _print_result(result)
            await publish_email(result, db)
            await _sync_calendar(db)
            await publish_changes(result, db)
            if result.has_failures:
                sys.exit(1)
        else:
            print("Session expired. Run 'vulcan-notify auth' to re-authenticate.")
            print(
                "Tip: set VULCAN_LOGIN/VULCAN_PASSWORD in .env, "
                "or store in macOS Keychain (service: vulcan-notify)."
            )
            sys.exit(1)
    finally:
        await client.close()
        await db.close()


async def cmd_calendar() -> None:
    """Force re-sync all active exams/homework to macOS Calendar."""
    if not settings.calendar_map:
        print("CALENDAR_MAP not configured. Set it in .env, e.g.:")
        print('  CALENDAR_MAP={"Alice Smith": "School Alice"}')
        sys.exit(1)

    db = Database(settings.db_path)
    await db.connect()

    try:
        # Clear all existing calendar events and UIDs
        print("Clearing existing calendar events...")
        # Delete events that have UIDs
        students_cursor = await db.db.execute("SELECT key, name FROM students")
        students = {row[0]: row[1] for row in await students_cursor.fetchall()}

        from vulcan_notify.calendar import _delete_event

        for student_key, student_name in students.items():
            calendar_name = settings.calendar_map.get(student_name)
            if not calendar_name:
                continue
            items = await db.get_items_for_calendar(student_key)
            for table in ("exams", "homework"):
                for item in items[table]:
                    if item["calendar_uid"]:
                        try:
                            await _delete_event(calendar_name, str(item["calendar_uid"]))
                        except Exception as exc:
                            logger.warning(
                                "Calendar deletion failed (%s); retaining UID for retry",
                                type(exc).__name__,
                            )
                        else:
                            await db.clear_calendar_uid(table, int(str(item["id"])))
        await db.commit()

        # Re-create all events
        print("Creating calendar events...")
        cal_result = await sync_to_calendar(db)
        _print_calendar_result(cal_result)
    finally:
        await db.close()


async def cmd_tui() -> None:
    """Launch interactive TUI for browsing synced data."""
    try:
        from vulcan_notify.tui import run_tui
    except ModuleNotFoundError as exc:
        if exc.name != "textual":
            raise
        print("Textual not installed. Run: uv sync --extra tui")
        sys.exit(1)
    await run_tui()


async def cmd_summarize(summary_type: str = "sync", days: int = 7) -> None:
    """Summarize stored data using AI."""
    if not settings.llm_api_key:
        print("LLM_API_KEY not set. Configure it in .env to use AI summaries.")
        sys.exit(1)

    db = Database(settings.db_path)
    await db.connect()

    try:
        if summary_type == "messages":
            await _summarize_messages(db, days)
        else:
            await _summarize_changes(db, days)
    finally:
        await db.close()


async def _summarize_changes(db: Database, days: int) -> None:
    """Summarize recent sync changes from the database."""
    changes = await db.get_recent_changes(days=days)
    if not changes:
        print(f"No changes in the last {days} day(s). Try a larger range with --days.")
        sys.exit(1)

    text = format_changes_for_llm(changes)
    summary = await summarize(text, settings, profile="default")
    if summary:
        print(f"{BOLD}Sync Summary (last {days} day(s)){RESET}")
        print(summary)
    else:
        print("Failed to generate summary.")
        sys.exit(1)


async def _summarize_messages(db: Database, days: int) -> None:
    """Summarize recent messages from the database."""
    messages = await db.get_recent_messages(days=days)
    if not messages:
        print(f"No messages in the last {days} days. Try a larger range with --days.")
        sys.exit(1)

    lines: list[str] = []
    for msg in messages:
        lines.append(f"From: {msg['sender']}")
        lines.append(f"Subject: {msg['subject']}")
        lines.append(f"Date: {msg['date']}")
        if msg["mailbox"]:
            lines.append(f"Mailbox: {msg['mailbox']}")
        if msg["content"]:
            lines.append(f"Content: {msg['content']}")
        lines.append("")

    text = "\n".join(lines)
    summary = await summarize(text, settings, profile="messages")
    if summary:
        print(f"{BOLD}Messages Summary (last {days} days, {len(messages)} messages){RESET}")
        print(summary)
    else:
        print("Failed to generate summary.")
        sys.exit(1)


def main() -> None:
    setup_logging()

    command = sys.argv[1] if len(sys.argv) > 1 else "sync"

    match command:
        case "auth":
            asyncio.run(cmd_auth())
        case "test":
            asyncio.run(cmd_test())
        case "api-gather" | "api-check":
            from vulcan_notify.eduvulcan_contract import main as contract_main

            contract_main(sys.argv[1:])
        case "sync":
            asyncio.run(cmd_sync())
        case "heartbeat":
            asyncio.run(cmd_heartbeat())
        case "email-retry":
            asyncio.run(cmd_email_retry())
        case "calendar":
            asyncio.run(cmd_calendar())
        case "tui":
            asyncio.run(cmd_tui())
        case "summarize":
            summary_type = "sync"
            days = 7
            args = sys.argv[2:]
            for i, arg in enumerate(args):
                if arg == "--type" and i + 1 < len(args):
                    summary_type = args[i + 1]
                elif arg == "--days" and i + 1 < len(args):
                    days = int(args[i + 1])
            if summary_type not in ("sync", "messages"):
                print("Invalid --type. Use 'sync' or 'messages'.")
                sys.exit(1)
            asyncio.run(cmd_summarize(summary_type=summary_type, days=days))
        case _:
            print(
                "Usage: vulcan-notify "
                "[auth|test|api-gather|api-check|sync|heartbeat|email-retry|calendar|tui|summarize]"
            )
            print("  auth      - Interactive login and save session")
            print("  test      - Test if saved session is valid")
            print("  api-gather - Gather the upstream OpenAPI baseline and sanitized fixtures")
            print("  api-check  - Check live upstream responses against the saved contract")
            print("  sync      - Fetch latest data and show changes (default)")
            print("  heartbeat - Publish the retained MQTT heartbeat only, no sync")
            print("  email-retry - Retry queued SMTP digests without an upstream sync")
            print("  calendar  - Force re-sync all events to macOS Calendar")
            print("  tui       - Interactive message browser")
            print("  summarize - AI summary of recent changes or messages")
            print("    --type sync|messages  (default: sync)")
            print("    --days N              (default: 7)")
            sys.exit(1)


if __name__ == "__main__":
    main()
