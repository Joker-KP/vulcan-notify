"""HTML change groups; the text alternative is derived from the same content."""

from __future__ import annotations

import re
from datetime import datetime
from html import escape
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from vulcan_notify.client import STUDENT_PAGE_PATHS
from vulcan_notify.email_rendering import render_email, render_template
from vulcan_notify.models import AttendanceEntry, Exam, Grade, Homework, Lesson
from vulcan_notify.text import message_text, strip_html
from vulcan_notify.time_utils import as_utc

if TYPE_CHECKING:
    from vulcan_notify.config import EmailDigestGroup, Settings
    from vulcan_notify.differ import Change
    from vulcan_notify.sync import FullSyncResult

# Order is shared by the subject and each student's sections.
# Completed lesson changes stay outside these groups; AI can use stored topic context.
GROUPS: dict[EmailDigestGroup, str] = {
    "grade": "Oceny",
    "attendance": "Frekwencja",
    "substitution": "Zastępstwa",
    "cancellation": "Anulowane zajęcia",
    "addition": "Dodatkowe zajęcia",
    "exam": "Sprawdziany",
    "homework": "Zadania domowe",
}
FAILURE_NOTICE = (
    "Nie udało się pobrać części danych. Podsumowanie obejmuje tylko poprawnie wykryte zmiany."
)


def _text(value: str) -> str:
    return escape(strip_html(value)).replace("\n", "<br>")


def _grade_value(value: str) -> str:
    """Color numeric grades and their +/- variants, leaving other marks neutral."""
    match = re.fullmatch(r"[+-]?([1-6])[+-]?", value.strip())
    text = _text(value)
    if not match:
        return text
    # Dark red, orange, yellow, guacamole, green, darker green.
    colors = ("#991b1b", "#f97316", "#facc15", "#7fb446", "#32c167", "#15AD4F")
    return f'<span style="color:{colors[int(match[1]) - 1]}">{text}</span>'


def _date(value: str, config: Settings) -> str:
    """Keep school dates as dates; convert instants using the display timezone."""
    try:
        if "T" in value or " " in value:
            return (
                as_utc(datetime.fromisoformat(value))
                .astimezone(config.timezone)
                .strftime("%Y-%m-%d")
            )
        return datetime.strptime(value, "%d.%m.%Y").strftime("%Y-%m-%d")
    except ValueError:
        return value


def _item(change: Change, config: Settings) -> str:
    """Category-specific content; source text is escaped before entering templates."""
    raw = change.raw
    title = change.title
    title_html = None
    details: list[str] = [change.body] if change.body else []
    if isinstance(raw, Grade):
        value = _grade_value(raw.value)
        if change.old_value:
            value = f"{_grade_value(change.old_value)} → {value}"
        title_html = f"{_text(raw.subject)}: {value}"
        details = [raw.column_name, f"Waga: {raw.weight:g}", f"Nauczyciel: {raw.teacher}"]
    elif isinstance(raw, AttendanceEntry):
        category = {2: "Nieobecność", 3: "Spóźnienie", 4: "Usprawiedliwiona nieobecność"}.get(
            raw.category, f"Kategoria {raw.category}"
        )
        title = f"{raw.subject}: {category}"
        details = [f"Lekcja: {raw.lesson_number}", f"Nauczyciel: {raw.teacher}"]
    elif isinstance(raw, Exam):
        title = raw.subject
        details = [raw.description or ""]
        if raw.teacher:
            details.append(f"Nauczyciel: {raw.teacher}")
    elif isinstance(raw, Homework):
        title = raw.subject
        details = [raw.content or ""]
        if raw.teacher:
            details.append(f"Nauczyciel: {raw.teacher}")
    elif isinstance(raw, Lesson):
        title = raw.subject
        details = [f"Godziny: {raw.time_from[11:16]}-{raw.time_to[11:16]}"]
        if raw.sub_teacher and raw.sub_teacher != raw.teacher:
            if change.item_type == "substitution":
                details.append(f"Nauczyciel: {raw.sub_teacher} → {raw.teacher}")
            else:
                details.append(f"Nauczyciel: {raw.teacher} → {raw.sub_teacher}")
        elif raw.teacher:
            details.append(f"Nauczyciel: {raw.teacher}")
        if raw.sub_room and raw.sub_room != raw.room:
            details.append(f"Sala: {raw.room or '?'} → {raw.sub_room}")
        elif raw.room:
            details.append(f"Sala: {raw.room}")
        details.extend([raw.absence_info or "", raw.remarks or ""])
    elif change.item_type == "cancellation":
        title = title.removeprefix("Cancelled: ")
    date = getattr(raw, "date", "")
    if date:
        details.append(f"Data: {_date(date, config)}")
    detail_html = "<br>".join(_text(detail) for detail in details if detail)
    return (
        '<li style="margin:0 0 14px;padding:0">'
        f"<strong>{title_html if title_html is not None else _text(title)}</strong>"
        f'<div style="margin-top:4px;color:#475569">{detail_html}</div></li>'
    )


def _footer(portal_url: str | None, kind: str) -> str:
    # Synthetic/older results can lack a tenant URL. Never invent an account URL.
    if not isinstance(portal_url, str):
        return ""
    parsed = urlsplit(portal_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        return ""
    url = escape(f"{portal_url.rstrip('/')}/{STUDENT_PAGE_PATHS[kind]}", quote=True)
    label = {
        "grade": "Otwórz oceny",
        "attendance": "Otwórz frekwencję",
        "exam": "Otwórz sprawdziany i zadania domowe",
        "homework": "Otwórz sprawdziany i zadania domowe",
    }.get(kind, "Otwórz plan zajęć")
    return (
        '<p style="margin:16px 0 0;padding-top:12px;border-top:1px solid #e2e8f0">'
        f'<a href="{url}" style="color:#1d4ed8">{label}</a></p>'
    )


def render_summary(result: FullSyncResult, config: Settings) -> tuple[str, str, str, int]:
    """Return subject, derived text, HTML and count for non-baseline digest changes."""
    sections: list[str] = []
    present: set[str] = set()
    count = 0
    for sr in result.student_results:
        if sr.is_first_sync:
            continue
        groups = {
            kind: [change for change in sr.all_changes if change.item_type == kind]
            for kind in GROUPS
            if config.email_digest_groups.get(kind, True)
        }
        if not any(groups.values()):
            continue
        student = sr.student
        sections.append(
            '<div style="margin:28px 0 12px">'
            f'<h2 style="margin:0;font-size:22px">{_text(student.name)}</h2>'
            f'<p style="margin:4px 0;color:#64748b">'
            f"{_text(student.class_name)} · {_text(student.school)}</p></div>"
        )
        for kind, changes in groups.items():
            if not changes:
                continue
            present.add(kind)
            count += len(changes)
            sections.append(
                render_template(
                    kind,
                    heading=GROUPS[kind],
                    count=str(len(changes)),
                    items="\n".join(_item(change, config) for change in changes),
                    footer=_footer(sr.portal_url, kind),
                )
            )
    if result.has_failures:
        sections.append(f'<p style="color:#92400e">{FAILURE_NOTICE}</p>')
    names = ", ".join(name for kind, name in GROUPS.items() if kind in present)
    subject = f"{config.email_subject_prefix} {names}".strip()
    if count > 1:
        noun = "zmiany" if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14) else "zmian"
        subject += f" (sumarycznie {count} {noun})"
    html = render_email("Zmiany w Dzienniku eduVULCAN", "\n".join(sections))
    return subject, message_text(html, include_links=True), html, count
