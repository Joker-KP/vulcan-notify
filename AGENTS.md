# AGENTS.md

This file provides repository-level guidance for coding agents (especially OpenAI Codex) working on `vulcan-notify`.

It complements `CLAUDE.md`. Read both files before making non-trivial changes. If the two files differ, prefer the instructions that describe the current local implementation and verify the code before assuming either document is fully up to date.

---

## 1. Project purpose

`vulcan-notify` is a self-hosted integration layer for the eduVULCAN school e-journal.

Its purpose is broader than forwarding unread messages. The system should:

1. authenticate to eduVULCAN reliably,
2. collect data from the available eduVULCAN areas/APIs,
3. normalize and persist the data locally,
4. compare each synchronization run with the previous stored state,
5. detect meaningful changes,
6. distribute those changes through one or more output channels,
7. expose current state to external systems such as Homey/Home Assistant, calendars, email clients, dashboards, or other automation software.

The core architectural idea is:

```text
eduVULCAN
   |
   v
Authentication / session management
   |
   v
API / web data acquisition
   |
   v
Normalization
   |
   v
SQLite current + historical state
   |
   v
Change detection
   |
   +--> terminal / logs
   +--> email (planned)
   +--> MQTT
   +--> HTTP API
   +--> iCalendar / calendar integration
   +--> future notification/integration channels
```

The project should remain a small, understandable, self-hosted application rather than evolve into an unnecessarily complex distributed system.

---

## 2. Product scope

Do not treat the current implementation as being limited to messages.

The intended scope is to synchronize as much useful school information as is practical from eduVULCAN, including current and future support for data such as:

- students / profiles,
- grades and grade changes,
- attendance,
- exams / tests / quizzes,
- homework / assignments,
- messages, including message bodies,
- lesson schedule,
- substitutions,
- cancellations,
- additional lessons,
- remarks / behavior notes,
- praise / positive remarks,
- other useful data exposed by eduVULCAN that can be represented cleanly in the local model.

The list above describes product direction. Current implementation status is:

| Data category | Status | Current coverage |
| --- | --- | --- |
| Students | Implemented | API discovery and per-student synchronization. |
| Grades | Implemented | All returned classification periods; new grades and value changes. |
| Classification periods and subject summaries | Implemented persistence | Period metadata, proposed/final grades and weighted-average settings; no separate change events. |
| Attendance | Implemented | Configurable lookback; notifications for new non-present records only. |
| Exams/tests/quizzes and homework | Implemented | Lists and detail backfill; new-item events and soft deletes. No update/delete events. |
| Messages | Implemented with bounded coverage | Unified inbox, latest 50 messages per sync; new-message content and bounded historical content backfill. No full inbox pagination. |
| Lesson schedule and substitutions/cancellations/additions | Implemented with bounded coverage | Previous 7 days through next 14 days; change semantics are described in section 5. |
| Remarks / behavior notes | Documented endpoint, not synchronized | `/api/Uwagi` is documented in `docs/eduvulcan-api.md`; no complete client/model/persistence/diff flow. |
| Praise / positive remarks | Planned | No dedicated synchronization flow. |

Output-channel status is documented in section 6. Implemented coverage does not imply live eduVULCAN behavior has been verified in the current task.

Before modifying or adding a data source, determine whether it is:

1. already fully implemented,
2. partially implemented,
3. documented but not implemented,
4. a planned extension.

Do not claim support for a feature until the code and tests support it.

---

## 3. Existing upstream architecture

The repository originated from the `kintecus/vulcan-notify` project and retains its general architecture.

Important modules include, or may include depending on the current branch:

- `src/vulcan_notify/__main__.py`
  - CLI entry point and subcommand dispatch.

- `src/vulcan_notify/config.py`
  - application settings through `pydantic-settings`; auth, API and startup scripts also read environment variables directly (see section 14).

- `src/vulcan_notify/auth.py`
  - Playwright-based authentication and eduVULCAN session persistence.

- `src/vulcan_notify/client.py`
  - async HTTP client for reverse-engineered eduVULCAN web APIs.

- `src/vulcan_notify/models.py`
  - typed/domain models for synchronized data.

- `src/vulcan_notify/sync.py`
  - orchestration of complete synchronization runs.

- `src/vulcan_notify/differ.py`
  - comparison of freshly fetched data with stored state and creation of change events.

- `src/vulcan_notify/db.py`
  - SQLite persistence.

