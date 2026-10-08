# Deploy with the Docker Hub image (default)

The default deployment uses `macjoker/vulcan-notify` on a Linux Docker host.
Deploy manually below or use the [example Ansible playbook](deployment-ansible.md).
The host needs neither a source build nor application Python/Playwright dependencies.

**No image has been published as part of this change.** A maintainer must first
follow the [build and publishing instructions](publishing.md). `0.20.0` below is
an example matching the current project version; select a tag actually published
on Docker Hub. AMD64 and ARM64 are build targets; full builds and live operation
on both architectures still need verification.

The [existing Proxmox homelab deployment](deployment-proxmox.md) remains an
alternative. The root `docker-compose.yml` remains the source-build configuration
for local development and that existing installation.

## Requirements

- Linux AMD64/ARM64 host with Docker Engine and Docker Compose plugin 2.18+.
  Follow the [official Docker installation guide](https://docs.docker.com/engine/install/).
- Outbound HTTPS to Docker Hub and eduVULCAN. Optional SMTP, MQTT and AI services
  must also be reachable from the containers.
- An eduVULCAN account with web journal access and SSH access to the Docker host
  for remote browser authentication.

## Services and persistent state

| Service | Purpose | Default exposure |
| --- | --- | --- |
| `vulcan-api` | HTTP API and lesson-schedule iCalendar feeds from SQLite | `127.0.0.1:8585` |
| `vulcan-sync` | Sequential polling with Xvfb for headed browser recovery | No published ports |
| `vulcan-auth` | Explicit interactive login using Xvfb/Openbox/noVNC | `127.0.0.1:6080`, `auth` profile only |

All three services share `./data:/app/data`: SQLite, `session.json`, the Chromium
profile and its lock survive container replacement. Configuration lives in `.env`.
The image includes Chromium and the graphical authentication tools. macOS
AppleScript Calendar integration is disabled; HTTP iCalendar remains available.
One sync/delivery owner must use this data directory at a time.

## 1. Prepare configuration

On the Docker host, become root (or prefix host commands below with `sudo`):

```bash
sudo -s
install -d -m 0700 /opt/vulcan-notify /opt/vulcan-notify/data
cd /opt/vulcan-notify
```

Copy these reviewed repository files to the host:

- [`deploy/docker/compose.yml`](../deploy/docker/compose.yml) → `/opt/vulcan-notify/compose.yml`
- [`.env.example`](../.env.example) → `/opt/vulcan-notify/.env`

For example, from a local checkout on your workstation:

```bash
scp deploy/docker/compose.yml .env.example deploy@docker-host:/tmp/
```

Then in the root shell on the host:

```bash
install -m 0644 /tmp/compose.yml /opt/vulcan-notify/compose.yml
install -m 0600 /tmp/.env.example /opt/vulcan-notify/.env
```

Edit `.env`. Minimal configuration:

```dotenv
VULCAN_IMAGE=macjoker/vulcan-notify:0.20.0
VULCAN_API_BIND=127.0.0.1
TZ=Europe/Warsaw
POLL_INTERVAL=1800
QUIET_HOURS_START=0
QUIET_HOURS_END=5
LOG_LEVEL=INFO

# Optional: enable automatic session recovery with credentials.
# VULCAN_LOGIN=parent@example.org
# VULCAN_PASSWORD='replace_me'
```

Without credentials, initialize the session through manual authentication in
step 3. With credentials, the worker validates saved cookies over HTTP, then tries
persistent-browser recovery before credential login when needed. Normal sync
does not launch interactive login automatically.

Use single quotes around literal secrets containing `$`, `#` or spaces; consult
[Compose's environment-file syntax](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/#env-file-syntax)
for embedded quotes and multiline values. The sample includes all application
options. `DB_PATH`, `SESSION_FILE`, `API_PORT`, browser profile/lock paths and
`CALENDAR_MAP` are fixed by this deployment to match its volume and service commands.
`TZ` controls logs, quiet hours and displayed timestamps; storage remains UTC.

For Home Assistant on another machine, set `VULCAN_API_BIND` to the Docker host's
trusted LAN IPv4 address. The API has no authentication; restrict access to trusted
clients. noVNC stays bound to loopback.

Optional email and MQTT example (merge into `.env`):

```dotenv
EMAIL_ENABLED=true
SMTP_HOST=smtp.example.org
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USERNAME=school@example.org
SMTP_PASSWORD='replace_me'
EMAIL_FROM='School notifications <school@example.org>'
EMAIL_TO=["parent@example.org"]

MQTT_ENABLED=true
MQTT_BROKER=mqtt.example.org
MQTT_PORT=1883
# MQTT_USERNAME=vulcan-notify
# MQTT_PASSWORD='replace_me'
```

`localhost` inside a container refers to that container. Use a reachable broker
or SMTP hostname. Email, MQTT and AI are optional; see [email configuration](email.md).

## 2. Pull and start

In `/opt/vulcan-notify` on the host:

```bash
# Validate without printing credentials from the resolved configuration.
docker compose config --quiet
docker compose --profile auth pull
docker compose up -d vulcan-api vulcan-sync
docker compose ps
docker compose logs --tail 50 vulcan-sync
```

The auth profile is pulled but not started. No host build is performed. First
successful imports establish baselines silently, rather than notifying about
historical school records.

## 3. Authenticate when needed

Stop the worker before opening the shared Chromium profile:

```bash
cd /opt/vulcan-notify
docker compose stop vulcan-sync
docker compose --profile auth up vulcan-auth
```

From the workstation, keep an SSH tunnel running:

```bash
ssh -N -L 6080:127.0.0.1:6080 deploy@docker-host
```

Open <http://127.0.0.1:6080/vnc.html> and complete login. The foreground auth
command saves the session and exits after reaching the student application.
If interrupted or authentication fails, check its logs and retry before resuming
the worker. The API can continue serving previously stored state.

```bash
docker compose --profile auth stop vulcan-auth
docker compose up -d vulcan-sync
docker compose logs --tail 50 vulcan-sync
```

Preserve `data/session.json` and `data/chromium-profile`; deleting them is not a
normal recovery step. Automatic browser recovery is headed under Xvfb by default;
`VULCAN_BROWSER_HEADLESS=true` opts into headless recovery. Interactive auth is
always headed. Authentication opens the first journal profile, then synchronization
processes every student discovered for the account.

Transient connectivity/server/malformed session-validation failures retry once,
then preserve the session for the next poll. Context HTTP 409 requests session
recovery; mid-sync recovery is limited to one attempt. Exhausted authentication
can queue a deduplicated recovery email when SMTP is configured.

## 4. Verify and operate

```bash
curl --fail http://127.0.0.1:8585/api/alive
# 503 means stale/missing data, including before the first successful sync.
curl -s http://127.0.0.1:8585/api/health
curl --fail http://127.0.0.1:8585/api/students
docker compose logs --tail 50 vulcan-api
docker compose logs --tail 50 vulcan-sync
```

Use your configured LAN address instead of loopback if you changed the API bind.
The container healthcheck uses `/api/alive`. Monitor `/api/health` separately for
freshness (`?soft=1` returns the same body with HTTP 200). Freshness excludes quiet
hours. `POLL_INTERVAL` is the delay after completion, defaulting to 1800 seconds;
quiet hours default to 00:00–05:00 in `TZ`.

After `.env` changes, use `docker compose up -d vulcan-api vulcan-sync` to recreate
services whose configuration changed. `docker compose restart` retains the old
environment. If Ansible manages the installation, edit controller files and rerun
the playbook instead.

For a one-off sync, keep the regular worker stopped:

```bash
docker compose stop vulcan-sync
docker compose run --rm vulcan-sync uv run vulcan-notify sync
docker compose up -d vulcan-sync
```

The [architecture reference](architecture.md) documents HTTP endpoints, MQTT
payloads and iCalendar feeds. Feeds cover lesson schedules, not exams/homework.

## 5. Backup, update and rollback

For a consistent offline backup, stop all services that can write shared state:

```bash
cd /opt/vulcan-notify
docker compose --profile auth stop
install -d -m 0700 /var/backups/vulcan-notify
tar -czf "/var/backups/vulcan-notify/state-$(date +%Y%m%d-%H%M%S).tar.gz" \
  data .env compose.yml
chmod 0600 /var/backups/vulcan-notify/*.tar.gz
docker compose up -d vulcan-api vulcan-sync
```

Backups contain private data and credentials; keep them protected. The playbook
does not install backup or auto-update timers. Schedule backups separately if desired.

To upgrade, review the release, back up state, and change `VULCAN_IMAGE` to a new
published version (or `macjoker/vulcan-notify@sha256:...` digest):

```bash
docker compose --profile auth pull
docker compose up -d vulcan-api vulcan-sync
docker compose ps
```

With Ansible, change `vulcan_image` in your vars file and rerun the playbook.
`latest` can be used deliberately; a version/digest makes upgrades explicit.
Treat published version tags as immutable and retain the previous tag/digest.
For rollback, select the previous image and recreate services. Older code may
not understand a newer schema; restore the matching pre-upgrade backup with all
services stopped when required. Image replacement preserves bind-mounted state.

## Migrating an existing Proxmox installation

Stop the old worker/auth service and disable `vulcan-deploy.timer` so it cannot
rebuild/restart that installation. Back up the old `.env` and full `data/` directory.
On a different host, copy state while services are stopped; on the same host,
retain the directory. Stop the old API before starting the new stack on the same port.

The old Compose project name may have been derived from its checkout directory.
Shut down the old stack with its original Compose file before replacing files,
using `docker compose down` without `--volumes`. The new project is explicitly
named `vulcan-notify`; keep exactly one sync owner. Apply the new definition or
playbook, then verify the API and sync logs. Review existing backup timers' paths;
do not copy homelab-specific timers blindly to a new host.
