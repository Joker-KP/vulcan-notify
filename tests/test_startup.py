"""Exercise quiet-hours scheduling without sleeping or contacting eduVULCAN."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("start", "end", "hour", "quiet"),
    [(0, 5, 1, True), (23, 5, 23, True), (23, 5, 0, True), (23, 5, 12, False), (0, 0, 0, False)],
)
def test_sync_loop_matches_quiet_window(tmp_path, start, end, hour, quiet):
    commands = {
        "date": """#!/bin/bash
case "$*" in
    *%-H*) echo "$TEST_HOUR" ;;
    *tomorrow*) echo 1200 ;;
    *today*) if [ "$TEST_HOUR" -ge 23 ]; then echo 900; else echo 1100; fi ;;
    *%s*) echo 1000 ;;
    *) echo fixture-time ;;
esac
""",
        "uv": '#!/bin/bash\necho "invoked: $*"\n',
        # End the infinite loop at its first scheduled sleep.
        "sleep": '#!/bin/bash\necho "sleep: $*"\nexit 77\n',
    }
    for name, script in commands.items():
        path = tmp_path / name
        path.write_text(script)
        path.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "QUIET_HOURS_START": str(start),
        "QUIET_HOURS_END": str(end),
        "POLL_INTERVAL": "1800",
        "TEST_HOUR": str(hour),
    }
    script = Path(__file__).parents[1] / "sync-loop.sh"
    result = subprocess.run(
        ["bash", str(script)], env=env, text=True, capture_output=True, timeout=5
    )
    assert result.returncode == 77
    assert ("Quiet hours" in result.stdout) is quiet
    assert ("invoked: run vulcan-notify sync" in result.stdout) is not quiet
    if quiet and hour == 23:
        assert "sleep: 200" in result.stdout
