"""SMTP change digests and separate message notifications with persistent retries."""

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
from html import escape
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from vulcan_notify.config import parse_email_sender, settings
from vulcan_notify.models import Remark
from vulcan_notify.summarizer import summarize
from vulcan_notify.text import message_html, message_text, strip_html

if TYPE_CHECKING:
    from vulcan_notify.config import Settings
    from vulcan_notify.db import Database
    from vulcan_notify.models import Message
    from vulcan_notify.sync import FullSyncResult

log = logging.getLogger(__name__)
_WEEKDAYS_PL = ("poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela")


def format_summary(result: FullSyncResult) -> tuple[str, int]:
    """Describe only this run's student changes, respecting baselines."""
    lines = ["eduVULCAN — detected changes", ""]
    count = 0
    for student_result in result.student_results:
        changes = [change for change in student_result.all_changes if change.item_type != "remark"]
        if student_result.is_first_sync or not changes:
            continue
        student = student_result.student
        lines.append(f"{student.name} ({student.class_name}, {student.school}):")
        for change in changes:
            count += 1
            lines.append(f"- [{change.item_type}/{change.change_type}] {strip_html(change.title)}")
            if change.body:
                lines.append(f"  {strip_html(change.body)}")
            date = getattr(change.raw, "date", None)
            if date:
                lines.append(f"  Date: {date}")
        lines.append("")

    if result.has_failures:
        lines.append(
            "Some sections failed; this summary covers successfully detected changes only."
        )
    return "\n".join(lines).strip(), count


def _format_message_date(value: str, config: Settings) -> str:
    """Display ISO timestamps in the household zone; naive timestamps mean UTC."""
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return value
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    try:
        local = stamp.astimezone(ZoneInfo(config.quiet_hours_tz))
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Unknown message display timezone; using UTC")
        local = stamp.astimezone(UTC)
    return f"{local:%Y-%m-%d %H:%M} ({_WEEKDAYS_PL[local.weekday()]})"


def _message_metadata(message: Message, config: Settings) -> list[str]:
    lines = [
        f"Autor: {strip_html(message.sender)}",
        f"Data: {_format_message_date(message.date, config)}",
        f"Skrzynka: {message.mailbox}",
    ]
    if message.has_attachments:
        lines.append("Załączniki: tak (zobacz w eduVULCAN)")
    return lines


def format_message(message: Message, config: Settings) -> str:
    """One upstream message, retaining its sender, date and mailbox identity."""
    lines = _message_metadata(message, config)
    if config.email_include_message_bodies and message.content:
        lines.extend(["", message_text(message.content, message.mailbox_url)])
    return "\n".join(lines)


def _message_html(message: Message, config: Settings) -> str:
    """Render metadata, original body layout and the public inbox footer."""
    metadata = _message_metadata(message, config)
    sender = escape(strip_html(message.sender))
    content = f"Autor: <strong>{sender}</strong><br>\n"
    content += "<br>\n".join(escape(line) for line in metadata[1:])
    if config.email_include_message_bodies and message.content:
        original = message_html(message.content, message.mailbox_url)
        content += f'<div style="margin-top:16px">{original}</div>'
    footer = ""
    if message.mailbox_url:
        url = escape(message.mailbox_url, quote=True)
        footer = (
            '<div style="margin-top:24px;padding-top:16px;border-top:1px solid #d0d0d0;'
            'font-family:Arial,sans-serif">'
            f'<a href="{url}" style="display:inline-block;padding:10px 14px;'
            'border:1px solid #999;border-radius:6px;text-decoration:none;font-weight:600">'
            "Otwórz skrzynkę wiadomości</a></div>"
        )
    return (
        '<html><body><div style="font-family:Arial,sans-serif">'
        f"{content}</div>\n"
        f"{footer}</body></html>"
    )


