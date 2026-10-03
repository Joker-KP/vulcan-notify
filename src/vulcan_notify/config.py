"""Configuration via environment variables and .env file."""

import logging
import os
import time
from email.errors import HeaderParseError
from email.headerregistry import Address, AddressHeader, HeaderRegistry
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings

EmailDigestGroup = Literal[
    "grade", "attendance", "substitution", "cancellation", "addition", "exam", "homework"
]


def parse_email_sender(value: str) -> Address:
    """Parse one sender mailbox, optionally including a display name."""
    error = "EMAIL_FROM must contain one valid email address, optionally with a display name"
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(error)
    try:
        header = HeaderRegistry()("From", value)
    except (HeaderParseError, ValueError):
        raise ValueError(error) from None
    if (
        not isinstance(header, AddressHeader)
        or header.defects
        or len(header.addresses) != 1
        or any(group.display_name is not None for group in header.groups)
    ):
        raise ValueError(error)
    sender = header.addresses[0]
    if not sender.username or not sender.domain:
        raise ValueError(error)
    return sender


class Settings(BaseSettings):
    # .env also contains settings read by auth.py, the API and startup scripts.
    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
        "hide_input_in_errors": True,
        "populate_by_name": True,
    }

    # One zone for process clocks, logs, scheduling and rendered timestamps.
    # QUIET_HOURS_TZ is accepted only as a legacy alias; TZ takes precedence.
    tz: str = Field(
        default="Europe/Warsaw",
        validation_alias=AliasChoices("TZ", "QUIET_HOURS_TZ", "quiet_hours_tz"),
    )

    @field_validator("tz")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("TZ must be a valid IANA timezone name") from None
        return value

    @property
    def timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.tz)
        except (ZoneInfoNotFoundError, ValueError):
            logging.getLogger(__name__).warning("Unknown timezone; using UTC")
            return ZoneInfo("UTC")

    @property
    def quiet_hours_tz(self) -> str:
        """Compatibility accessor; there is no independent quiet-hours zone."""
        return self.tz

    @quiet_hours_tz.setter
    def quiet_hours_tz(self, value: str) -> None:
        self.tz = value

    # Session file path (cookies from browser login)
    session_file: Path = Path("session.json")

    # Auto-login credentials (optional - enables browser recovery/credential login)
    # Can also be read from macOS Keychain (service: vulcan-notify)
    vulcan_login: str | None = None
    vulcan_password: str | None = None

    # ntfy.sh
    ntfy_topic: str = "vulcan-notify"
    ntfy_server: str = "https://ntfy.sh"

    # Sync
    sync_attendance_days: int = 90  # how far back to sync attendance
    sync_message_backfill_batch: int = 10  # messages to backfill per cycle
    sync_history_keep_days: int = 90  # sync_runs / sync_sections retention

    # sync-loop.sh independently reads POLL_INTERVAL; keep its default aligned.
    poll_interval: int = 1800  # seconds

    # Quiet hours use TZ in both sync-loop.sh and freshness.py.
    quiet_hours_start: int = Field(default=0, ge=0, le=23)
    quiet_hours_end: int = Field(default=5, ge=0, le=23)

    # Data older than this is reported stale by /api/health and the `_meta` block.
    # Two missed cycles: one late sync is normal, two means something is wrong.
    # Measured with quiet hours excluded, so this stays a tight daytime threshold.
    stale_after_seconds: int = 3600

    # Storage
    db_path: Path = Path("vulcan_notify.db")

    # Message filtering (comma-separated sender names, empty = show all)
    message_sender_whitelist: list[str] = []

    # LLM (optional - all providers use OpenAI-compatible API)
    llm_base_url: str = "https://api.cerebras.ai/v1"
    llm_api_key: str | None = None
    llm_model: str = "gpt-oss-120b"
    prompts_file: Path = Path("prompts.toml")

    # SMTP digests (optional). Lists in .env use JSON, e.g. EMAIL_TO=["you@example.org"].
    email_enabled: bool = False
    email_from: str = ""
    email_to: list[str] = []
    email_subject_prefix: str = "[eduVulcan]"
    # Omitted groups are enabled; JSON object in .env, e.g. {"attendance": false}.
    email_digest_groups: dict[EmailDigestGroup, bool] = Field(default_factory=dict)
    email_message_subject_prefix: str = "[Nowa wiadomość]"
    email_remark_subject_prefix: str = "[Uwagi]"
    email_include_message_bodies: bool = False
    email_ai_summary: bool = False
    email_ai_timeout_seconds: float = Field(default=30, gt=0)
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_timeout_seconds: float = Field(default=30, gt=0)

    @model_validator(mode="after")
    def validate_email(self) -> "Settings":
        if not self.email_enabled:
            return self
        if not self.smtp_host.strip() or not self.email_from or not self.email_to:
            raise ValueError("EMAIL_ENABLED requires SMTP_HOST, EMAIL_FROM and EMAIL_TO")
        parse_email_sender(self.email_from)
        # Recipients remain bare addresses; never echo invalid configuration values.
        for address in self.email_to:
            if (
                address.count("@") != 1
                or not all(address.split("@"))
                or any(char.isspace() or char in ",;<>" for char in address)
            ):
                raise ValueError("EMAIL_TO must contain bare email addresses")
        for name in (
            "email_subject_prefix",
            "email_message_subject_prefix",
            "email_remark_subject_prefix",
        ):
            prefix = getattr(self, name)
            if "\r" in prefix or "\n" in prefix:
                raise ValueError(f"{name.upper()} must be a single line")
        if bool(self.smtp_username) != bool(self.smtp_password):
            raise ValueError("Set SMTP_USERNAME and SMTP_PASSWORD together, or leave both unset")
        return self

    # Calendar (macOS Calendar via AppleScript, empty map = disabled)
    calendar_map: dict[str, str] = {}  # student name -> calendar name
    calendar_reminder_hours: int = 24  # alarm trigger (hours before event)
    calendar_timeout_seconds: float = Field(default=30, gt=0)

    # MQTT (optional - publish changes to Mosquitto broker)
    mqtt_enabled: bool = False
    mqtt_broker: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_topic_prefix: str = "school"
    mqtt_status_suffix: str = "status"  # retained heartbeat topic under the prefix

    # Short display names for push notifications (full name -> nickname).
    # Keeps the notification title terse on a watch / lock screen.
    display_name_map: dict[str, str] = {}

    # Logging
    log_level: str = "INFO"


settings = Settings()

# Settings also loads TZ from .env for standalone Python commands. Apply it before
# clocks/loggers/browser subprocesses are used; database writes use explicit UTC.
os.environ["TZ"] = settings.tz
if hasattr(time, "tzset"):
    time.tzset()