- `src/vulcan_notify/display.py`
  - terminal/log representation of synchronization results.

- `src/vulcan_notify/mqtt.py`
  - MQTT publication and a persistent retry outbox, with delivery limitations described in section 6.

- `src/vulcan_notify/api.py`
  - HTTP API, currently expected on port `8585`.

- `src/vulcan_notify/ics.py`
  - iCalendar generation for the lesson schedule served by `api.py`.

- `src/vulcan_notify/calendar.py`
  - exams/homework integration with macOS Calendar via AppleScript; unavailable in the Linux Docker image.

- `src/vulcan_notify/summarizer.py`
  - optional AI-based summaries where enabled.

- `tests/`
  - automated test suite.

- `docs/`
  - architecture, deployment and reverse-engineered eduVULCAN API documentation.

Before introducing a new module or abstraction, inspect the current implementation and extend the existing architecture where practical.

---

## 4. Current synchronization model

The preferred conceptual pipeline is:

```text
Auth
  -> Fetch
  -> Normalize
  -> Diff
  -> Persist
  -> Publish
```

A normal synchronization run should:

1. obtain a valid eduVULCAN session,
2. discover the available students/profiles,
3. fetch all configured data categories for each applicable student,
4. convert responses into stable internal models,
5. compare the new data with the SQLite baseline,
6. persist the updated state,
7. emit only meaningful changes,
8. deliver those changes through enabled output channels,
9. record enough information to diagnose the synchronization run.

The database is the system of record for previously observed state.

Change detection must not depend only on what happened during the current process lifetime.

---

## 5. Baseline and change detection

First synchronization must not generate a flood of historical notifications.

### Current baseline behavior

`sync_student()` checks `sync_state["last_sync:{student.key}"]`. When absent, it stores fetched data without emitting student change events. This marker applies to the whole student, not individual data categories.

`sync_messages()` uses a separate account-level marker, `sync_state["last_sync:messages"]`, for the unified inbox and suppresses new-message notifications on its first sync.

There are no per-category baseline markers. Adding a category for an already synchronized student does not automatically suppress historical notifications. New categories must explicitly provide baseline behavior for existing installations; do not assume the student marker is sufficient.

### Current change semantics

| Category | Comparison identity | Events and limitations |
| --- | --- | --- |
| Grades | `column_id` within the student | `new` for an unknown column; `updated` only when `value` changes. Metadata changes do not emit events. |
| Attendance | `(date, lesson_number)` within the student | `new` only for an unknown record with `category != 1`. Changes to an existing record's category do not emit events. |
| Exams and homework | Upstream numeric `id`, checked against the student's stored IDs | New-item events only. Missing items are soft-deleted after baseline; updates and soft deletes do not emit `Change` events. |
| Messages | Numeric `Message.id` in the unified inbox | New messages are returned separately in `FullSyncResult.new_messages`, not as `Change` objects. `api_global_key` has a DB uniqueness constraint and is used to fetch message detail. |
| Schedule | `(date, time_from, subject)` within the student | New/updated substitutions when the fetched lesson is substituted; new extra lessons without substitution emit additions. Stored lessons missing within the diff window emit cancellations and are deleted from the DB. Ordinary lesson changes and complete removal of substitution fields do not emit substitution events. |

Prefer stable upstream identifiers as keys whenever available. Keep comparison scope explicit and preserve student/account boundaries.

When an upstream item can disappear because it was cancelled or deleted, consider soft-delete semantics rather than destructive deletion.

Change detection must remain deterministic and testable.

---

## 6. Notification and integration model

Data acquisition and notification delivery are separate concerns.

A new eduVULCAN data source should normally follow this flow:

```text
fetch
 -> model
 -> database
 -> diff/change event
 -> one or more output adapters
```

Do not embed email, MQTT, calendar, HTTP, or Home Automation behavior directly inside scraping/API parsing code.

The system may distribute information through multiple channels.

Current output status:

| Channel | Status | Scope |
| --- | --- | --- |
| Terminal / logs | Implemented | Synchronization results and diagnostics. |
| MQTT | Implemented, optional | Detected student changes and new messages; persistent retry outbox. |
| HTTP API | Implemented | Reads local SQLite state in the separate `vulcan-api` service; liveness at `/api/alive`, freshness at `/api/health` (`?soft=1` forces HTTP 200). |
| iCalendar feed | Implemented | Lesson schedule at `/calendar/{student}.ics`; currently excludes exams and homework. |
| macOS Calendar | Implemented, optional, macOS only | Exams/homework via AppleScript, enabled by `CALENDAR_MAP`. |
| Email | Planned | No email adapter or transport settings in the current application. |
| ntfy | Deployment-only | Used by `deploy/vulcan-deploy.sh`; no adapter for synchronized school changes. |

