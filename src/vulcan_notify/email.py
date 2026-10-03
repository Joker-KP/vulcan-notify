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
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from vulcan_notify.config import parse_email_sender, settings
from vulcan_notify.email_digest import GROUPS, render_summary
from vulcan_notify.email_rendering import render_email, render_template
from vulcan_notify.models import Remark
from vulcan_notify.summarizer import lessons_context, summarize
from vulcan_notify.text import message_html, message_text, strip_html

if TYPE_CHECKING:
    from vulcan_notify.config import Settings
    from vulcan_notify.db import Database
    from vulcan_notify.models import Message, Student
    from vulcan_notify.sync import FullSyncResult

log = logging.getLogger(__name__)
_WEEKDAYS_PL = ("poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela")
AUTH_FAILURE_STATE = "email:auth_failure"
AuthFailureReason = Literal[
    "recovery_failed", "credentials_missing", "session_expired", "interactive_failed"
]
_AUTH_FAILURE_REASONS: dict[AuthFailureReason, str] = {
    "recovery_failed": (
        "Zapisana sesja jest niedostępna lub nieważna. Automatyczne odzyskanie dostępu "
        "z użyciem trwałego profilu Chromium i skonfigurowanych danych logowania "
        "nie zakończyło się utworzeniem nowej sesji."
    ),
    "credentials_missing": (
        "Zapisana sesja jest niedostępna lub nieważna. Brak danych VULCAN_LOGIN / "
        "VULCAN_PASSWORD uniemożliwia automatyczne odzyskanie dostępu; "
        "profil Chromium nie był uruchamiany."
    ),
    "session_expired": (
        "Sesja ponownie wygasła podczas synchronizacji po próbie automatycznego "
        "odzyskania dostępu. Potrzebne jest logowanie interaktywne."
    ),
    "interactive_failed": (
        "Logowanie interaktywne nie zakończyło się zapisaniem nowej sesji. "
        "Sprawdź dostęp do eduVULCAN oraz możliwość zapisu plików sesji."
    ),
}


def format_auth_failure(reason: AuthFailureReason, config: Settings) -> tuple[str, str]:
    """Recovery instructions in the shared layout, without raw authentication errors."""
    html = render_email(
        "Wymagane logowanie do eduVULCAN",
        render_template(
            "auth_failure",
            reason=escape(_AUTH_FAILURE_REASONS[reason]),
            detected_at=escape(_format_message_date(datetime.now(UTC).isoformat(), config)),
        ),
    )
    return message_text(html), html


async def queue_auth_failure(db: Database, reason: AuthFailureReason) -> None:
    """One alert per recipient and outage, persisted across sync process restarts."""
    if not settings.email_enabled:
        return
    identity = await db.get_state(AUTH_FAILURE_STATE) or f"auth_failure:{uuid4().hex}"
    body, html = format_auth_failure(reason, settings)
    subject = f"{settings.email_subject_prefix} Błąd logowania — wymagana nowa sesja".strip()
    await _queue_email(db, identity, subject, body, html)
    await db.set_state(AUTH_FAILURE_STATE, identity)
    # Enqueue and the outage marker must survive or roll back together.
    await db.commit()


async def clear_auth_failure(db: Database) -> None:
    """Re-arm the alert after authentication succeeds; preserve queued retries."""
    if not settings.email_enabled:
        return
    try:
        if await db.get_state(AUTH_FAILURE_STATE):
            await db.set_state(AUTH_FAILURE_STATE, "")
            await db.commit()
    except Exception as exc:
        log.warning("Email authentication alert reset failed (%s)", type(exc).__name__)


async def publish_auth_failure(db: Database, reason: AuthFailureReason) -> None:
    """Queue and retry authentication alerts even when no sync result is available."""
    if not settings.email_enabled:
        return
    try:
        await queue_auth_failure(db, reason)
        await drain_email_outbox(db)
    except Exception as exc:
        log.warning("Email authentication alert failed (%s)", type(exc).__name__)


def format_summary(result: FullSyncResult) -> tuple[str, int]:
    """Text preview of the HTML digest, also used as input to optional AI."""
    _, body, _, count = render_summary(result, settings)
    return body, count


def _format_message_date(value: str, config: Settings) -> str:
    """Display ISO timestamps in the household zone; naive timestamps mean UTC."""
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return value
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    local = stamp.astimezone(config.timezone)
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


def _message_student(message: Message, result: FullSyncResult) -> Student | None:
    """Use mailbox identity; retain unambiguous label matching for older results."""
    if message.mailbox_key:
        matches = [
            sr.student
            for sr in result.student_results
            if sr.student.mailbox_key == message.mailbox_key
        ]
        return matches[0] if len(matches) == 1 else None
    mailbox = message.mailbox.strip()
    parts = mailbox.rsplit(" - ", 2)
    matches = []
    for sr in result.student_results:
        student = sr.student
        if mailbox == student.name.strip() or (
            len(parts) == 3
            and parts[1].strip() == student.name.strip()
            and parts[2].strip() == f"({student.school.strip()})"
        ):
            matches.append(student)
    return matches[0] if len(matches) == 1 else None


