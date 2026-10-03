"""One runtime zone must not reinterpret existing UTC storage timestamps."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from vulcan_notify.config import Settings, settings
from vulcan_notify.freshness import ages
from vulcan_notify.ics import _stale_event
from vulcan_notify.sync import _api_date_window
from vulcan_notify.time_utils import local_timestamp


@pytest.mark.parametrize(
    "environment,env_file,zone,winter,summer",
    [
        ({}, "", "Europe/Warsaw", "+0100", "+0200"),
        ({}, "TZ=Asia/Tokyo\n", "Asia/Tokyo", "+0900", "+0900"),
        ({"QUIET_HOURS_TZ": "Asia/Tokyo"}, "", "Asia/Tokyo", "+0900", "+0900"),
        (
            {"TZ": "Europe/Warsaw", "QUIET_HOURS_TZ": "Asia/Tokyo"},
            "TZ=UTC\n",
            "Europe/Warsaw",
            "+0100",
            "+0200",
        ),
    ],
)
def test_runtime_logs_and_clock_share_the_zone(
    tmp_path, environment, env_file, zone, winter, summer
):
    """Exercise real .env startup/tzset and logging rather than mocked clocks."""
    (tmp_path / ".env").write_text(env_file)
    env = {k: v for k, v in os.environ.items() if k not in ("TZ", "QUIET_HOURS_TZ")}
    env.update(environment)
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json, logging, os, time
from datetime import UTC, datetime
from vulcan_notify.config import settings
from vulcan_notify.__main__ import setup_logging
setup_logging()
formatter = logging.getLogger().handlers[0].formatter
clocks, logs = [], []
for month in (1, 7):
    instant = datetime(2026, month, 15, 12, tzinfo=UTC).timestamp()
    clocks.append(time.strftime('%z', time.localtime(instant)))
    record = logging.LogRecord('fixture', logging.INFO, '', 0, 'fixture', (), None)
    record.created = instant
    logs.append(formatter.format(record).split(" INFO", 1)[0])
print(json.dumps([settings.tz, os.environ['TZ'], clocks, logs]))
""",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    configured, process, clocks, logs = json.loads(result.stdout)
    assert configured == process == zone
    assert clocks == [winter, summer]
    assert logs == [
        f"2026-{month:02d}-15 {12 + int(offset[:3]):02d}:00:00"
        for month, offset in zip((1, 7), clocks, strict=True)
    ]


def test_invalid_timezone_is_rejected_without_echoing_input():
    with pytest.raises(ValidationError, match="valid IANA timezone") as error:
        Settings(_env_file=None, tz="Invalid/PrivateValue")
    assert "PrivateValue" not in str(error.value)


@pytest.mark.parametrize("raw", ["2026-07-15T09:00:00", "2026-07-15T09:00:00Z"])
def test_legacy_utc_and_aware_stamps_have_the_same_age(monkeypatch, raw):
    monkeypatch.setattr(settings, "tz", "Europe/Warsaw")
    now = datetime(2026, 7, 15, 11, 10, tzinfo=ZoneInfo("Europe/Warsaw"))
    assert ages(raw, now) == (600, 600)
    assert local_timestamp(raw) == "2026-07-15T11:00:00+02:00"


@pytest.mark.parametrize(
    "instant,start,end",
    [
        (
            datetime(2026, 3, 29, 12, tzinfo=UTC),
            "2026-03-28T23:00:00.000Z",
            "2026-03-29T21:59:59.999Z",
        ),
        (
            datetime(2026, 10, 25, 12, tzinfo=UTC),
            "2026-10-24T22:00:00.000Z",
            "2026-10-25T22:59:59.999Z",
        ),
    ],
)
def test_api_day_bounds_use_the_offset_at_each_dst_boundary(monkeypatch, instant, start, end):
    monkeypatch.setattr(settings, "tz", "Europe/Warsaw")
    assert _api_date_window(instant, 0, 0) == (start, end)


def test_calendar_warning_uses_local_date_and_time(monkeypatch):
    monkeypatch.setattr(settings, "tz", "Europe/Warsaw")
    warning = _stale_event(
        "fixture", datetime(2026, 7, 15, 21, tzinfo=UTC), datetime(2026, 7, 15, 23, tzinfo=UTC)
    )
    assert "DTSTART;VALUE=DATE:20260716" in warning
    assert "SUMMARY:⚠️ School sync stale since 2026-07-15 23:00 +0200" in warning


async def test_python_freshness_stamps_match_sqlite_utc_clock(db):
    run_id = await db.create_sync_run()
    await db.record_section(run_id, "messages", "ok")
    stamp = datetime.fromisoformat(await db.get_state("last_success::messages"))
    cursor = await db.db.execute("SELECT CURRENT_TIMESTAMP")
    stored = datetime.fromisoformat((await cursor.fetchone())[0]).replace(tzinfo=UTC)
    assert stamp.utcoffset().total_seconds() == 0
    assert abs((stamp - stored).total_seconds()) < 5


async def test_both_health_readers_preserve_legacy_utc_age(db, monkeypatch):
    from vulcan_notify import api
    from vulcan_notify import db as db_module
    from vulcan_notify.db import SECTIONS
    from vulcan_notify.models import Student

    fixed = datetime(2026, 7, 15, 9, 10, tzinfo=UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return (
                fixed.astimezone(tz)
                if tz
                else fixed.astimezone(settings.timezone).replace(tzinfo=None)
            )

    monkeypatch.setattr(settings, "tz", "Europe/Warsaw")
    monkeypatch.setattr(settings, "db_path", db._db_path)
    monkeypatch.setattr(api, "datetime", FixedDatetime)
    monkeypatch.setattr(db_module, "datetime", FixedDatetime)
    await db.upsert_student(Student("fixture", "Example", "3A", "School", 1, ""))
    run_id = await db.create_sync_run()
    await db.complete_sync_run(run_id, "completed")
    await db.db.execute(
        "UPDATE sync_runs SET started_at='2026-07-15 09:00:00', completed_at='2026-07-15 09:00:00'"
    )
    for section in SECTIONS:
        key = "" if section == "messages" else "fixture"
        await db.set_state(f"last_success:{key}:{section}", "2026-07-15T09:00:00")
    await db.commit()
    for health in (await db.get_health(), api._get_health()):
        assert health["status"] == "ok"
        assert health["age_seconds"] == 600
        assert health["generated_at"] == "2026-07-15T11:10:00+02:00"
        assert health["last_run"]["started_at"] == "2026-07-15T11:00:00+02:00"
