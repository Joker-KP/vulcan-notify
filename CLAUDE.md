# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

CLI tool that syncs data from the eduVulcan school e-journal (grades, attendance, exams, homework, praise/notes, messages) to a local SQLite database and detects changes between syncs. Uses HTTP cookie validation and persistent Chromium recovery before credential login. Manual headed authentication is available through the explicit `vulcan-auth` Compose profile. Read `AGENTS.md` for current local Docker/auth behavior.

Completed lessons (`/api/RealizacjaZajec13`, `status=1`) are also synchronized using
`CompletedLesson` and the additive `completed_lessons` table keyed by student/ID.
`SYNC_COMPLETED_LESSONS_DAYS` defaults to 90 plus today. The independent successful
baseline `last_sync:<student>:completed_lessons` suppresses initial/import history.
New/content-update events use MQTT `completed_lessons/new` and `/updated`;
missing records are soft-deleted only within the fetched interval and restoration
is silent. TUI key 7 opens the list/detail view; `/api/completed-lessons` supports
common student filters, limits and freshness. Completed lesson changes do not
trigger emails or form digest groups. `LLM_INCLUDE_LESSONS=true` optionally
adds stored topics as AI context for email/CLI change summaries, with
`LLM_LESSONS_DAYS=7` local calendar days including today. Standalone
`summarize --type lessons [--days N]` uses `[lessons]` prompts.
`summarize --type mix [--days N]` (default 7) separately uses `[messages]` and
`[lessons]` to email at most two nonempty summaries through dedicated
`summary_messages.html` / `summary_lessons.html` subtemplates and `layout.html`.
Prompts request Markdown; mixed emails render sanitized HTML, with inbox and
active student lesson-list links from the saved session (omitted if unavailable).
Requires enabled SMTP email and an LLM key, independently of digest AI/context
switches. Final bodies persist in the existing outbox; retries do not rerun AI.
Default prompts group by student/subject and omit routine activities.
Collections/resources retain JSON
without assumptions about unverified populated upstream shapes. Live behavior
was not verified during implementation.

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

- `sync.py` - `sync_all()` orchestrates per-student sync: fetch data via client, diff against stored state, upsert into database. Returns `SyncResult` per student. First sync stores baseline without reporting changes. Praise/notes also use their own successful-fetch baseline (`last_sync:<student>:remarks`) for upgrades, with new-ID events and silent updates/soft deletes.

  Grades, attendance, exams, homework and schedule now also have independent successful-section baselines. Failed initial sections baseline silently on recovery. Legacy initialization preserves prior confirmed successes (or the student marker when no section history exists). Schedule comparison covers the full requested local window, including missing boundary days.

- `api.py` - `/api/students` discovers stable profile keys. Student endpoints support `student_key` and `keyed=1`; unique-name response keys remain compatible, while ambiguous names return HTTP 409. Calendar feeds accept explicit profile keys; historical profile merging requires a matching nonempty mailbox identity.

- Session writes are atomic and mode 0600. Invalid session files use the existing recovery policy without exposing file contents. Calendar transient failures preserve stored UIDs for retry; `CALENDAR_TIMEOUT_SECONDS` bounds AppleScript and kills/reaps timed-out or cancelled processes.

- Exhausted/missing authentication and explicit interactive auth failures notify through the existing email outbox when `EMAIL_ENABLED=true`. `auth_failure.html` shares the email layout and includes noVNC/SSH recovery instructions. The persisted `email:auth_failure` outage marker suppresses duplicate alerts across restarts and resets after completed non-expired syncs or successful interactive auth. No raw authentication errors, secrets or AI are included.

- `differ.py` - Compares fetched API data against stored database rows. `diff_grades()` detects new/updated grades by column_id. `diff_attendance()` detects new records by (date, lesson_number). Returns `Change` dataclasses.

- `display.py` - Formats `SyncResult` for terminal output with ANSI colors (auto-disabled when piped). Groups by student, then by data type.

- `email.py` - Optional per-sync SMTP digest of student changes plus a separate email per new message or praise/note, with persistent per-recipient retries. `EMAIL_REMARK_SUBJECT_PREFIX` defaults to `[Uwagi]`; full note content and a student-specific Pochwały i uwagi link are included, excluded from digest/AI input. Message notifications include a text link and HTML inbox button derived from the session tenant, stored in the outbox for retries. The digest uses separate HTML templates for seven groups, with per-student module links and included group names/count in the subject (count omitted for one change). `EMAIL_DIGEST_GROUPS` controls digest inclusion before counting/AI. Separate `message.html` and `remark.html` templates share `layout.html` and a button footer with the digest style. AI adds a summary above the groups; message bodies are excluded by default. See `docs/email.md` for configuration and delivery limits.

- `db.py` - `Database` class wrapping aiosqlite. Normalized tables: students, grades, attendance, exams, homework, remarks, messages, sync_state. The additive `remarks` table is keyed by `(student_key, id)` and retains original numeric type/kind, full content and soft-deleted history. Entity writes use ON CONFLICT DO UPDATE for idempotent upserts. Per-section outcomes and confirmed-fetch timestamps drive freshness checks.

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

`vulcan-api` serves port 8585 independently. `vulcan-sync` runs `sync-loop.sh` through `entrypoint-xvfb.sh`; the wrapper forwards its supplied command. `vulcan-auth` is an explicit GUI service with noVNC at loopback port 6080. All share `./data:/app/data`. The image uses CMD and has no ENTRYPOINT; `entrypoint.sh` was removed. TZ defaults to Europe/Warsaw for all runtime clocks, logs, Chromium, scheduling and displayed dates; QUIET_HOURS_TZ is a legacy fallback. SQLite and Python persistence explicitly use UTC, preserving existing history. Stop the worker during manual auth or a one-off sync.