### Terminal / logs

Useful for development, diagnostics, cron/container logs and manual synchronization.

### Email

Email is a planned notification channel, particularly for content that is poorly represented by the standard eduVULCAN notifications. Do not claim email delivery support until an adapter, configuration and tests exist.

Message bodies and other sensitive content must be handled deliberately.

Avoid duplicate emails when repeated synchronization sees the same upstream state.

### MQTT

MQTT is implemented for integration with home automation when `MQTT_ENABLED=true`. `publish_changes()` first enqueues events into SQLite and then drains the persistent outbox. Enqueued events that fail publication remain available for retry on a later run.

Current delivery limitations:

- Entity persistence in `sync.py` and outbox enqueue in `mqtt.py` use separate commits. A failure between these stages can lose notifications for changes already persisted.
- Event publication does not explicitly set QoS 1. Retained heartbeat and LWT use QoS 1, but this does not establish acknowledged delivery of change events.
- End-to-end at-least-once delivery is not guaranteed. Duplicate delivery remains possible, so consumers must tolerate it.

Preserve retries for enqueued events. Atomic entity/outbox persistence and acknowledged event publication are improvement targets, not existing guarantees.

Keep topic naming stable unless there is a clear migration reason.

### HTTP API

The HTTP API exposes locally persisted state to other systems.

It should read primarily from SQLite rather than trigger unnecessary live eduVULCAN requests.

Current deployment expects the service on port `8585`.

### iCalendar / calendar

`ics.py` generates a lesson schedule feed with substitution information, refresh hints and a warning event when that student's schedule is stale. It does not generate exam or homework feeds. Event stamps use row `last_seen`, which advances on each successful upsert; it is not an actual modification timestamp.

`calendar.py` separately synchronizes exams and homework to macOS Calendar using `osascript`. It stores macOS event UIDs in SQLite for subsequent updates and deletion. This integration requires macOS and should remain disabled in Linux Docker deployments (`CALENDAR_MAP` empty).

Prefer stable event identifiers so that changed items update existing calendar events instead of generating duplicates.

### Future integrations

New integrations should be implemented as output adapters over the common change/state model rather than by duplicating synchronization logic.

---

## 7. Authentication: current local implementation

Authentication is an area where this repository intentionally differs from the original upstream implementation.

Do not revert these changes merely to make the code resemble upstream.

The current design uses a persistent Chromium profile:

```text
/app/data/chromium-profile
```

The authentication flow should preserve the following principles.

### Persistent browser state

Use Playwright persistent Chromium context where the current implementation does so, based on:

```text
/app/data/chromium-profile
```

The persistent browser profile is valuable state and should survive container recreation through the persistent `/app/data` volume.

Do not routinely delete or recreate it.

### `session.json`

`session.json` remains useful as explicit application session state / cookie persistence.

The current authentication implementation can bootstrap/import cookies from the stored session into the persistent browser context and save refreshed state after successful authentication.

Do not unnecessarily overwrite or invalidate a working session.

### Session reuse first

Normal `sync` uses `_ensure_session()` in `__main__.py`:

1. Load `SESSION_FILE` and validate it through an HTTP request to `/api/Context`, without starting Chromium.
2. If the session is missing or expired and credentials are available, call `auto_login()`.
3. Inside `auto_login()`, open the persistent Chromium profile, import stored session cookies, and try to restore portal/student access without entering credentials.
4. If browser-session reuse fails, perform credential login and save refreshed session state.
5. Without credentials, normal sync exits with instructions to run `vulcan-notify auth`; it does not automatically launch interactive authentication or attempt browser-profile recovery.

Credentials come from `VULCAN_LOGIN`/`VULCAN_PASSWORD` settings, with a macOS Keychain fallback where available. The explicit `auth` command also tries browser-session reuse before asking the user to log in.

Do not force a fresh login on every sync.

### Interactive authentication

Interactive authentication must remain possible inside the Docker-based deployment.

The `vulcan-auth` Compose service exposes Xvfb + Openbox + x11vnc + noVNC/websockify through the explicit `auth` profile (see section 8).

