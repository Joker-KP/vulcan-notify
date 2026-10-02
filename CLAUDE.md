# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

CLI tool that syncs data from the eduVulcan school e-journal (grades, attendance, exams, homework, messages) to a local SQLite database and detects changes between syncs. Uses HTTP cookie validation and persistent Chromium recovery before credential login. Manual headed authentication is available through the explicit `vulcan-auth` Compose profile. Read `AGENTS.md` for current local Docker/auth behavior.

## Sibling repos (cross-repo work is common)

Changes here almost always need a corresponding change in the homelab repo — new API endpoints need HA REST sensors, new MQTT topics need HA automations, new deploy behavior needs CLAUDE.md updates. Always check the homelab repo before assuming infra/docs don't exist, and update both in a single session when a change spans them.

- **homelab** (`/Users/ostaps/code/homelab`) — Proxmox + HA + Jellyfin + arr-stack docs and config. Owns the LXC 103 deploy environment, HA `configuration.yaml` REST sensors that call this daemon's API, the `MQTT-TEST` automation that forwards our MQTT events to push notifications, and the "School" Lovelace dashboard. See `homelab/vulcan-ha-integration.md` for the integration plan and `homelab/notifications.md` for the MQTT→push flow.

Cross-repo permission is granted for `/Users/ostaps/code/homelab/**` (Read/Edit/Write) + git ops in `.claude/settings.local.json`, so you can operate on that repo without prompts.

## Commands

```bash
# Install dependencies (including dev tools: pytest, ruff, mypy, pre-commit)
uv sync --all-extras

# Authenticate (opens browser)
uv run vulcan-notify auth

# Test session validity
uv run vulcan-notify test

# Sync data and show changes (default command)
uv run vulcan-notify sync

# Run all tests
uv run pytest

# Run a single test file
uv run pytest tests/test_sync.py -x -q

# Lint and format
uv run ruff check --fix .
uv run ruff format .

# Type checking
uv run mypy src/
```

## Architecture

The tool follows a linear pipeline: **Auth -> Client -> Sync -> Diff -> Display**.

- `auth.py` - Persistent Playwright Chromium under `/app/data/chromium-profile`, guarded by a shared profile lock. Imports `session.json` cookies, tries browser reuse before credentials and saves refreshed session state. Interactive auth always forces headed mode; automatic selection opens the first journal profile. Also provides `cookies_for_url()` and `_make_ssl_context()` used by the client.

- `client.py` - `VulcanClient` wraps aiohttp for the uczen.eduvulcan.pl JSON API. Handles cookie auth, SSL (certifi), and session expiry detection (HTML response = expired). Returns typed dataclasses from `models.py`.

- `models.py` - Dataclasses for all API response types: `Student`, `Grade`, `AttendanceEntry`, `Exam`, `Homework`, `ClassificationPeriod`, `DashboardData`.

- `sync.py` - `sync_all()` orchestrates per-student sync: fetch data via client, diff against stored state, upsert into database. Returns `SyncResult` per student. First sync stores baseline without reporting changes.

- `differ.py` - Compares fetched API data against stored database rows. `diff_grades()` detects new/updated grades by column_id. `diff_attendance()` detects new records by (date, lesson_number). Returns `Change` dataclasses.

- `display.py` - Formats `SyncResult` for terminal output with ANSI colors (auto-disabled when piped). Groups by student, then by data type.

- `email.py` - Optional per-sync SMTP digest of student changes plus a separate email per new message, with persistent per-recipient retries. Message notifications include a text link and HTML inbox button derived from the session tenant, stored in the outbox for retries. AI replacement applies to the change digest only; message bodies are excluded by default. See `docs/email.md` for configuration and delivery limits.

- `db.py` - `Database` class wrapping aiosqlite. Normalized tables: students, grades, attendance, exams, homework, messages, sync_state. Entity writes use ON CONFLICT DO UPDATE for idempotent upserts. Per-section outcomes and confirmed-fetch timestamps drive freshness checks.

- `config.py` - `pydantic-settings` `Settings` singleton loaded from `.env`.

## Key dependencies

- **playwright** - browser automation for auth flow only
- **aiohttp** - HTTP client for eduVulcan API
- **aiosqlite** - async SQLite storage
- **certifi** - CA certificates for SSL
- **pydantic-settings** - config from environment variables

## Testing patterns

- Tests use `pytest-asyncio` with `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed)
- The `db` fixture in `conftest.py` provides a temporary SQLite database
- `VulcanClient` is mocked with `AsyncMock` in sync tests
- aiohttp is mocked with `MagicMock` in client tests (see `_mock_response` pattern in `test_client.py`)

## API reference

See `docs/eduvulcan-api.md` for the reverse-engineered eduVulcan web API documentation.

## Docker startup

`vulcan-api` serves port 8585 independently. `vulcan-sync` runs `sync-loop.sh` through `entrypoint-xvfb.sh`; the wrapper forwards its supplied command. `vulcan-auth` is an explicit GUI service with noVNC at loopback port 6080. All share `./data:/app/data`. The image uses CMD and has no ENTRYPOINT; `entrypoint.sh` was removed. Quiet hours use Europe/Warsaw separately from the UTC database clock. Stop the worker during manual auth or a one-off sync.
