"""HTTP client for the eduVulcan web API (uczen.eduvulcan.pl)."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp

from vulcan_notify.auth import _make_ssl_context, cookies_for_url
from vulcan_notify.models import (
    AttendanceEntry,
    ClassificationPeriod,
    CompletedLesson,
    DashboardData,
    Exam,
    Grade,
    Homework,
    Lesson,
    Message,
    Remark,
    Student,
    SubjectSummary,
)

logger = logging.getLogger(__name__)

# Public student-app modules used by notification links (not API endpoints).
STUDENT_PAGE_PATHS = {
    "grade": "oceny",
    "attendance": "frekwencja",
    "substitution": "planZajec",
    "cancellation": "planZajec",
    "addition": "planZajec",
    "exam": "sprawdzianyZadaniaDomowe",
    "homework": "sprawdzianyZadaniaDomowe",
    "completed_lesson": "realizacjaZajec",
}

# Mimic a real Chrome browser to avoid bot detection
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "X-Requested-With": "XMLHttpRequest",
}

# Random delay range between requests (seconds)
_MIN_DELAY = 0.3
_MAX_DELAY = 1.5

# Bounded retry for transient upstream faults
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 2.0

# Per-request ceiling. Without this aiohttp applies its 5-minute default, which is
# how a single cycle wedges the whole poll loop.
_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=15)


class SessionExpiredError(Exception):
    """Raised when the session cookies are no longer valid."""


class VulcanFetchError(Exception):
    """Raised when a fetch fails or returns something we cannot parse.

    This exists so that "upstream is broken" is structurally distinct from
    "the student genuinely has nothing". Collapsing the two into an empty list
    is what let a Vulcan outage look like a quiet week all the way up to the
    dashboard.
    """

    def __init__(self, message: str, *, url: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status = status


def _retryable(status: int) -> bool:
    """5xx and 429 are worth another attempt; 4xx generally are not."""
    return status >= 500 or status == 429


class VulcanClient:
    """Async client for the eduVulcan web API.

    Uses saved browser session cookies for authentication.
    """

    def __init__(self, session_data: dict[str, Any]) -> None:
        self._session_data = session_data
        self._base_url: str = session_data["base_url"]
        self._tenant: str = session_data.get("tenant") or urlsplit(self._base_url).path.strip("/")
        self._messages_base = f"https://wiadomosci.eduvulcan.pl/{self._tenant}"
        self._ssl_ctx = _make_ssl_context()
        self._http: aiohttp.ClientSession | None = None

    def _cookie_header(self) -> str:
        url = f"{self._base_url}/api/"
        cookies = cookies_for_url(self._session_data, url)
        return "; ".join(f"{k}={v}" for k, v in cookies.items())

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            headers = {**_BROWSER_HEADERS, "Cookie": self._cookie_header()}
            headers["Referer"] = f"{self._base_url}/App"
            headers["Origin"] = self._base_url
            self._http = aiohttp.ClientSession(headers=headers, timeout=_TIMEOUT)
        return self._http

    @staticmethod
    async def _jitter() -> None:
        """Random delay between requests to mimic human browsing."""
        await asyncio.sleep(random.uniform(_MIN_DELAY, _MAX_DELAY))

    async def close(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()

    async def _fetch(self, url: str, *, extra_headers: dict[str, str] | None = None) -> Any:
        """GET a URL and return parsed JSON, retrying transient faults.

        Raises SessionExpiredError when the response is HTML (login redirect) and
        VulcanFetchError when the request fails for any other reason. It never
        returns None -- a caller that gets a value back knows the fetch succeeded.
        """
        last_error: Exception | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            await self._jitter()
            session = await self._ensure_session()

            try:
                async with session.get(url, ssl=self._ssl_ctx, headers=extra_headers or {}) as resp:
                    content_type = resp.headers.get("content-type", "")

                    if resp.status in (401, 403) or (
                        resp.status == 200 and "text/html" in content_type.lower()
                    ):
                        # Login redirect. Let the caller re-auth; retrying won't help.
                        raise SessionExpiredError(
                            "Session expired. Run 'vulcan-notify auth' to re-authenticate."
                        )

                    if resp.status != 200:
                        error = VulcanFetchError(
                            # Response bodies can contain private data or tokens.
                            f"HTTP {resp.status} for {url.split('?')[0]}",
                            url=url,
                            status=resp.status,
                        )
                        if not _retryable(resp.status) or attempt == _MAX_ATTEMPTS:
                            raise error
                        last_error = error
                        logger.warning(
                            "HTTP %d for %s (attempt %d/%d), retrying",
                            resp.status,
                            url,
                            attempt,
                            _MAX_ATTEMPTS,
                        )
                    else:
                        data = await resp.json()
                        if data is None:
                            raise VulcanFetchError("Unexpected null API response", url=url)
                        return data

            except (TimeoutError, aiohttp.ClientError) as exc:
                last_error = exc
                if attempt == _MAX_ATTEMPTS:
                    raise VulcanFetchError(
                        f"{type(exc).__name__} for {url}: {exc}", url=url
                    ) from exc
                logger.warning(
                    "%s for %s (attempt %d/%d), retrying",
                    type(exc).__name__,
                    url,
                    attempt,
                    _MAX_ATTEMPTS,
                )

            await asyncio.sleep(_BACKOFF_BASE ** (attempt - 1))

        # Unreachable: the final attempt either returns or raises.
        raise VulcanFetchError(f"exhausted retries for {url}: {last_error}", url=url)

    async def _request_url(self, url: str) -> Any:
        """Make a GET request to an absolute URL. Returns parsed JSON.

        Builds a per-request Cookie header matching the URL's domain,
        since different subdomains need different cookies.
        """
        cookie_header = "; ".join(
            f"{k}={v}" for k, v in cookies_for_url(self._session_data, url).items()
        )
        return await self._fetch(url, extra_headers={"Cookie": cookie_header, "Referer": url})

    async def _request(self, path: str) -> Any:
        """Make a GET request to the API. Returns parsed JSON."""
        return await self._fetch(f"{self._base_url}{path}")

    # ── Student context ──────────────────────────────────────────────

    def student_portal_url(self, student: Student) -> str:
        """Public student app URL, without session cookies or tokens."""
        return f"{self._base_url.rstrip('/')}/App/{quote(student.key, safe='')}"

    def student_module_url(self, student_key: str, module: str) -> str:
        """Public module URL for a locally stored student profile."""
        return (
            f"{self._base_url.rstrip('/')}/App/{quote(student_key, safe='')}/"
            f"{STUDENT_PAGE_PATHS[module]}"
        )

    @property
    def message_inbox_url(self) -> str:
        """Public unified inbox URL for this tenant."""
        return f"{self._messages_base}/App/odebrane"

    async def get_students(self) -> list[Student]:
        data = await self._request("/api/Context")
        # A 200 that doesn't carry the key we expect means the API shape moved under
        # us (eduVULCAN has redesigned before). That is a failure, not an empty roster.
        if not isinstance(data, dict) or "uczniowie" not in data:
            raise VulcanFetchError("/api/Context response has no 'uczniowie' key")

        return [
            Student(
                key=s["key"],
                name=s["uczen"],
                class_name=s["oddzial"],
                school=s["jednostka"],
                diary_id=s["idDziennik"],
                mailbox_key=s.get("globalKeySkrzynka", ""),
            )
            for s in data["uczniowie"]
            if s.get("aktywny", True)
        ]

    async def get_remarks(self, student: Student) -> list[Remark]:
        """Fetch both praise and notes from the student's Pochwały i uwagi view."""
        key = quote(student.key, safe="")
        data = await self._request(f"/api/Uwagi?key={key}")
        if not isinstance(data, list):
            raise VulcanFetchError("/api/Uwagi response is not a list")
        remarks: list[Remark] = []
        required = {"id", "data", "kategoria", "typ", "autor", "tresc", "rodzaj", "liczbaPunktow"}
        for item in data:
            if not isinstance(item, dict) or not required.issubset(item):
                raise VulcanFetchError("/api/Uwagi item is missing required fields")
            if any(
                not isinstance(item[name], str) for name in ("data", "kategoria", "autor", "tresc")
            ):
                raise VulcanFetchError("/api/Uwagi item has invalid text fields")
            for name in ("id", "typ", "rodzaj"):
                value = item[name]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or value != int(value)
                ):
                    raise VulcanFetchError("/api/Uwagi item has invalid numeric fields")
            points = item["liczbaPunktow"]
            if points is not None and (
                isinstance(points, bool) or not isinstance(points, (int, float))
            ):
                raise VulcanFetchError("/api/Uwagi item has invalid points")
            remarks.append(
                Remark(
                    id=int(item["id"]),
                    date=item["data"],
                    category=item["kategoria"],
                    type=int(item["typ"]),
                    author=item["autor"],
                    content=item["tresc"],
                    kind=int(item["rodzaj"]),
                    points=points,
                    url=f"{self.student_portal_url(student)}/pochwalyUwagi",
                )
            )
        return remarks

    async def get_completed_lessons(
        self, student: Student, date_from: str, date_to: str
    ) -> list[CompletedLesson]:
        """Fetch completed lessons only; retain unverified resource shapes as JSON."""
        endpoint = "/api/RealizacjaZajec13"
        data = await self._request(
            f"{endpoint}?key={quote(student.key, safe='')}&dataOd={quote(date_from, safe='')}"
            f"&dataDo={quote(date_to, safe='')}&status=1"
        )
        if not isinstance(data, list):
            raise VulcanFetchError(f"{endpoint} response is not a list")
        lessons: dict[int, CompletedLesson] = {}
        text_fields = ("data", "przedmiot", "nauczyciel", "tematOpis", "blokTematyczny", "online")
        required = {
            *text_fields,
            "id",
            "nrLekcji",
            "kolekcjePoLekcji",
            "existsKolekcjePoLekcji",
            "zasoby",
        }
        for item in data:
            if not isinstance(item, dict) or not required.issubset(item):
                raise VulcanFetchError(f"{endpoint} item is missing required fields")
            if any(not isinstance(item[name], str) for name in text_fields):
                raise VulcanFetchError(f"{endpoint} item has invalid text fields")
            for name in ("id", "nrLekcji"):
                value = item[name]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise VulcanFetchError(f"{endpoint} item has invalid numeric fields")
                if not float(value).is_integer():
                    raise VulcanFetchError(f"{endpoint} item has invalid numeric fields")
            try:
                datetime.fromisoformat(item["data"])
            except ValueError:
                raise VulcanFetchError(f"{endpoint} item has invalid date") from None
            if not isinstance(item["kolekcjePoLekcji"], list) or not isinstance(
                item["existsKolekcjePoLekcji"], bool
            ):
                raise VulcanFetchError(f"{endpoint} item has invalid collection fields")
            lesson = CompletedLesson(
                id=int(item["id"]),
                date=item["data"],
                lesson_number=int(item["nrLekcji"]),
                subject=item["przedmiot"],
                teacher=item["nauczyciel"],
                topic=item["tematOpis"],
                thematic_block=item["blokTematyczny"],
                online=item["online"],
                collections=item["kolekcjePoLekcji"],
                has_collections=item["existsKolekcjePoLekcji"],
                resources=item["zasoby"],
                url=self.student_module_url(student.key, "completed_lesson"),
            )
            if lesson.id in lessons and lessons[lesson.id] != lesson:
                raise VulcanFetchError(f"{endpoint} has conflicting duplicate IDs")
            lessons[lesson.id] = lesson
        return list(lessons.values())

    # ── Grades ───────────────────────────────────────────────────────

    async def get_periods(self, student: Student) -> list[ClassificationPeriod]:
        data = await self._request(
            f"/api/OkresyKlasyfikacyjne?key={student.key}&idDziennik={student.diary_id}"
        )
        if not data:
            return []

        return [
            ClassificationPeriod(
                id=p["id"],
                number=p["numerOkresu"],
                date_from=p["dataOd"],
                date_to=p["dataDo"],
            )
            for p in data
        ]

    async def get_grades(self, student: Student, period: ClassificationPeriod) -> list[Grade]:
        grades, _ = await self.get_grades_and_summaries(student, period)
        return grades

    async def get_grades_and_summaries(
        self, student: Student, period: ClassificationPeriod
    ) -> tuple[list[Grade], list[SubjectSummary]]:
        """Fetch /api/Oceny once, parse both per-grade rows and per-subject summaries."""
        data = await self._request(
            f"/api/Oceny?key={student.key}&idOkresKlasyfikacyjny={period.id}"
        )
        if not isinstance(data, dict) or "ocenyPrzedmioty" not in data:
            raise VulcanFetchError("/api/Oceny response has no 'ocenyPrzedmioty' key")

        grades: list[Grade] = []
        summaries: list[SubjectSummary] = []
        for subject in data["ocenyPrzedmioty"]:
            subject_name = subject.get("przedmiotNazwa", "")
            final = subject.get("ocenaOkresowa")
            proposed = subject.get("proponowanaOcenaOkresowa")
            # Vulcan returns ' ' (single space) for empty cells — normalize to None.
            if isinstance(final, str) and not final.strip():
                final = None
            if isinstance(proposed, str) and not proposed.strip():
                proposed = None
            summaries.append(
                SubjectSummary(
                    subject=subject_name,
                    period_id=period.id,
                    final_grade=final,
                    proposed_final_grade=proposed,
                    use_weighted_average=bool(subject.get("uwzglednijWageOcen", True)),
                )
            )
            for column in subject.get("kolumnyOcenyCzastkowe") or []:
                for grade in column.get("oceny", []):
                    grades.append(
                        Grade(
                            column_id=grade.get("idKolumny", column.get("idKolumny", 0)),
                            # Vulcan returns an explicit null (not an absent key) for some
                            # fields, so dict.get(key, default) yields None rather than the
                            # default. These columns are NOT NULL in the DB, so coalesce the
                            # null to "" — otherwise upsert_grade raises IntegrityError, the
                            # grade never persists, and it re-publishes to MQTT every cycle.
                            value=grade.get("wpis") or "",
                            date=grade.get("dataOceny", ""),
                            subject=subject_name,
                            column_name=grade.get("nazwaKolumny")
                            or column.get("nazwaKolumny")
                            or "",
                            category=grade.get("kategoriaKolumny")
                            or column.get("kategoriaKolumny")
                            or "",
                            weight=float(grade.get("waga", 1) or 1),
                            teacher=grade.get("nauczyciel", ""),
                            changed_since_login=grade.get("zmienionaOdOstatniegoLogowania", False),
                            period_id=period.id,
                            superseded_by_grade_id=grade.get("idOcenaPoprawiona"),
                        )
                    )
        return grades, summaries

    # ── Attendance ───────────────────────────────────────────────────

    async def get_attendance(
        self, student: Student, date_from: str, date_to: str
    ) -> list[AttendanceEntry]:
        """Fetch attendance. date_from/date_to are ISO 8601 strings."""
        data = await self._request(
            f"/api/Frekwencja?key={student.key}&dataOd={date_from}&dataDo={date_to}"
        )
        if not data:
            return []

        entries: list[AttendanceEntry] = []
        for entry in data.get("oddzialy", []):
            entries.append(
                AttendanceEntry(
                    lesson_number=entry.get("numerLekcji", 0),
                    category=entry.get("kategoriaFrekwencji", 0),
                    date=entry.get("data", ""),
                    subject=entry.get("opisZajec", ""),
                    teacher=entry.get("nauczyciel", ""),
                    time_from=entry.get("godzinaOd", ""),
                    time_to=entry.get("godzinaDo", ""),
                )
            )
        return entries

    # ── Dashboard (Tablica) endpoints ────────────────────────────────

    async def get_exams(self, student: Student) -> list[Exam]:
        data = await self._request(f"/api/SprawdzianyTablica?key={student.key}")
        if not data:
            return []
        return [
            Exam(
                id=e["id"],
                date=e.get("data", ""),
                subject=e.get("przedmiot", ""),
                type=e.get("rodzaj", 0),
            )
            for e in data
        ]

    async def get_homework(self, student: Student) -> list[Homework]:
        data = await self._request(f"/api/ZadaniaDomoweTablica?key={student.key}")
        if not data:
            return []
        return [
            Homework(
                id=h["id"],
                date=h.get("data", ""),
                subject=h.get("przedmiot", ""),
            )
            for h in data
        ]

    # ── Detail endpoints ────────────────────────────────────────────

    async def get_homework_detail(
        self, student: Student, homework_id: int
    ) -> dict[str, Any] | None:
        """Fetch homework detail (description, teacher, attachments)."""
        data = await self._request(
            f"/api/ZadanieDomoweSzczegoly?key={student.key}&id={homework_id}"
        )
        if not data or not isinstance(data, dict):
            return None
        return dict(data)

    async def get_exam_detail(self, student: Student, exam_id: int) -> dict[str, Any] | None:
        """Fetch exam detail (description, teacher)."""
        data = await self._request(f"/api/SprawdzianSzczegoly?key={student.key}&id={exam_id}")
        if not data or not isinstance(data, dict):
            return None
        return dict(data)

    async def get_dashboard(self, student: Student) -> DashboardData:
        """Fetch all dashboard (Tablica) data concurrently for a student."""
        grades_task = self._request(f"/api/OcenyTablica?key={student.key}")
        attendance_task = self._request(f"/api/FrekwencjaTablica?key={student.key}")
        exams_task = self.get_exams(student)
        homework_task = self.get_homework(student)
        announcements_task = self._request(f"/api/OgloszeniaTablica?key={student.key}")
        messages_task = self._request("/api/WiadomosciNieodczytane")

        results = await asyncio.gather(
            grades_task,
            attendance_task,
            exams_task,
            homework_task,
            announcements_task,
            messages_task,
            return_exceptions=True,
        )

        def safe_result(r: Any, default: Any = None) -> Any:
            if isinstance(r, Exception):
                logger.warning("Dashboard fetch failed: %s", r)
                return default
            return r if r is not None else default

        unread_data = safe_result(results[5], {})
        unread_count = (
            unread_data.get("liczbaWiadomosciNieodczytanych", 0)
            if isinstance(unread_data, dict)
            else 0
        )

        return DashboardData(
            grades=safe_result(results[0], []),
            attendance=safe_result(results[1], {}),
            exams=safe_result(results[2], []),
            homework=safe_result(results[3], []),
            announcements=safe_result(results[4], []),
            unread_messages=unread_count,
        )

    # ── Schedule (Plan zajęć) ────────────────────────────────────────

    async def get_schedule(
        self,
        student: Student,
        date_from: str,
        date_to: str,
    ) -> list[Lesson]:
        """Fetch the lesson schedule with substitutions for a date range.

        date_from/date_to are ISO-8601 UTC datetimes as expected by the API
        (e.g. `2026-03-31T22:00:00.000Z` for local midnight in Europe/Warsaw).
        """
        data = await self._request(
            f"/api/PlanZajec?key={student.key}&dataOd={date_from}&dataDo={date_to}&zakresDanych=2"
        )
        if not data:
            return []

        lessons: list[Lesson] = []
        for entry in data:
            raw_date = entry.get("data", "")
            iso_date = raw_date[:10]  # "YYYY-MM-DD..." prefix
            zmiany = entry.get("zmiany") or []
            uwagi = entry.get("zmianyUwagi") or []
            sub = zmiany[0] if zmiany else {}
            lessons.append(
                Lesson(
                    date=iso_date,
                    time_from=entry.get("godzinaOd", ""),
                    time_to=entry.get("godzinaDo", ""),
                    subject=entry.get("przedmiot", ""),
                    teacher=entry.get("prowadzacy", ""),
                    room=entry.get("sala", "") or "",
                    group=entry.get("podzial") or None,
                    annotation=entry.get("adnotacja", 0) or 0,
                    is_extra=bool(entry.get("dodatkowe")),
                    sub_teacher=(sub.get("prowadzacy") or None) if sub else None,
                    sub_room=(sub.get("sala") or None) if sub else None,
                    sub_type=sub.get("zmiana") if sub else None,
                    absence_info=sub.get("informacjeNieobecnosc") if sub else None,
                    remarks="; ".join(str(u) for u in uwagi) if uwagi else None,
                )
            )
        return lessons

    # ── Messages (wiadomosci.eduvulcan.pl) ───────────────────────────

    async def get_messages(self, page_size: int = 50) -> list[Message]:
        """Fetch received messages from the messages subdomain.

        Returns up to page_size most recent messages (unified inbox).
        """
        data = await self._request_url(
            f"{self._messages_base}/api/Odebrane?idLastWiadomosc=0&pageSize={page_size}"
        )
        if not data:
            return []

        # Inbox labels need not match Context's pupil/school display names.
        # Resolve the mailbox's stable identity before notification rendering.
        mailbox_keys: dict[str, set[str]] = {}
        try:
            mailboxes = await self._request_url(f"{self._messages_base}/api/Skrzynki")
            if not isinstance(mailboxes, list):
                raise VulcanFetchError("/api/Skrzynki response is not a list")
            for mailbox in mailboxes:
                name = mailbox.get("nazwa")
                key = mailbox.get("globalKey")
                if isinstance(name, str) and isinstance(key, str) and key:
                    mailbox_keys.setdefault(name.strip(), set()).add(key)
        except SessionExpiredError:
            raise
        except Exception as exc:
            logger.warning("Mailbox context unavailable (%s)", type(exc).__name__)
        resolved_mailboxes = {
            name: next(iter(keys)) for name, keys in mailbox_keys.items() if len(keys) == 1
        }

        return [
            Message(
                id=m["id"],
                api_global_key=m.get("apiGlobalKey", ""),
                sender=m.get("korespondenci", ""),
                subject=m.get("temat", "").strip(),
                date=m.get("data", ""),
                mailbox=m.get("skrzynka", ""),
                has_attachments=m.get("hasZalaczniki", False),
                is_read=m.get("przeczytana", False),
                mailbox_url=self.message_inbox_url,
                mailbox_key=resolved_mailboxes.get(m.get("skrzynka", "").strip(), ""),
            )
            for m in data
        ]

    async def get_message_detail(self, api_global_key: str) -> str | None:
        """Fetch full message content (HTML) by its apiGlobalKey."""
        data = await self._request_url(
            f"{self._messages_base}/api/WiadomoscSzczegoly?apiGlobalKey={api_global_key}"
        )
        if not data:
            return None
        tresc = data.get("tresc")
        return str(tresc) if tresc is not None else None