async def _queue_email(
    db: Database,
    identity: str,
    subject: str,
    body: str,
    html_body: str | None = None,
) -> list[str]:
    """Queue one email per recipient, returning only newly inserted delivery keys."""
    date_header = format_datetime(datetime.now(UTC))
    new_keys: list[str] = []
    for recipient in dict.fromkeys(settings.email_to):
        key = hashlib.sha256(f"{identity}\0{recipient}".encode()).hexdigest()
        inserted = await db.enqueue_email(
            key,
            settings.email_from,
            recipient,
            subject,
            body,
            make_msgid(domain="vulcan-notify.local"),
            date_header,
            html_body=html_body,
        )
        if inserted:
            new_keys.append(key)
    return new_keys


async def queue_messages(result: FullSyncResult, db: Database) -> None:
    """Queue separate notifications, deduplicated by upstream message and recipient."""
    if not settings.email_enabled or result.is_first_message_sync:
        return
    for message in result.new_messages:
        # Keep the original subject while flattening upstream line breaks for headers.
        title = " ".join(message.subject.split())
        subject = f"{settings.email_message_subject_prefix} {title}".strip()
        identity = f"message:{message.api_global_key or message.id}"
        body = format_message(message, settings)
        html_body = _message_html(message, settings)
        if message.mailbox_url:
            body += f"\n\nOtwórz skrzynkę wiadomości:\n{message.mailbox_url}"
        await _queue_email(db, identity, subject, body, html_body)
    await db.commit()


async def queue_remarks(result: FullSyncResult, db: Database) -> None:
    """One praise/note per recipient, using student-scoped upstream identity."""
    if not settings.email_enabled:
        return
    for sr in result.student_results:
        if sr.is_first_sync or sr.is_first_remarks_sync:
            continue
        for change in sr.new_remarks:
            remark = change.raw
            if not isinstance(remark, Remark):
                continue
            title = " ".join(strip_html(f"{sr.student.name}: {remark.category}").split())
            subject = f"{settings.email_remark_subject_prefix} {title}".strip()
            metadata = [
                f"Autor: {strip_html(remark.author)}",
                f"Data: {_format_message_date(remark.date, settings)}",
                f"Kategoria: {strip_html(remark.category)}",
            ]
            if remark.points is not None:
                metadata.append(f"Punkty: {remark.points:g}")
            body = "\n".join(metadata) + "\n\n" + message_text(remark.content, remark.url)
            html_body = '<html><body><div style="font-family:Arial,sans-serif">'
            html_body += "<br>\n".join(escape(line) for line in metadata)
            html_body += (
                f'<div style="margin-top:16px">{message_html(remark.content, remark.url)}</div>'
            )
            if remark.url:
                body += f"\n\nOtwórz pochwały i uwagi:\n{remark.url}"
                html_body += (
                    '<div style="margin-top:24px"><a style="display:inline-block;'
                    "padding:10px 14px;border:1px solid #999;border-radius:6px;"
                    'text-decoration:none;font-weight:600" '
                    f'href="{escape(remark.url, quote=True)}">Otwórz pochwały i uwagi</a></div>'
                )
            html_body += "</div></body></html>"
            await _queue_email(db, f"remark:{sr.student.key}:{remark.id}", subject, body, html_body)
    await db.commit()


async def queue_summary(result: FullSyncResult, db: Database) -> None:
    """Persist the plain digest before attempting optional AI or SMTP."""
    if not settings.email_enabled:
        return
    body, count = format_summary(result)
    if not count:
        return
    subject = f"{settings.email_subject_prefix}: {count} change(s)"
    new_keys = await _queue_email(db, result.notification_id, subject, body)
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
    if row["html_body"]:
        message.add_alternative(row["html_body"], subtype="html")

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
        log.info("Email notifications: SMTP accepted=%d, pending=%d", delivered, pending)
    return delivered, pending


async def publish_email(result: FullSyncResult, db: Database) -> None:
    """Optional output; failures must not stop other notification channels."""
    if not settings.email_enabled:
        return
    try:
        # Persist individual messages before optional AI preparation of the digest.
        await queue_messages(result, db)
        await queue_remarks(result, db)
        await queue_summary(result, db)
        await drain_email_outbox(db)
    except Exception as exc:
        log.warning("Email output failed (%s)", type(exc).__name__)
