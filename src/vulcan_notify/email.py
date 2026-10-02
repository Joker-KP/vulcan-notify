"""SMTP change digests with a persistent, per-recipient retry outbox."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import smtplib
import ssl
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import TYPE_CHECKING

from vulcan_notify.config import parse_email_sender, settings
from vulcan_notify.summarizer import summarize
from vulcan_notify.text import strip_html

if TYPE_CHECKING:
    from vulcan_notify.config import Settings
    from vulcan_notify.db import Database
    from vulcan_notify.sync import FullSyncResult

log = logging.getLogger(__name__)


def format_summary(result: FullSyncResult, config: Settings) -> tuple[str, int]:
    """Describe only this run's events, respecting student/account baselines."""
    lines = ["eduVULCAN — detected changes", ""]
    count = 0
    for student_result in result.student_results:
        if student_result.is_first_sync or not student_result.all_changes:
            continue
        student = student_result.student
        lines.append(f"{student.name} ({student.class_name}, {student.school}):")
        for change in student_result.all_changes:
            count += 1
            lines.append(f"- [{change.item_type}/{change.change_type}] {strip_html(change.title)}")
            if change.body:
                lines.append(f"  {strip_html(change.body)}")
            date = getattr(change.raw, "date", None)
            if date:
                lines.append(f"  Date: {date}")
        lines.append("")

    if not result.is_first_message_sync:
        for message in result.new_messages:
            count += 1
            lines.extend(
                [
                    f"New message: {strip_html(message.subject)}",
                    f"From: {strip_html(message.sender)}",
                    f"Date: {message.date}",
                    f"Mailbox: {message.mailbox}",
                ]
            )
            if message.has_attachments:
                lines.append("Attachments: yes (view in eduVULCAN)")
            if config.email_include_message_bodies and message.content:
                lines.append(strip_html(message.content))
            lines.append("")

    if result.has_failures:
        lines.append(
            "Some sections failed; this summary covers successfully detected changes only."
        )
    return "\n".join(lines).strip(), count


async def queue_summary(result: FullSyncResult, db: Database) -> None:
    """Persist the plain digest before attempting optional AI or SMTP."""
    if not settings.email_enabled:
        return
    body, count = format_summary(result, settings)
    if not count:
        return
    subject = f"{settings.email_subject_prefix}: {count} change(s)"
    date_header = format_datetime(datetime.now(UTC))
    new_keys: list[str] = []
    for recipient in dict.fromkeys(settings.email_to):
        key = hashlib.sha256(f"{result.notification_id}\0{recipient}".encode()).hexdigest()
        inserted = await db.enqueue_email(
            key,
            settings.email_from,
            recipient,
            subject,
            body,
            make_msgid(domain="vulcan-notify.local"),
            date_header,
        )
        if inserted:
            new_keys.append(key)
    await db.commit()

    # Retry uses the stored body. If preparation is interrupted, the queued plain
    # version survives; an AI failure never prevents delivery of the facts.
    if new_keys and settings.email_ai_summary and settings.llm_api_key:
        try:
            replacement = await asyncio.wait_for(
                summarize(body, settings),
                timeout=settings.email_ai_timeout_seconds,
            )
        except Exception as exc:
            log.warning("Email AI summary failed (%s); using plain summary", type(exc).__name__)
            replacement = None
        if replacement and replacement.strip():
            for key in new_keys:
                await db.update_email_body(key, replacement.strip())
            await db.commit()


def _send_email(row: dict[str, str], config: Settings) -> None:
    """Blocking SMTP operation; called in a worker thread, never logs content."""
    sender = parse_email_sender(row["sender"])
    message = EmailMessage()
    message["From"] = sender
    message["To"] = row["recipient"]
    message["Subject"] = row["subject"]
    message["Message-ID"] = row["message_id"]
    message["Date"] = row["date_header"]
    message.set_content(row["body"])

    context = ssl.create_default_context()
    connection: smtplib.SMTP
    if config.smtp_security == "ssl":
        connection = smtplib.SMTP_SSL(
            config.smtp_host,
            config.smtp_port,
            timeout=config.smtp_timeout_seconds,
            context=context,
        )
    else:
        connection = smtplib.SMTP(
            config.smtp_host,
            config.smtp_port,
            timeout=config.smtp_timeout_seconds,
        )
    try:
        connection.ehlo()
        if config.smtp_security == "starttls":
            connection.starttls(context=context)
            connection.ehlo()
        if config.smtp_username and config.smtp_password:
            connection.login(config.smtp_username, config.smtp_password.get_secret_value())
        refused = connection.send_message(
            message,
            from_addr=sender.addr_spec,
            to_addrs=[row["recipient"]],
        )
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    finally:
        # QUIT failure after DATA acceptance must not turn a success into a retry.
        try:
            connection.quit()
        except Exception:
            with contextlib.suppress(Exception):
                connection.close()


async def drain_email_outbox(db: Database) -> tuple[int, int]:
    """Retry queued digests, returning (accepted by SMTP, still pending)."""
    if not settings.email_enabled:
        return 0, 0
    delivered = 0
    rows = await db.list_email_outbox()
    for row in rows:
        try:
            await asyncio.to_thread(_send_email, row, settings)
        except Exception as exc:
            error_type = type(exc).__name__
            # SMTP errors may embed recipients, credentials or message text.
            log.warning("SMTP delivery failed (%s); digest remains queued", error_type)
            await db.mark_email_failure(row["delivery_key"], error_type)
        else:
            await db.mark_email_sent(row["delivery_key"])
            delivered += 1
        # Commit each recipient's outcome before attempting the next one.
        await db.commit()
    pending = len(rows) - delivered
    if rows:
        log.info("Email digests: SMTP accepted=%d, pending=%d", delivered, pending)
    return delivered, pending


async def publish_email(result: FullSyncResult, db: Database) -> None:
    """Optional output; failures must not stop other notification channels."""
    if not settings.email_enabled:
        return
    try:
        await queue_summary(result, db)
        await drain_email_outbox(db)
    except Exception as exc:
        log.warning("Email output failed (%s)", type(exc).__name__)
