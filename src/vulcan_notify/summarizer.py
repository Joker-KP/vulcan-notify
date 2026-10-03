"""AI-powered summary of sync results using OpenAI-compatible API."""

from __future__ import annotations

import json
import logging
import re
import tomllib
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vulcan_notify.config import Settings
    from vulcan_notify.db import Database

from vulcan_notify.time_utils import as_utc

log = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


async def lessons_context(
    db: Database,
    settings: Settings,
    *,
    days: int | None = None,
    student_keys: list[str] | None = None,
    now: datetime | None = None,
) -> str:
    """Format stored topics from N local calendar days, including today, for AI."""
    days = settings.llm_lessons_days if days is None else days
    if days < 1:
        raise ValueError("Completed lesson summary days must be positive")
    now = as_utc(now or datetime.now(UTC))
    local_date = now.astimezone(settings.timezone).date()
    start_date = local_date - timedelta(days=days - 1)
    start = datetime.combine(start_date, time.min, settings.timezone).astimezone(UTC)
    lessons = await db.get_lessons_for_summary(start.isoformat(), now.isoformat(), student_keys)
    if not lessons:
        return ""
    for lesson in lessons:
        lesson["date"] = (
            as_utc(datetime.fromisoformat(str(lesson["date"])))
            .astimezone(settings.timezone)
            .isoformat()
        )
    return (
        f"Tematy zrealizowanych lekcji ({start_date} - {local_date}, "
        f"ostatnie {days} dni, strefa {settings.tz}).\n"
        "To kontekst z lokalnej bazy, a nie lista nowych zmian. Dane mogą być niepełne.\n"
        + json.dumps(lessons, ensure_ascii=False, indent=2)
    )


async def summarize(
    sync_output: str,
    settings: Settings,
    profile: str = "default",
) -> str | None:
    """Generate an AI summary of sync output.

    Returns None if LLM is not configured or on any error.
    """
    if not settings.llm_api_key:
        return None

    try:
        from openai import AsyncOpenAI

        prompts = _load_prompts(settings, profile)
        if prompts is None:
            return None

        system_prompt, user_prompt_template = prompts
        clean_output = _strip_ansi(sync_output)
        user_prompt = user_prompt_template.format(sync_output=clean_output)

        client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
        )
        response = await client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = response.choices[0].message.content
        return content if content else None

    except Exception as exc:
        # Provider errors can contain private prompts or authentication details.
        log.warning("Failed to generate AI summary (%s)", type(exc).__name__)
        return None


_ATTENDANCE_CATEGORIES = {
    2: "Absent",
    3: "Late",
    4: "Excused absence",
    5: "Excused lateness",
    6: "Exempt",
}

_EXAM_TYPES = {
    0: "Test",
    1: "Quiz",
    2: "Oral exam",
}


def format_changes_for_llm(changes: dict[str, list[dict[str, object]]]) -> str:
    """Format structured DB change records into plain text for the LLM."""
    lines: list[str] = []

    if "grades" in changes:
        lines.append("Grades:")
        for g in changes["grades"]:
            lines.append(
                f"  {g['student']} - {g['subject']}: {g['value']} "
                f"({g['column_name']}, weight {g['weight']}, {g['date']})"
            )
        lines.append("")

    if "attendance" in changes:
        lines.append("Attendance:")
        for a in changes["attendance"]:
            cat_id = int(str(a["category"]))
            category = _ATTENDANCE_CATEGORIES.get(cat_id, f"Category {cat_id}")
            lines.append(
                f"  {a['student']} - {a['subject']}: {category} "
                f"(lesson {a['lesson_number']}, {a['date']})"
            )
        lines.append("")

    if "exams" in changes:
        lines.append("Upcoming exams:")
        for e in changes["exams"]:
            exam_type = _EXAM_TYPES.get(int(str(e["type"])), f"Type {e['type']}")
            lines.append(f"  {e['student']} - {e['subject']}: {exam_type} on {e['date']}")
        lines.append("")

    if "homework" in changes:
        lines.append("Homework:")
        for h in changes["homework"]:
            lines.append(f"  {h['student']} - {h['subject']}: due {h['date']}")
        lines.append("")

    return "\n".join(lines)


def _load_prompts(
    settings: Settings,
    profile: str,
) -> tuple[str, str] | None:
    """Load system and user prompts from TOML file."""
    try:
        with open(settings.prompts_file, "rb") as f:
            data = tomllib.load(f)
        section = data.get(profile)
        if not section:
            log.warning("Prompt profile '%s' not found in %s", profile, settings.prompts_file)
            return None
        return section["system"], section["prompt"]
    except (FileNotFoundError, KeyError, tomllib.TOMLDecodeError) as e:
        log.warning("Failed to load prompts from %s: %s", settings.prompts_file, e)
        return None
