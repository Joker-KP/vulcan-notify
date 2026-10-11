"""The example environment includes settings owned by several readers."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from vulcan_notify.config import Settings


def test_example_environment_accepts_auth_and_startup_settings():
    config = Settings(_env_file=Path(__file__).parents[1] / ".env.example")
    assert config.poll_interval > 0
    assert config.quiet_hours_tz


def test_deployment_environment_does_not_reject_shell_settings(tmp_path):
    env = tmp_path / ".env"
    env.write_text("DISPLAY=:99\nAPI_PORT=8585\nVULCAN_BROWSER_HEADLESS=false\n")
    assert Settings(_env_file=env).session_file is not None


def test_lesson_summary_environment_uses_short_names(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LLM_INCLUDE_LESSONS=true\nLLM_LESSONS_DAYS=3\n")
    config = Settings(_env_file=env)
    assert config.llm_include_lessons is True
    assert config.llm_lessons_days == 3


def test_weekly_summary_can_be_disabled_in_environment(tmp_path):
    env = tmp_path / ".env"
    env.write_text("WEEKLY_SUMMARY_ENABLED=false\n")
    assert Settings(_env_file=env).weekly_summary_enabled is False


def test_homework_windows_are_configurable_and_nonnegative(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SYNC_HOMEWORK_DAYS=30\nSYNC_HOMEWORK_FUTURE_DAYS=60\n")
    config = Settings(_env_file=env)
    assert config.sync_homework_days == 30 and config.sync_homework_future_days == 60
    for field in ("sync_homework_days", "sync_homework_future_days"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{field: -1})
