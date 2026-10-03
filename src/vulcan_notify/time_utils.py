"""UTC storage compatibility and conversion to the application's display zone."""

from contextlib import suppress
from datetime import UTC, datetime

from vulcan_notify.config import settings


def as_utc(moment: datetime) -> datetime:
    """Legacy naive database timestamps mean UTC, independent of process TZ."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def local_timestamp(raw: str) -> str:
    """Render a stored ISO timestamp with the configured zone's explicit offset."""
    return as_utc(datetime.fromisoformat(raw)).astimezone(settings.timezone).isoformat()


def local_storage_timestamps(run: dict[str, object] | None) -> dict[str, object] | None:
    """Convert storage metadata for output without changing stored history."""
    if run is None:
        return None
    result = dict(run)
    for field in ("started_at", "completed_at", "first_seen", "last_seen"):
        raw = result.get(field)
        if isinstance(raw, str):
            # Preserve unparseable legacy diagnostic values.
            with suppress(ValueError):
                result[field] = local_timestamp(raw)
    return result