def _message_heading(student: Student | None) -> tuple[str, str]:
    if student:
        return strip_html(student.name), strip_html(f"{student.class_name} · {student.school}")
    return "Wiadomość z eduVULCAN", ""


def format_message(message: Message, config: Settings, student: Student | None = None) -> str:
    """One upstream message, retaining its sender, date and mailbox identity."""
    heading, context = _message_heading(student)
    lines = [heading]
    if context:
        lines.append(context)
    lines.extend(["", message.subject, "", *_message_metadata(message, config)])
    if config.email_include_message_bodies and message.content:
        lines.extend(["", message_text(message.content, message.mailbox_url)])
    return "\n".join(lines)


def _message_html(message: Message, config: Settings, student: Student | None = None) -> str:
    """Render metadata, original body layout and the public inbox footer."""
    metadata = _message_metadata(message, config)
    sender = escape(strip_html(message.sender))
    metadata_html = f"Autor: <strong>{sender}</strong><br>\n"
    metadata_html += "<br>\n".join(escape(line) for line in metadata[1:])
    content = ""
    if config.email_include_message_bodies and message.content:
        content = message_html(message.content, message.mailbox_url)
    heading, context = _message_heading(student)
    return render_email(
        heading,
        render_template(
            "message",
            subject=escape(message.subject),
            metadata=metadata_html,
            content=content,
            footer=_notification_footer(message.mailbox_url, "Otwórz skrzynkę wiadomości"),
        ),
        heading_context=context,
    )


def _notification_footer(url: str | None, label: str) -> str:
    if not url:
        return ""
    return render_template("notification_footer", url=escape(url, quote=True), label=escape(label))


def format_remark(remark: Remark, student: Student, config: Settings) -> tuple[str, str]:
    """Text and templated HTML for one note, retaining its full sanitized content."""
    metadata = [
        f"Autor: {strip_html(remark.author)}",
        f"Data: {_format_message_date(remark.date, config)}",
        f"Kategoria: {strip_html(remark.category)}",
    ]
    if remark.points is not None:
        metadata.append(f"Punkty: {remark.points:g}")
    body = "\n".join(metadata) + "\n\n" + message_text(remark.content, remark.url)
    if remark.url:
        body += f"\n\nOtwórz pochwały i uwagi:\n{remark.url}"
    html = render_email(
        "Pochwały i uwagi",
        render_template(
            "remark",
            student_name=escape(strip_html(student.name)),
            student_context=escape(strip_html(f"{student.class_name} · {student.school}")),
            category=escape(strip_html(remark.category)),
            metadata="<br>\n".join(escape(line) for line in metadata),
            content=message_html(remark.content, remark.url),
            footer=_notification_footer(remark.url, "Otwórz pochwały i uwagi"),
        ),
    )
    return body, html


async def _queue_email(
    db: Database,
    identity: str,
    subject: str,
    body: str,
    html_body: str | None = None,
) -> list[str]:
    """Queue one email per recipient, returning only newly inserted delivery keys."""
    date_header = format_datetime(datetime.now(settings.timezone))
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
        student = _message_student(message, result)
        body = format_message(message, settings, student)
        html_body = _message_html(message, settings, student)
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
            body, html_body = format_remark(remark, sr.student, settings)
            await _queue_email(db, f"remark:{sr.student.key}:{remark.id}", subject, body, html_body)
    await db.commit()


async def queue_summary(result: FullSyncResult, db: Database) -> None:
    """Persist both digest alternatives before attempting optional AI or SMTP."""
    if not settings.email_enabled:
        return
    subject, body, html_body, count = render_summary(result, settings)
    if not count:
        return
    new_keys = await _queue_email(db, result.notification_id, subject, body, html_body)
    await db.commit()

    # Retry uses stored bodies. AI adds a summary while retaining every group,
    # its facts and student-specific links in both alternatives.
    if new_keys and settings.email_ai_summary and settings.llm_api_key:
        try:
            ai_input = body
            if settings.llm_include_lessons:
                student_keys = [
                    sr.student.key
                    for sr in result.student_results
                    if any(
                        change.item_type == group and settings.email_digest_groups.get(group, True)
                        for change in sr.all_changes
                        for group in GROUPS
                    )
                ]
                context = await lessons_context(db, settings, student_keys=student_keys)
                if context:
                    ai_input += "\n\n" + context
            replacement = await asyncio.wait_for(
                summarize(ai_input, settings),
                timeout=settings.email_ai_timeout_seconds,
            )
        except Exception as exc:
            log.warning("Email AI summary failed (%s); using grouped digest", type(exc).__name__)
            replacement = None
        if replacement and replacement.strip():
            ai_text = replacement.strip()
            ai_html = (
                '<section style="margin:20px 0;padding:16px;background:#f8fafc">'
                '<h2 style="margin:0 0 12px;font-size:20px">Podsumowanie AI</h2>'
                f"<p>{escape(ai_text).replace(chr(10), '<br>')}</p></section>"
            )
            html_body = html_body.replace("</h1>", "</h1>\n" + ai_html, 1)
            for key in new_keys:
                await db.update_email_body(key, f"{ai_text}\n\n{body}", html_body=html_body)
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