This exists because eduVULCAN login may require browser interaction, cookie-consent handling, anti-bot processing or other behavior that is unreliable in a purely headless first-login flow.

Do not remove this capability without replacing it with an equally reliable authentication path.

### Unattended normal operation

After a valid authenticated state exists, normal synchronization uses HTTP without starting a browser. Browser recovery defaults to headed Chromium (`VULCAN_BROWSER_HEADLESS=false`) under Xvfb. Unattended operation does not imply headless Chromium. `VULCAN_BROWSER_HEADLESS=true` opts into headless recovery; explicit interactive `auth` always forces headed mode.

The expected long-term operating mode is:

```text
interactive authentication only when needed
                 |
                 v
persistent authenticated browser/session state
                 |
                 v
unattended periodic synchronization via HTTP
```

### Portal/profile selection

The authentication implementation may navigate through eduVULCAN's access/profile page (for example `/dostep-do-dziennika/`) before entering the student portal.

Do not assume there is exactly one student/profile.

Automatic authentication opens the first available journal profile, following upstream behavior. Picker overlays are dismissed before selection, and both the current `/dziennik?` links and previous profile markup are supported. `VULCAN_STUDENT` selection rules have been removed; that variable no longer selects a profile.

The entry profile is not a synchronization filter. `sync_all()` synchronizes all students returned by `client.get_students()` after authentication.

### Authentication failures

`test_session()` has a 30-second total and 10-second connection timeout. A failed validation returns false without replacing session.json. Mid-sync `SessionExpiredError` propagates to the CLI for one credential-backed recovery attempt. Partial results carry already persisted changes to the CLI for output delivery before retry, so a fresh diff does not silently discard them. A degraded sync exits nonzero after publishing successful changes; the loop counts it as a failure. HTML server failures are retried as fetch failures, rather than interpreted as expired sessions.

Automatic recovery failures print instructions for manual authentication without launching a GUI. Interactive recovery remains explicit: stop `vulcan-sync`, run `docker compose --profile auth up vulcan-auth`, then restart `vulcan-sync`. The API can continue serving stored state during this process. Do not automatically launch interactive authentication from normal sync.

### Authentication logging

Log authentication state at a high level only.

Safe examples:

- session reused,
- session expired,
- login required,
- profile found,
- portal reached,
- cookies refreshed.

Never log:

- passwords,
- complete cookies,
- session tokens,
- local-storage secrets,
- raw `session.json`,
- authentication headers.

---

## 8. Docker deployment: current local implementation

Docker is the primary deployment model.

Keep the application fully containerized unless there is a strong technical reason not to.

The current Docker image has been extended beyond upstream to support the interactive authentication environment.

The current Dockerfile includes:

- Python 3.12 base,
- `uv`,
- project dependencies,
- Playwright Chromium,
- Chromium runtime libraries,
- Xvfb,
- `xauth`,
- Openbox,
- x11vnc,
- noVNC / websockify,
- persistent `/app/data`,
- persistent Chromium profile directory under `/app/data/chromium-profile`,
- the project entrypoint.

The startup paths are:

- Compose `vulcan-api`: directly runs `uv run python -m vulcan_notify.api`, publishing port 8585. Its healthcheck uses `/api/alive`; data freshness is a separate concern.
- Compose `vulcan-sync`: uses `entrypoint-xvfb.sh` to start Xvfb and wait for its socket, then executes the supplied `./sync-loop.sh` command.
- `sync-loop.sh`: runs a sequential sync/sleep loop with quiet hours and MQTT heartbeat/outbox draining during long quiet pauses. It does not launch the API.
- The image alone: has no `ENTRYPOINT`; `CMD ["./entrypoint-xvfb.sh", "./sync-loop.sh"]` supplies Xvfb for headed browser recovery. Override the command to run the API directly.
- Compose `vulcan-auth`: an explicit service in profile `auth`, with its own GUI startup script and `uv run vulcan-notify auth` command. Compose shell variables in that script must use `$$` to defer expansion until container startup.

All three services share `./data:/app/data`. Both browser services use `/app/data/chromium-profile` and `/app/data/chromium-profile.lock`; do not change one without the other. `entrypoint.sh` has been removed. The wrapper must forward its command instead of recreating the previous combined API/sync process.

Do not move runtime dependencies to the host merely to simplify the Dockerfile.

### Persistent state

Persistent application state belongs under `/app/data`.

Examples include:

- SQLite database,
- `session.json`,
- Chromium persistent profile,
- other explicitly persistent runtime state.

A container rebuild/recreation must not destroy these items when the volume is configured correctly.

### Xvfb

The current Compose service uses `DISPLAY=:99`. Its wrapper starts:

```text
Xvfb :99 -screen 0 1440x900x24 -ac +extension RANDR
```

The `vulcan-sync` service makes Xvfb available for headed Chromium recovery; it does not start Openbox or noVNC. The separate `vulcan-auth` service starts those graphical components on its own display `:99` and shares the same `./data:/app/data` volume.

Start interactive authentication explicitly:

```bash
docker compose --profile auth up vulcan-auth
```

noVNC is published at `127.0.0.1:6080`; open `http://127.0.0.1:6080/vnc.html` locally or through an SSH tunnel. Both browser services reuse `/app/data/session.json` and `/app/data/chromium-profile`.

When changing Docker startup behavior, preserve both:

- unattended operation,
- recoverable interactive authentication.

### Docker changes

After changing Docker-related files:

1. validate Dockerfile syntax,
2. validate Compose configuration,
3. check paths and volumes,
4. check executable permissions,
5. verify `/app/data` persistence assumptions,
6. ensure Chromium/Playwright dependencies remain present,
7. ensure the interactive authentication path still has all required graphical components.

---

## 9. eduVULCAN API interaction

Treat eduVULCAN as an undocumented external dependency.

The repository uses a reverse-engineered web API.

Do not assume:

- endpoint stability,
- response-field stability,
- HTML structure stability,
- that all accounts expose the same modules,
- that all students expose the same data.

Prefer direct JSON/API access after authentication rather than browser scraping whenever a stable endpoint has already been identified.

Use Playwright primarily for authentication/session establishment unless browser interaction is genuinely required for a specific feature.

Keep API knowledge centralized in:

- `client.py`,
- relevant models,
- `docs/eduvulcan-api.md`.

When discovering a new endpoint:

1. document the endpoint,
2. capture the request requirements,
3. define the response structure,
4. create/update typed models,
5. add client support,
6. add persistence,
7. add diff logic,
8. add tests,
9. add output integration only after the data layer works.

Do not scatter raw URLs and ad-hoc JSON parsing across unrelated modules.

---

## 10. Adding a new synchronized data category

Use the same architecture for new categories such as remarks/praise or other eduVULCAN modules.

A complete implementation usually requires all of the following:

1. **API discovery/client**
   - endpoint and request parameters,
   - authentication requirements,
   - pagination/date ranges if applicable.

2. **Model**
   - stable typed representation,
   - upstream identifier,
   - timestamps/dates normalized consistently.

3. **Database**
   - table/schema migration,
   - stable primary/unique key,
   - timestamps such as first/last seen if useful.

4. **Diff**
   - new / updated / deleted semantics,
   - first-sync baseline behavior.

5. **Sync orchestration**
   - include the category in the appropriate per-student or account-level run.

6. **Change representation**
   - use the common `Change`/event model where present.

7. **Outputs**

   - terminal,
   - MQTT,
   - HTTP API,
   - calendar only where semantically appropriate.

   Integrate with applicable existing outputs; implementing an email adapter is a separate extension, not a prerequisite for every new data category.

8. **Tests**
   - parsing/client,
   - database,
   - differ,
   - sync,
   - serialization/output payload.

9. **Documentation**
   - update API/architecture/configuration docs.

Do not implement only a scraper that prints raw results and call the feature complete.

---

## 11. Student and account boundaries

One eduVULCAN parent account may expose multiple students.

All student-specific persisted data must retain an explicit student identity.

Never merge data between students based only on subject names, teachers, dates, or other non-unique human-readable fields.

Some data, especially mailboxes/messages, may be account-level or mailbox-level rather than student-level.

Model those scopes explicitly instead of forcing every entity into a student-only abstraction.

---

## 12. Database principles

SQLite is the local source of truth for synchronized state.

Prefer:

- simple normalized tables,
- explicit unique keys,
- deterministic upserts,
- migrations that preserve existing data,
- queries that work without contacting eduVULCAN.

Avoid adding a separate database server.

Do not delete user history or rebuild the database as a normal way of fixing schema/code problems.

When changing schema:

1. inspect existing migration/initialization behavior,
2. preserve backward compatibility where practical,
3. add tests for existing databases,
4. document any required manual migration.

Idempotency is important.

