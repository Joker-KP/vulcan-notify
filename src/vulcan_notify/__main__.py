"""Entry point for vulcan-notify service."""

import asyncio
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from vulcan_notify.auth import (
    InvalidSessionError,
    SessionValidationError,
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
from vulcan_notify.email import (
    WEEKLY_MIX_STATE,
    AuthFailureReason,
    clear_auth_failure,
    drain_email_outbox,
    publish_auth_failure,
    publish_email,
    queue_mix_summary,
)
from vulcan_notify.mqtt import drain_outbox, publish_changes
from vulcan_notify.summarizer import format_changes_for_llm, lessons_context, summarize
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
    try:
        await login_and_save_session(settings.session_file)
    except Exception as exc:
        logger.error("Interactive authentication failed (%s)", type(exc).__name__)
        await _email_auth_status("interactive_failed")
        sys.exit(1)
    await _email_auth_status()


async def _email_auth_status(reason: AuthFailureReason | None = None) -> None:
    """Authentication can fail before the sync database has been opened."""
    if not settings.email_enabled:
        return
    db = Database(settings.db_path)
    try:
        try:
            await db.connect()
            if reason is None:
                await clear_auth_failure(db)
            else:
                await publish_auth_failure(db, reason)
        finally:
            await db.close()
    except Exception as exc:
        logger.warning("Email authentication status failed (%s)", type(exc).__name__)


async def cmd_test() -> None:
    """Test if saved session is still valid."""
    try:
        session = load_session(settings.session_file)
    except (FileNotFoundError, InvalidSessionError):
        print("No usable session. Run 'vulcan-notify auth' to authenticate.")
        sys.exit(1)
    try:
        valid = await test_session(session)
    except SessionValidationError:
        print("Session validation unavailable. Saved session preserved; try again later.")
        sys.exit(1)
    if not valid:
        print("Session is not usable. Run 'vulcan-notify auth' to restore access.")
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
        await _email_auth_status("recovery_failed")
        sys.exit(1)


async def _ensure_session() -> dict[str, Any]:
    """Load session, auto-reauth if expired and credentials are available."""
    try:
        session = load_session(settings.session_file)
    except FileNotFoundError:
        session = None
    except (InvalidSessionError, OSError) as exc:
        logger.warning(
            "Saved session is unavailable (%s); authentication required", type(exc).__name__
        )
        session = None

    if session:
        try:
            if await test_session(session):
                return session
        except SessionValidationError:
            print("Session validation unavailable. Saved session preserved; try again later.")
            sys.exit(1)

    # Session missing or expired - try auto-login
    creds = _get_credentials()
    if creds:
        logger.info("Saved session requires recovery; restoring eduVULCAN access...")
        return await _recover_session(creds[0], creds[1])

    if session is None:
        print("No usable session. Run 'vulcan-notify auth' to authenticate.")
    else:
        print("Session is not usable. Run 'vulcan-notify auth' to restore access.")
    print(
        "Tip: set VULCAN_LOGIN/VULCAN_PASSWORD in .env, "
        "or store in macOS Keychain (service: vulcan-notify)."
    )
    await _email_auth_status("credentials_missing")
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
    started_at = datetime.now(UTC)
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
        await clear_auth_failure(db)

        if not result.student_results:
            print("No students found.")
            sys.exit(1)

        _print_result(result)
        await publish_email(result, db)
        await _sync_calendar(db)
        await publish_changes(result, db)
        if result.has_failures:
            sys.exit(1)
        await _weekly_mix_summary(db, started_at)

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
            logger.info("Student access requires recovery during sync; restoring session...")
            await client.close()
            session = await _recover_session(creds[0], creds[1])
            client = VulcanClient(session)
            try:
                result = await sync_all(client, db)
            except SessionExpiredError as retry_exc:
                if isinstance(retry_exc, SyncSessionExpiredError):
                    _print_result(retry_exc.partial_result)
                    await publish_email(retry_exc.partial_result, db)
                    await _sync_calendar(db)
                    await publish_changes(retry_exc.partial_result, db)
                print("Session still unusable after recovery. Run 'vulcan-notify auth'.")
                await publish_auth_failure(db, "session_expired")
                sys.exit(1)
            await clear_auth_failure(db)
            if not result.student_results:
                print("No students found after session recovery.")
                sys.exit(1)
            _print_result(result)
            await publish_email(result, db)
            await _sync_calendar(db)
            await publish_changes(result, db)
            if result.has_failures:
                sys.exit(1)
            await _weekly_mix_summary(db, started_at)
        else:
            print("Session expired. Run 'vulcan-notify auth' to re-authenticate.")
            print(
                "Tip: set VULCAN_LOGIN/VULCAN_PASSWORD in .env, "
                "or store in macOS Keychain (service: vulcan-notify)."
            )
            await publish_auth_failure(db, "credentials_missing")
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


async def cmd_summarize(summary_type: str = "sync", days: int | None = None) -> None:
    """Summarize stored data using AI."""
    if days is None:
        days = settings.llm_lessons_days if summary_type == "lessons" else 7
    if days < 1:
        print("--days must be a positive integer.")
        sys.exit(1)
    if not settings.llm_api_key:
        print("LLM_API_KEY not set. Configure it in .env to use AI summaries.")
        sys.exit(1)
    if summary_type == "mix" and not settings.email_enabled:
        print("EMAIL_ENABLED must be true to send a mixed summary email.")
        sys.exit(1)

    db = Database(settings.db_path)
    await db.connect()

    try:
        if summary_type == "messages":
            await _summarize_messages(db, days)
        elif summary_type == "lessons":
            await _summarize_lessons(db, days)
        elif summary_type == "mix":
            await _summarize_mix(db, days)
        else:
            await _summarize_changes(db, days)
    finally:
        await db.close()


async def _summarize_changes(db: Database, days: int) -> None:
    """Summarize recent sync changes from the database."""
    changes = await db.get_recent_changes(days=days)
    text = format_changes_for_llm(changes)
    if settings.llm_include_lessons:
        context = await lessons_context(db, settings)
        if context:
            text = f"{text}\n\n{context}".strip()
    if not text:
        print(f"No changes in the last {days} day(s). Try a larger range with --days.")
        sys.exit(1)

    summary = await summarize(text, settings, profile="default")
    if summary:
        print(f"{BOLD}Sync Summary (last {days} day(s)){RESET}")
        print(summary)
    else:
        print("Failed to generate summary.")
        sys.exit(1)


async def _summarize_lessons(db: Database, days: int) -> None:
    """Summarize stored lesson topics without contacting eduVULCAN."""
    text = await lessons_context(db, settings, days=days)
    if not text:
        print(f"No completed lesson topics in the last {days} day(s).")
        sys.exit(1)
    summary = await summarize(text, settings, profile="lessons")
    if summary:
        print(f"{BOLD}Completed Lessons Summary (last {days} day(s)){RESET}")
        print(summary)
    else:
        print("Failed to generate summary.")
        sys.exit(1)


async def _messages_context(db: Database, days: int) -> tuple[str, int]:
    """Share the messages AI input between standalone and mixed summaries."""
    messages = await db.get_recent_messages(days=days)
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

    return "\n".join(lines), len(messages)


async def _summarize_messages(db: Database, days: int) -> None:
    """Summarize recent messages from the database."""
    text, count = await _messages_context(db, days)
    if not text:
        print(f"No messages in the last {days} days. Try a larger range with --days.")
        sys.exit(1)
    summary = await summarize(text, settings, profile="messages")
    if summary:
        print(f"{BOLD}Messages Summary (last {days} days, {count} messages){RESET}")
        print(summary)
    else:
        print("Failed to generate summary.")
        sys.exit(1)


async def _weekly_mix_summary(db: Database, started_at: datetime) -> None:
    """Run Friday's summary once, keeping optional output failures out of sync status."""
    if not (settings.weekly_summary_enabled and settings.email_enabled and settings.llm_api_key):
        return
    local_start = started_at.astimezone(settings.timezone)
    if local_start.weekday() != 4 or local_start.hour < 15:
        return
    period = local_start.date().isoformat()
    try:
        if await db.get_state(WEEKLY_MIX_STATE) == period:
            return
        logger.info("Running weekly mixed summary (%s)", period)
        await _summarize_mix(db, 7, weekly_period=period)
    except SystemExit:
        # CLI mix exits nonzero for AI failure or pending SMTP. Queued summaries
        # already have a marker, so normal outbox retries never repeat their AI.
        logger.warning("Weekly mixed summary incomplete; subsequent syncs will retry")
    except Exception as exc:
        await db.db.rollback()
        logger.warning(
            "Weekly mixed summary failed (%s); subsequent syncs will retry", type(exc).__name__
        )


async def _summarize_mix(db: Database, days: int, *, weekly_period: str | None = None) -> None:
    """Email independent messages/lessons AI results in at most two sections."""
    messages, _ = await _messages_context(db, days)
    lessons = await lessons_context(db, settings, days=days)
    if weekly_period and not messages and not lessons:
        await db.set_state(WEEKLY_MIX_STATE, weekly_period)
        await db.commit()
        logger.info("No source data for weekly mixed summary (%s)", weekly_period)
        return
    summaries: dict[str, str | None] = {}
    for profile, text in (("messages", messages), ("lessons", lessons)):
        summary = None
        if text:
            try:
                summary = await asyncio.wait_for(
                    summarize(text, settings, profile=profile),
                    timeout=settings.email_ai_timeout_seconds,
                )
            except TimeoutError:
                logger.warning("Mixed summary AI timed out (%s)", profile)
            if not summary or not summary.strip():
                print(f"Failed to generate {profile} summary; omitting its email section.")
        summaries[profile] = summary.strip() if summary else None
    if not any(summaries.values()):
        print(f"No mixed summary to email for the last {days} day(s).")
        sys.exit(1)
    await queue_mix_summary(
        db, summaries["messages"], summaries["lessons"], days, weekly_period=weekly_period
    )
    delivered, pending = await drain_email_outbox(db)
    print(f"Summary email: SMTP accepted={delivered}, pending={pending}.")
    if pending:
        print("Queued emails can be retried with vulcan-notify email-retry.")
        sys.exit(1)


def _print_summary_help() -> None:
    print("Usage: vulcan-notify summarize [--type sync|messages|lessons|mix] [--days N]")
    print("    --type sync|messages|lessons|mix  (default: sync; mix sends an email)")
    print("    --days N  (default: 7; lessons uses LLM_LESSONS_DAYS)")
    print("    mix requires EMAIL_ENABLED=true and configured SMTP settings")


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
            days = None
            args = sys.argv[2:]
            if "--help" in args or "-h" in args:
                _print_summary_help()
                return
            for i, arg in enumerate(args):
                if arg == "--type" and i + 1 < len(args):
                    summary_type = args[i + 1]
                elif arg == "--days" and i + 1 < len(args):
                    try:
                        days = int(args[i + 1])
                    except ValueError:
                        print("--days must be a positive integer.")
                        sys.exit(1)
            if summary_type not in ("sync", "messages", "lessons", "mix"):
                print("Invalid --type. Use 'sync', 'messages', 'lessons' or 'mix'.")
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
            print("  summarize - AI summary of changes, messages or lessons; mix emails both")
            _print_summary_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
