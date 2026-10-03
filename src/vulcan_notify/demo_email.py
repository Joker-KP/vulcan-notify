"""Generate synthetic school changes and exercise the real sync/email pipeline."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import random
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from uuid import uuid4

from vulcan_notify.client import VulcanClient
from vulcan_notify.config import settings
from vulcan_notify.db import Database
from vulcan_notify.email import drain_email_outbox, queue_summary
from vulcan_notify.email_digest import render_summary
from vulcan_notify.models import (
    AttendanceEntry,
    ClassificationPeriod,
    Exam,
    Grade,
    Homework,
    Lesson,
    Remark,
    Student,
    SubjectSummary,
)
from vulcan_notify.sync import FullSyncResult, sync_student

DEMO_MARKER = "demo:email"
EXPECTED_COUNTS = dict.fromkeys(
    ("grade", "substitution", "addition", "attendance", "exam", "homework"), 3
)


class DemoClient(VulcanClient):
    """Two snapshots of a fictional student; no eduVULCAN requests are made."""

    def __init__(self, seed: int | None = None) -> None:
        super().__init__({"base_url": "https://example.invalid"})
        self.baseline = True
        token = uuid4()
        self.student = Student(
            f"email-demo-{token.hex}", "Uczeń TESTOWY", "6T", "Szkoła TESTOWA", -1, ""
        )
        rng = random.Random(seed)
        subjects = rng.sample(
            ["Matematyka", "Język polski", "Język angielski", "Historia", "Biologia"], 3
        )
        today = datetime.now(settings.timezone).replace(hour=0, minute=0, second=0, microsecond=0)
        year = today.year if today.month >= 9 else today.year - 1
        self.period = ClassificationPeriod(1, 1, f"{year}-09-01", f"{year + 1}-01-31")
        self.grades: list[Grade] = []
        self.attendance: list[AttendanceEntry] = []
        self.exams: list[Exam] = []
        self.homework: list[Homework] = []
        self.original_lessons: list[Lesson] = []
        self.lessons: list[Lesson] = []
        # Negative IDs and unique student keys keep separate demo runs independent.
        id_base = -(token.int % (2**50) + 10)
        for i, subject in enumerate(subjects):
            teacher = rng.choice(["Anna Testowa", "Piotr Przykładowy", "Maria Demonstracyjna"])
            past = today - timedelta(days=i + 1)
            while past.weekday() >= 5:
                past -= timedelta(days=1)
            future = today + timedelta(days=i + 1)
            while future.weekday() >= 5:
                future += timedelta(days=1)
            column = rng.choice(["Kartkówka", "Sprawdzian", "Odpowiedź ustna"])
            self.grades.append(
                Grade(
                    id_base - i,
                    rng.choice(["3+", "4", "4+", "5", "5-", "6"]),
                    past.strftime("%d.%m.%Y"),
                    subject,
                    column,
                    column,
                    rng.choice([1, 2, 3]),
                    teacher,
                    False,
                    period_id=1,
                )
            )
            self.attendance.append(
                AttendanceEntry(
                    i + 1,
                    2,
                    past.date().isoformat(),
                    subject,
                    teacher,
                    (past + timedelta(hours=8 + i)).isoformat(),
                    (past + timedelta(hours=8 + i, minutes=45)).isoformat(),
                )
            )
            self.exams.append(
                Exam(
                    id_base - i,
                    future.date().isoformat(),
                    subject,
                    rng.choice([1, 2]),
                    f"TEST: powtórzenie materiału z działu {i + 1}.",
                    teacher,
                )
            )
            self.homework.append(
                Homework(
                    id_base - i,
                    future.date().isoformat(),
                    subject,
                    f"TEST: zadania {i + 1}-{i + 3} ze strony {rng.randint(10, 80)}.",
                    teacher,
                )
            )
            lesson = Lesson(
                future.date().isoformat(),
                (future + timedelta(hours=8 + i)).isoformat(),
                (future + timedelta(hours=8 + i, minutes=45)).isoformat(),
                subject,
                teacher,
                str(101 + i),
                None,
                0,
                False,
            )
            self.original_lessons.append(lesson)
            self.lessons.append(
                replace(
                    lesson,
                    annotation=1,
                    sub_teacher="Jan Zastępujący",
                    sub_room=str(rng.randint(201, 210)),
                    sub_type=1,
                    absence_info="TEST: nieobecność nauczyciela",
                )
            )
            self.lessons.append(
                replace(
                    lesson,
                    is_extra=True,
                    time_from=(future + timedelta(hours=14 + i)).isoformat(),
                    time_to=(future + timedelta(hours=14 + i, minutes=45)).isoformat(),
                )
            )

    async def _fetch(self, url: str, *, extra_headers: dict[str, str] | None = None) -> Any:
        raise RuntimeError("DemoClient cannot contact eduVULCAN")

    async def get_periods(self, student: Student) -> list[ClassificationPeriod]:
        return [self.period]

    async def get_grades_and_summaries(
        self, student: Student, period: ClassificationPeriod
    ) -> tuple[list[Grade], list[SubjectSummary]]:
        return ([] if self.baseline else self.grades), []

    async def get_attendance(
        self, student: Student, date_from: str, date_to: str
    ) -> list[AttendanceEntry]:
        return [] if self.baseline else self.attendance

    async def get_exams(self, student: Student) -> list[Exam]:
        return [] if self.baseline else self.exams

    async def get_homework(self, student: Student) -> list[Homework]:
        return [] if self.baseline else self.homework

    async def get_exam_detail(self, student: Student, exam_id: int) -> dict[str, Any]:
        exam = next(item for item in self.exams if item.id == exam_id)
        return {"opis": exam.description, "nauczycielImieNazwisko": exam.teacher}

    async def get_homework_detail(self, student: Student, homework_id: int) -> dict[str, Any]:
        item = next(item for item in self.homework if item.id == homework_id)
        return {"opis": item.content, "nauczycielImieNazwisko": item.teacher}

    async def get_schedule(self, student: Student, date_from: str, date_to: str) -> list[Lesson]:
        return self.original_lessons if self.baseline else self.lessons

    async def get_remarks(self, student: Student) -> list[Remark]:
        return []


def check_demo_database(path: Path) -> None:
    """Check existing files read-only, before Database.connect can run migrations."""
    if path.resolve() == settings.db_path.resolve():
        raise ValueError("Wskaż osobną bazę testową, inną niż DB_PATH.")
    if not path.exists():
        return
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            row = connection.execute(
                "SELECT value FROM sync_state WHERE key = ?", (DEMO_MARKER,)
            ).fetchone()
    except sqlite3.Error:
        raise ValueError("Istniejący plik nie jest bazą demonstracyjną.") from None
    if row != ("1",):
        raise ValueError("Istniejąca baza nie jest bazą demonstracyjną.")


async def generate_demo(db: Database, seed: int | None = None) -> FullSyncResult:
    client = DemoClient(seed)
    baseline = await sync_student(client, db, client.student)
    if baseline.has_failures or baseline.has_changes or not baseline.is_first_sync:
        raise RuntimeError("Nie udało się utworzyć stanu początkowego.")
    client.baseline = False
    result = await sync_student(client, db, client.student)
    counts = Counter(change.item_type for change in result.all_changes)
    if result.has_failures or counts != EXPECTED_COUNTS:
        raise RuntimeError("Synchronizacja testowa nie wykryła oczekiwanych 18 zmian.")
    return FullSyncResult([result])


def save_email_preview(path: Path, row: dict[str, str]) -> None:
    message = EmailMessage()
    for header, key in (
        ("From", "sender"),
        ("To", "recipient"),
        ("Subject", "subject"),
        ("Message-ID", "message_id"),
        ("Date", "date_header"),
    ):
        message[header] = row[key]
    message.set_content(row["body"])
    if row["html_body"]:
        message.add_alternative(row["html_body"], subtype="html")
    path.write_bytes(message.as_bytes())


async def run_demo(
    path: Path, *, send: bool = False, retry: bool = False, seed: int | None = None
) -> int:
    if (send or retry) and not settings.email_enabled:
        raise ValueError("Wysyłka wymaga EMAIL_ENABLED=true i konfiguracji SMTP w .env.")
    if retry and not path.exists():
        raise ValueError("Brak bazy testowej z kolejką do ponowienia.")
    check_demo_database(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Only this demo's outbox is drained; the production worker can keep running.
    with path.with_suffix(path.suffix + ".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Inny proces korzysta z tej bazy testowej.") from None
        db = Database(path)
        try:
            await db.connect()
            await db.set_state(DEMO_MARKER, "1")
            await db.commit()
            if not retry:
                if send and await db.list_email_outbox():
                    raise ValueError("W kolejce jest poprzedni test. Użyj --retry.")
                result = await generate_demo(db, seed)
                _, body, html_body, count = render_summary(result, settings)
                stored_count = sum(len(sr.all_changes) for sr in result.student_results)
                print(f"Zapisano {stored_count} zmian: po 3 w każdej z 6 kategorii.")
                if count != stored_count:
                    print(f"Po wyborze grup w mailu uwzględniono {count} zmian.")
                if send:
                    if not count:
                        raise ValueError(
                            "Wszystkie grupy testowe są wyłączone w EMAIL_DIGEST_GROUPS."
                        )
                    await queue_summary(result, db)
                    rows = await db.list_email_outbox()
                    if not rows:
                        raise RuntimeError("Nie utworzono powiadomienia w kolejce SMTP.")
                    # Capture the real queued body, including any configured AI summary.
                    body = rows[0]["body"]
                    html_body = rows[0]["html_body"]
                    mail_path = path.with_suffix(".eml")
                    save_email_preview(mail_path, rows[0])
                    print(f"Podgląd wiadomości: {mail_path}")
                preview = path.with_suffix(".txt")
                preview.write_text(body + "\n", encoding="utf-8")
                print(f"Podgląd treści: {preview}")
                html_preview = path.with_suffix(".html")
                html_preview.write_text(html_body, encoding="utf-8")
                print(f"Podgląd HTML: {html_preview}")
            if send or retry:
                delivered, pending = await drain_email_outbox(db)
                print(f"SMTP zaakceptował: {delivered}; pozostało w kolejce: {pending}.")
                return 1 if pending else 0
            print("Podgląd gotowy. Dodaj --send, aby wysłać nowy zestaw na EMAIL_TO z .env.")
            return 0
        finally:
            await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Testowy mail: po 3 wpisy w 6 kategoriach.")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data/email-demo.db"),
        help="Osobna baza testowa (domyślnie data/email-demo.db).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--send", action="store_true", help="Wyślij przez SMTP z .env.")
    mode.add_argument("--retry", action="store_true", help="Ponów kolejkę bez nowych danych.")
    parser.add_argument("--seed", type=int, help="Ziarno losowania przykładowej treści.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    try:
        code = asyncio.run(run_demo(args.db, send=args.send, retry=args.retry, seed=args.seed))
    except (ValueError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