Running the same synchronization twice against unchanged upstream data should not create duplicated rows or duplicate notifications.

---

## 13. Scheduling and concurrency

The service is designed for periodic synchronization.

The Docker `sync-loop.sh` controls scheduling: run sync, wait `POLL_INTERVAL` seconds (default `1800`), then repeat. This is a delay after completion, not a fixed start-to-start interval. `Settings.poll_interval` also defaults to `1800`; the shell reads the process environment independently of `Settings`.

Quiet hours default to `QUIET_HOURS_START=0`, `QUIET_HOURS_END=5` and `QUIET_HOURS_TZ=Europe/Warsaw`. Equal start/end disables the pause; windows crossing midnight are supported. Keep scheduler and `freshness.py` semantics aligned. The container clock remains UTC because stored naive timestamps are interpreted as UTC; only the quiet window uses the household timezone.

Successful fetches stamp `last_success:<student>:<section>`; messages use account-level `last_success::messages`. Freshness excludes scheduled quiet hours and defaults to `STALE_AFTER_SECONDS=3600`. A section failure is recorded without advancing its timestamp. Missing timestamps remain stale until a successful sync; do not backfill them from attempted-sync markers. `sync_sections` stores outcomes and `sync_runs` distinguishes completed, degraded, failed and interrupted runs. `SYNC_HISTORY_KEEP_DAYS=90` prunes run/section history, preserving entity data and baseline markers.

Treat these values as configuration, not hard-coded product behavior.

Avoid overlapping synchronization jobs.

If concurrency is introduced, protect:

- SQLite writes,
- authentication/session state,
- message delivery/outbox draining,
- sync-run bookkeeping.

Prefer a single clear synchronization owner unless concurrency provides a measurable benefit.

---

## 14. Configuration

Configuration comes from three current sources:

1. `config.py`: `pydantic-settings` loads application settings from environment variables and `.env`.
2. `auth.py` and the API entry point: selected variables are read directly from the process environment using `os.getenv()` / `os.environ`.
3. `sync-loop.sh`, `entrypoint-xvfb.sh` and Compose: scheduling, display and deployment settings.

Direct environment readers do not load `.env` themselves. Compose uses `env_file: .env` to populate the container environment; do not assume the same behavior for a standalone CLI invocation.

### Important variables and effective defaults

| Variable | Reader | Default / deployment behavior |
| --- | --- | --- |
| `SESSION_FILE` | `Settings` | `session.json`; Compose sets `/app/data/session.json`. |
| `DB_PATH` | `Settings` | `vulcan_notify.db`; Compose sets `/app/data/vulcan_notify.db`. |
| `VULCAN_LOGIN`, `VULCAN_PASSWORD` | `Settings` | Unset; optional credential login with macOS Keychain fallback. |
| `SYNC_ATTENDANCE_DAYS` | `Settings` | `90`. |
| `SYNC_MESSAGE_BACKFILL_BATCH` | `Settings` | `10`. |
| `MQTT_ENABLED` | `Settings` | `false`. |
| `MQTT_TOPIC_PREFIX`, `MQTT_STATUS_SUFFIX` | `Settings` | `school`, `status`. |
| `CALENDAR_MAP` | `Settings` | Empty map disables macOS Calendar integration. |
| `LLM_API_KEY` | `Settings` | Unset; AI summaries are optional. |
| `POLL_INTERVAL` | `sync-loop.sh`; also declared in `Settings` | `1800` seconds in both readers. |
| `QUIET_HOURS_START`, `QUIET_HOURS_END` | `sync-loop.sh` and `Settings` | `0`, `5`; equal values disable quiet hours. |
| `QUIET_HOURS_TZ` | `sync-loop.sh` and `Settings` | `Europe/Warsaw`; container timestamps remain UTC. |
| `STALE_AFTER_SECONDS` | `Settings` | `3600`; excludes scheduled quiet hours. |
| `SYNC_HISTORY_KEEP_DAYS` | `Settings` | `90`; retention for run and section history. |
| `API_PORT` | API entry point | `8585`; Compose publishes a fixed `8585:8585` mapping. |
| `DISPLAY` | `entrypoint-xvfb.sh` / Compose | `:99`. |
| `VULCAN_BROWSER_HEADLESS` | `auth.py`, direct environment | `false`; interactive `auth` forces headed mode. |
| `VULCAN_BROWSER_PROFILE_DIR` | `auth.py`, direct environment | `/app/data/chromium-profile`. |
| `VULCAN_BROWSER_LOCK_FILE` | `auth.py`, direct environment | `/app/data/chromium-profile.lock`. |
| `VULCAN_BROWSER_SLOW_MO_MS` | `auth.py`, direct environment | `0`. |
| `VULCAN_LOGIN_DELAY_SECONDS` | `auth.py`, direct environment | `2`. |
| `VULCAN_CAPTCHA_DETECT_TIMEOUT_SECONDS` | `auth.py`, direct environment | `5`. |
| `VULCAN_CAPTCHA_COMPLETE_TIMEOUT_SECONDS` | `auth.py`, direct environment | `60`. |

