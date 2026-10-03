# vulcan-notify

> [!TIP]
> ✨ ***Push notifications, calendars, and dashboards from a school e-journal that refuses to give them to you.***

CLI tool that syncs data from the eduVulcan school e-journal to a local SQLite database, detects changes between runs, and fans them out to a terminal, SMTP email, macOS Calendar, an MQTT broker, and an HTTP/iCalendar API.

Solves the problem of eduVulcan paywalling push notifications behind a subscription, while the web version (which is legally required to remain free) has no notification support. Also exposes a local HTTP + MQTT surface so Home Assistant (or anything else) can react to school events in real time.

> [!NOTE]
> [📦 Installation](#installation) · [⚡ Commands](#commands) · [⚙️ How it works](#how-it-works) · [🏠 Home Assistant integration](#home-assistant-integration) · [🔐 Auto-login](#auto-login) · [📅 Calendar integration](#calendar-integration) · [🔧 Configuration](#configuration) · [📚 Documentation](#documentation)

## What it tracks

- **Grades** - new and changed, with subject, teacher, category, and weight
- **Attendance** - absences and late arrivals
- **Exams** - upcoming tests and quizzes, with description and teacher
- **Homework** - upcoming assignments, with full description
- **Messages** - unread count and body, with optional sender whitelist filtering
- **Praise and behavior notes** - per-student persistence, new-item events and separate emails with a link to the Pochwały i uwagi view
- **Lesson schedule** - substitutions, cancellations, and extra lessons

Supports multiple students under one parent account.

## What it exposes

- **Terminal output** - colored when interactive, plain when piped
- **macOS Calendar** - exams and homework as all-day events with reminders (iCloud syncs to iOS)
- **MQTT events** - every detected change published to Mosquitto (with a persistent outbox for retries)
- **Email notifications** - change digests with optional AI summary, plus a separate email for each new message; persistent SMTP retries ([setup](docs/email.md))
- **HTTP API** - grade aggregates, homework, messages, and schedule over aiohttp on port 8585
- **iCalendar feed** - per-student `.ics` feed for subscribing from iOS/macOS Calendar, Google Calendar, or Home Assistant
- **AI summaries** - optional digest of recent changes or messages via any OpenAI-compatible API

## 📦 Installation <a name="installation"></a>

**Requirements:** Python 3.12+, [uv](https://docs.astral.sh/uv/) package manager

```bash
git clone https://github.com/yourname/vulcan-notify.git
cd vulcan-notify
uv sync

# Install Playwright browsers (needed for auth)
uv run playwright install chromium

# Configure (optional)
cp .env.example .env
# Edit .env to set MESSAGE_SENDER_WHITELIST, MQTT_*, CALENDAR_MAP, etc.

# Authenticate with eduVulcan (opens browser)
uv run vulcan-notify auth

# Test session validity
uv run vulcan-notify test

# Sync data and see changes
uv run vulcan-notify sync
```

## ⚡ Commands <a name="commands"></a>

| Command | Description |
|---------|-------------|
| `vulcan-notify auth` | Interactive browser login, saves session cookies |
| `vulcan-notify test` | Test if saved session is still valid |
| `vulcan-notify api-gather` | On-demand upstream OpenAPI gathering and sanitized fixture update |
| `vulcan-notify api-check` | On-demand live check against the saved upstream contract |
| `vulcan-notify sync` | Fetch latest data and show changes (default) |
| `vulcan-notify email-retry` | Retry queued SMTP digests without contacting eduVULCAN |
| `vulcan-notify calendar` | Force re-sync all exams/homework to macOS Calendar |
| `vulcan-notify tui` | Interactive Textual browser for synced content (requires `uv sync --extra tui`) |
| `vulcan-notify summarize [--type sync\|messages] [--days N]` | AI summary of recent changes or messages (requires `LLM_API_KEY`) |

In the TUI, press `6` for **Remarks** (praise and behavior notes). The list shows
date, student, category, author, optional points and a content preview. Press Enter
for full content and the student's Vulcan URL; use `s` to cycle the student filter
and `o` / `O` to sort. Soft-deleted notes are hidden. All views read local SQLite.

## ⚙️ How it works <a name="how-it-works"></a>

For upstream API verification and discovery, see the
[OpenAPI contract guide](docs/eduvulcan/README.md). These jobs run only when invoked.

End-to-end flow from the eduVulcan API down to a push notification on your phone and a tile on your Home Assistant dashboard:

```mermaid
%%{init: {'theme':'base','flowchart':{'curve':'basis','nodeSpacing':50,'rankSpacing':60}}}%%
flowchart TB
    accTitle: vulcan-notify end-to-end flow
    accDescr: A scheduled sync pulls grades, attendance, exams, homework, messages, and schedule from eduVulcan. Changes are diffed against SQLite and fanned out to the terminal, SMTP email, macOS Calendar, MQTT, and an HTTP/iCalendar API. Home Assistant consumes MQTT events and the HTTP API to drive a school dashboard and push notifications. Calendar, email and HA push converge on the parent's phone.

    Vulcan([uczen.eduvulcan.pl])

    subgraph Daemon[vulcan-notify daemon]
        direction TB
        Cron[/cron · timer/] --> Sync(sync)
        Auth[[auth · Playwright]] -.->|cookies| Sync
        Sync --> Differ{New or<br/>changed?}
        Differ --> DB[(SQLite)]
        Differ --> Fanout{{fan-out}}
        Fanout --> DB
        Fanout --> Term[/terminal/]
        Fanout --> CalSync[calendar sync]
        Fanout --> Outbox[(MQTT outbox)]
        API[api · aiohttp :8585] --> DB
        EmailOutbox[(Email outbox)]
    end

    subgraph HA[Home Assistant]
        direction TB
        Mosq[(Mosquitto)] --> Sensors[REST + MQTT sensors]
        Sensors --> Dash[School dashboard]
        Sensors --> Auto{{Automations}}
        Auto --> Push[/Push notification/]
    end

    Phone([Parent phone])

    Sync <-->|HTTPS · JSON| Vulcan
    CalSync -->|AppleScript| iCloud[(iCloud Calendar)]
    Outbox -->|school/#| Mosq
    API -->|REST polling| Sensors
    API -->|/calendar/<name>.ics| iCloud
    iCloud --> Phone
    Push --> Phone
    Fanout --> EmailOutbox
    EmailOutbox -->|SMTP digest · optional AI| Inbox[Parent email inbox]
    Inbox --> Phone

    classDef storage fill:#ecfdf5,stroke:#16a34a,color:#064e3b;
    classDef external fill:#fef3c7,stroke:#d97706,color:#78350f;
    classDef actor fill:#eef2ff,stroke:#4f46e5,color:#312e81;
    class DB,Outbox,EmailOutbox,Mosq,iCloud storage;
    class Vulcan external;
    class Phone actor;

    linkStyle default stroke:#64748b,stroke-width:1.5px
    linkStyle 14 stroke:#2563eb,stroke-width:2px
    linkStyle 3,4,5 stroke:#16a34a,stroke-width:2px
    linkStyle 15,16,17,18 stroke:#ea580c,stroke-width:2px
    linkStyle 12,13 stroke:#9333ea,stroke-width:1.5px,stroke-dasharray: 4 3
```

1. **Auth** - Playwright opens a browser for you to log into eduvulcan.pl. After login, session cookies are saved locally. Subsequent syncs validate cookies over HTTP. With credentials available, expired sessions first try persistent-browser recovery, then credential login.
2. **Fetch** - The tool calls the eduVulcan web API directly (using saved cookies) to pull grades (all periods), attendance (last 90 days), exams, homework with full body, messages, and the lesson schedule including substitutions.
3. **Diff** - Each item is compared against the local SQLite database. New or changed items are reported; exams and homework that disappear from the API are soft-deleted.
4. **Persist** - All upserts are idempotent (`INSERT OR REPLACE`). Each run is recorded in a `sync_runs` table.
5. **Publish** - Changes are printed to the terminal, summarized for SMTP email (optionally using AI), written to macOS Calendar, and published to MQTT when those channels are configured. Email and MQTT have separate persistent retry outboxes.
6. **Serve** (separate command, long-running) - `vulcan-notify api` (via Docker or systemd service) exposes the HTTP + iCalendar endpoints backed by the same SQLite file.

On first sync, all existing data is stored without reporting changes (baseline). Only subsequent syncs show what's new.

For the full module breakdown, database schema, MQTT topic map, and HTTP endpoint reference, see [`docs/architecture.md`](docs/architecture.md).

## 🏠 Home Assistant integration <a name="home-assistant-integration"></a>

Production setup runs vulcan-notify as a Docker container on a Proxmox LXC, publishing MQTT events to the Mosquitto broker inside Home Assistant OS. Two ways to wire it up:

- **Event-driven (MQTT)** - subscribe to `school/#` and build sensors or automations. Topic scheme: `<prefix>/<student-slug>/<segment>/<change_type>`, e.g. `school/alice/grades/new`, `school/alice/exams/updated`, `school/alice/attendance/alert`, `school/alice/substitutions/new`. Payloads are structured JSON with the full change metadata.
- **Pull-based (HTTP)** - HA's REST sensor polls `/api/grades/monthly`, `/api/messages`, `/api/schedule`, etc. for dashboards and history graphs. The iCalendar feed at `/calendar/<name>.ics` can be subscribed directly from any calendar client.

See [`docs/architecture.md`](docs/architecture.md) for the full endpoint list, MQTT topic map, and payload examples. See [`docs/deployment.md`](docs/deployment.md) for the Proxmox/Docker/systemd setup.

## 🔐 Auto-login <a name="auto-login"></a>

eduVulcan sessions expire after a few hours. To run fully unattended (e.g., on a home server with a cron job), store your credentials so the script re-authenticates automatically when a session expires.

**Option 1: macOS Keychain** (recommended, no plaintext on disk)

```bash
security add-generic-password -s vulcan-notify -a your.email@example.com -w
# Prompts for password interactively
```

**Option 2: environment variables** - add to `.env`:

```
VULCAN_LOGIN=your.email@example.com
VULCAN_PASSWORD=your_password
```

When credentials are available, `vulcan-notify sync` detects expired sessions, imports saved cookies into persistent Chromium and attempts browser-session recovery before credential login. Docker provides Xvfb for headed recovery by default; `VULCAN_BROWSER_HEADLESS=true` opts into headless mode. If automatic recovery fails, stop `vulcan-sync` and run `docker compose --profile auth up vulcan-auth` for manual headed authentication through noVNC, then restart the worker. All services share the persistent `/app/data` volume.

Session files are replaced atomically with permissions 0600. Invalid saved sessions use the same recovery policy, preserving the file until authentication succeeds.

Student API endpoints keep existing name-based response keys when names are unique. `/api/students` lists stable keys; use `?student_key=KEY` for one profile or `?keyed=1` for responses keyed by profile. Each student payload includes `name` and `student_key`. Ambiguous name lookups return HTTP 409. Calendar subscriptions can use `/calendar/<name>.ics?student_key=KEY`; historical profiles are combined by name only when their nonempty mailbox identity matches.

## 📅 Calendar integration <a name="calendar-integration"></a>

Push exams and homework to iCloud Calendar as all-day events with reminder alarms. Events sync to all devices via iCloud.

Add to `.env`:

```
CALENDAR_MAP={"Alice Smith": "School Alice", "Bob Johnson": "School Bob"}
```

The calendar names must match existing calendars in macOS Calendar. Each student maps to their own calendar.

When configured, `vulcan-notify sync` automatically creates and updates calendar events. Use `vulcan-notify calendar` to force a clean re-sync of all events. Events are deduplicated by storing the macOS calendar UID in the database; when exams or homework are removed from the API (soft-deleted), their calendar events are also removed.

Transient update/deletion failures retain UIDs for retry, including forced re-sync. `CALENDAR_TIMEOUT_SECONDS` defaults to 30 seconds per AppleScript operation; timeout or cancellation kills and reaps the subprocess. Missing events are recreated on a later sync after Calendar confirms their absence.

## 🔧 Configuration <a name="configuration"></a>

Set `TZ=Europe/Warsaw` in `.env` (the default) for all Docker services, application logs, Chromium, quiet hours, email dates, MQTT timestamps and API diagnostics. The IANA zone applies daylight-saving rules automatically. `TZ` takes precedence over the legacy `QUIET_HOURS_TZ` alias. SQLite timestamps remain explicitly UTC; old UTC history needs no migration. UTC encodings in upstream requests and iCalendar preserve the same instants. Docker daemon log metadata (`docker logs --timestamps`) is managed by Docker and remains UTC; timestamps inside application log lines use `TZ`.

Python settings load `.env`; Compose exports it to the containers. Direct auth/API/shell readers need exported variables outside Docker. See the commented [`.env.example`](.env.example) for all settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `vulcan_notify.db` | SQLite database path |
| `SESSION_FILE` | `session.json` | Saved session cookies path |
| `VULCAN_LOGIN` | (none) | eduVulcan login email for auto-login |
| `VULCAN_PASSWORD` | (none) | eduVulcan password for auto-login |
| `SYNC_ATTENDANCE_DAYS` | `90` | How many days back to sync attendance |
| `SYNC_MESSAGE_BACKFILL_BATCH` | `10` | Legacy messages to refetch bodies for, per run |
| `POLL_INTERVAL` | `1800` | Seconds between polls when run as a service |
| `QUIET_HOURS_START` | `0` | Hour (0-23) to start quiet window, sync paused |
| `QUIET_HOURS_END` | `5` | Hour (0-23) to end quiet window, sync resumes |
| `TZ` | `Europe/Warsaw` | Shared runtime, log, scheduling and display timezone; `QUIET_HOURS_TZ` is a legacy fallback |
| `MESSAGE_SENDER_WHITELIST` | `[]` | JSON list of sender substrings to filter displayed messages |
| `CALENDAR_MAP` | (empty) | JSON dict mapping student names to macOS calendar names |
| `CALENDAR_REMINDER_HOURS` | `24` | Hours before event for calendar alarm |
| `MQTT_ENABLED` | `false` | Enable MQTT publishing |
| `MQTT_BROKER` | `localhost` | Mosquitto hostname |
| `MQTT_PORT` | `1883` | Mosquitto port |
| `MQTT_USERNAME` | (none) | Optional MQTT auth |
| `MQTT_PASSWORD` | (none) | Optional MQTT auth |
| `MQTT_TOPIC_PREFIX` | `school` | Topic namespace root |
| `EMAIL_ENABLED` | `false` | Enable SMTP digests; [full configuration and retry behavior](docs/email.md) |
| `SMTP_HOST`, `EMAIL_FROM`, `EMAIL_TO` | (empty) | Required email server, sender, and JSON recipient list |
| `SMTP_PORT`, `SMTP_SECURITY` | `587`, `starttls` | SMTP port and TLS mode (`starttls`, `ssl`, `none`) |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | (none) | Optional SMTP credentials |
| `EMAIL_AI_SUMMARY` | `false` | Add an AI summary above the HTML change groups; also requires `LLM_API_KEY` |
| `EMAIL_DIGEST_GROUPS` | `{}` (all enabled) | Individual digest group switches, e.g. `{"attendance":false,"homework":false}`; omitted keys stay enabled |
| `EMAIL_MESSAGE_SUBJECT_PREFIX` | `[Nowa wiadomość]` | Prefix for separate new-message notifications, followed by the original subject |
| `EMAIL_REMARK_SUBJECT_PREFIX` | `[Uwagi]` | Prefix for separate praise/note emails, followed by student name and category; full note content is included |
| `EMAIL_INCLUDE_MESSAGE_BODIES` | `false` | Include original message bodies in individual notifications; messages are excluded from digest AI input |
| `NTFY_TOPIC` | `vulcan-notify` | ntfy.sh topic (if used) |
| `NTFY_SERVER` | `https://ntfy.sh` | ntfy server base URL |
| `LLM_BASE_URL` | `https://api.cerebras.ai/v1` | OpenAI-compatible API base URL for AI summaries |
| `LLM_API_KEY` | (none) | API key for AI summaries (disabled if unset) |
| `LLM_MODEL` | `gpt-oss-120b` | Model name for AI summaries |
| `LOG_LEVEL` | `INFO` | Logging level |

## 📚 Documentation <a name="documentation"></a>

- [`docs/architecture.md`](docs/architecture.md) - internal architecture, pipeline, database schema, MQTT payloads, endpoint reference
- [`docs/email.md`](docs/email.md) - HTML groups/templates, SMTP digests, optional AI summary, configuration and retries
- [`docs/deployment.md`](docs/deployment.md) - Docker + Proxmox LXC + systemd setup
- [`docs/eduvulcan-api.md`](docs/eduvulcan-api.md) - reverse-engineered eduVulcan web API reference
