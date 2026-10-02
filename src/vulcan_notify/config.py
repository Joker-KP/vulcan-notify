"""Configuration via environment variables and .env file."""

from email.errors import HeaderParseError
from email.headerregistry import Address, AddressHeader, HeaderRegistry
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings


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
    }

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

    # Polling. sync-loop.sh reads POLL_INTERVAL from the environment, so this is the
    # single source of truth for both the loop and the staleness threshold below.
    poll_interval: int = 1800  # seconds

    # Quiet window, in the container's local time. sync-loop.sh reads these same two
    # env vars to decide when to pause; they live here too so /api/health can subtract
    # the pause from data age. Without that the two disagreed and a normal overnight
    # sleep read as an outage -- see freshness.py.
    # Evaluated in this zone, not the container's. The LXC runs on UTC, which quietly
    # turned a 00:00-05:00 window into 02:00-07:00 local -- the loop went quiet two
    # hours after midnight and resumed half an hour before the kids left, so the
    # morning schedule was always five hours stale.
    quiet_hours_tz: str = "Europe/Warsaw"
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
    email_subject_prefix: str = "eduVULCAN"
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
        if "\r" in self.email_subject_prefix or "\n" in self.email_subject_prefix:
            raise ValueError("EMAIL_SUBJECT_PREFIX must be a single line")
        if bool(self.smtp_username) != bool(self.smtp_password):
            raise ValueError("Set SMTP_USERNAME and SMTP_PASSWORD together, or leave both unset")
        return self

    # Calendar (macOS Calendar via AppleScript, empty map = disabled)
    calendar_map: dict[str, str] = {}  # student name -> calendar name
    calendar_reminder_hours: int = 24  # alarm trigger (hours before event)

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