The variables read directly by `auth.py` above are not currently declared as `Settings` fields. When changing configuration, check `.env.example` against `Settings` and verify `.env` parsing with sanitized values; `Settings` ignores extra `.env` keys so auth/startup settings do not prevent CLI startup. Direct auth readers still require exported variables outside Compose. SMTP/email transport settings do not exist yet.

Do not hard-code user-specific values.

Possible configuration areas include:

- database path,
- session path,
- eduVULCAN credentials,
- selected student/profile,
- synchronization windows,
- message filtering,
- MQTT broker and credentials,
- future email transport/settings when an adapter is implemented,
- calendar mappings,
- HTTP/API configuration,
- logging,
- optional AI summarization.

When adding configuration:

1. add application settings to the settings model; for auth/API/startup environment settings, explicitly document the reader and verify compatibility with `.env` loading,
2. add a safe example to `.env.example`,
3. document the effective default and any Compose overrides,
4. avoid surprising behavior when unset,
5. never commit a real secret.

---

## 15. Security and privacy

This project processes private school and family information.

Treat the following as sensitive:

- eduVULCAN credentials,
- cookies and session data,
- student identity data,
- grades,
- attendance,
- behavior notes,
- messages and message bodies,
- email addresses,
- MQTT credentials,
- API keys.

Never expose secrets in logs, test fixtures, commits, screenshots, generated documentation or error messages.

Use sanitized fixtures for tests.

Do not send synchronized school data to external AI/cloud services unless that behavior is explicitly configured and expected.

Optional AI summarization must remain optional.

---

## 16. Development principles

This is a small project.

Prefer simple, explicit code over framework-heavy design.

Do not introduce without clear need:

- microservices,
- Redis,
- external message queues,
- database servers,
- dependency-injection frameworks,
- generic plugin frameworks,
- unnecessary repository layers.

A new abstraction should solve a concrete repeated problem.

Do not perform unrelated refactoring while fixing a focused issue.

Preserve working interfaces unless a change is necessary.

---

## 17. Commands and tooling

The project uses `uv`.

Typical development commands are:

```bash
uv sync --all-extras
uv run vulcan-notify test
uv run vulcan-notify sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src/
```

For a focused test:

```bash
uv run pytest tests/test_sync.py -x -q
```

Inspect the current `pyproject.toml` before assuming all commands/options above are still configured exactly as upstream.

For Docker changes, also use:

```bash
docker compose config
```

and build/run validation where practical.

---

## 18. Testing strategy

Tests should focus on logic that can be reproduced locally.

Prefer tests for:

- API response parsing,
- typed model conversion,
- cookie/session helper behavior,
- database upserts,
- migrations,
- first-sync baseline,
- change detection,
- duplicate suppression,
- soft deletes,
- output payload generation,
- MQTT outbox behavior,
- HTTP responses,
- iCalendar generation.

Mock eduVULCAN HTTP responses rather than making the normal automated suite depend on the live service.

Existing tests use patterns such as:

- `pytest-asyncio`,
- temporary SQLite DB fixtures,
- `AsyncMock` for async client methods,
- `MagicMock` for aiohttp behavior.

Live eduVULCAN tests are integration tests and must be treated separately.

Never claim live authentication or API behavior was validated unless it actually was.

---

## 19. Authentication-specific testing

Changes in `auth.py` deserve extra caution.

Where practical, test separately:

1. session-file parsing,
2. cookie conversion/import,
3. profile-path handling,
4. selection logic,
5. session reuse decisions,
6. fallback decisions.

A test that mocks Playwright can validate decision logic but does not prove eduVULCAN login itself works.

For authentication changes, explicitly state which of these were verified:

- unit logic,
- container startup,
- browser startup,
- persistent profile reuse,
- manual login through noVNC,
- live session reuse,
- live headless sync.

---

## 20. Logging

Logs should help diagnose unattended operation.

Useful events include:

- sync start/end,
- student/profile selected,
- session reused/expired,
- data categories fetched,
- item counts,
- detected change counts,
- notification delivery status,
- MQTT outbox pending count,
- recoverable retry/failure decisions.

Avoid per-item debug spam at normal log levels.

Never log raw secrets or complete sensitive payloads by default.

---

## 21. Failure handling

A failure in one optional output channel should not unnecessarily destroy successfully fetched data.

Prefer this separation:

```text
fetch/diff/persist succeeds
          |
          +--> email may fail
          +--> MQTT may fail
          +--> calendar may fail
```

Use durable retry mechanisms where already designed, especially MQTT outbox behavior.

Do not hide failures with broad `except Exception: pass`.

Log enough context to identify:

- which stage failed,
- which student/account scope was affected,
- whether persisted data is still valid,
- whether retry is safe.

---

## 22. Git discipline

Before editing:

```bash
git status
git diff
```

Do not overwrite unrelated uncommitted work.

Do not run destructive Git operations unless explicitly requested.

Do not commit or push unless explicitly requested.

After implementation:

1. inspect the complete diff,
2. ensure unrelated files were not changed,
3. run applicable tests/checks,
4. summarize the result.

---

## 23. Relationship with `CLAUDE.md`

`CLAUDE.md` contains useful architecture and repository knowledge from earlier development with Claude Code.

Do not delete it merely because `AGENTS.md` exists.

When starting a substantial task:

1. read `AGENTS.md`,
2. read `CLAUDE.md`,
3. inspect the current code,
4. use documentation such as `docs/architecture.md` and `docs/eduvulcan-api.md`,
5. resolve contradictions based on the actual current implementation.

Long-term, shared technical facts should preferably remain consistent across both agent instruction files.

Do not blindly copy obsolete upstream-specific assumptions into the local project.

In particular, the local authentication and Docker setup have already diverged from upstream.

---

## 24. Documentation discipline

When behavior changes, update the relevant documentation in the same task where practical.

Important documentation includes:

- `README.md`,
- `AGENTS.md`,
- `CLAUDE.md`,
- `docs/architecture.md`,
- `docs/eduvulcan-api.md`,
- `docs/deployment.md`,
- `.env.example`.

Do not let documentation state that a feature is supported when it is still only planned.

Distinguish clearly between:

- implemented,
- experimentally implemented,
- planned,
- unsupported.

---

## 25. Recommended agent workflow

For non-trivial tasks, follow this sequence.

### Investigation

1. Read `AGENTS.md` and `CLAUDE.md`.
2. Inspect the relevant modules and tests.
3. Trace the existing execution path.
4. Inspect database/config implications.
5. Check whether documentation already describes the feature.

### Plan

Before editing, provide a short plan containing:

- root cause or requested capability,
- files that need modification,
- persistence/schema impact,
- compatibility risks,
- validation approach.

### Implementation

Make the smallest coherent change that satisfies the task.

Avoid unrelated cleanup.

### Validation

Run the relevant subset of:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src/
docker compose config
```

Add focused tests for new behavior.

### Review

Review the diff as if it had been written by someone else.

Look specifically for:

- duplicate notifications,
- incorrect first-sync behavior,
- broken idempotency,
- cross-student data leakage,
- auth/session regression,
- lost persistent data,
- fragile eduVULCAN selectors,
- leaked secrets,
- output-channel coupling,
- schema compatibility problems.

### Report

At the end, summarize:

- files changed,
- behavior changed,
- tests/checks run,
- live behavior verified,
- anything not verified,
- any migration/config action required.

---

## 26. Current local priorities

When choosing between an upstream-preserving implementation and one that better fits the current local deployment, preserve the local requirements.

Important local priorities are:

1. fully Dockerized operation,
2. reliable unattended synchronization,
3. recoverable interactive eduVULCAN login via Xvfb/noVNC,
4. persistent Chromium/session state,
5. multi-student correctness,
6. broad synchronization coverage rather than message-only behavior,
7. SQLite-based state and change detection,
8. flexible fan-out to email, MQTT, calendars, HTTP and future integrations,
9. simple maintenance and observability,
10. no unnecessary host dependencies.

The desired system is a robust local synchronization/integration service for the complete useful eduVULCAN data surface, not a one-purpose mail-forwarding script.
