# vulcan-notify

Self-hosted synchronization and notifications for the **eduVULCAN school
e-journal**. Data is stored locally in SQLite and compared across syncs.
Supports multiple students under one parent account.

- Grades, attendance, exams, homework, messages, praise/notes, lesson schedules
  and completed lesson topics.
- Optional SMTP notifications and MQTT events for home automation.
- HTTP API and lesson-schedule iCalendar feeds.
- Optional AI summaries through an OpenAI-compatible provider.
- Persistent sessions and Chromium profile with a noVNC browser for manual login.

First successful imports establish a silent baseline. Coverage and change
semantics vary by category; see the
[repository documentation](https://github.com/Joker-KP/vulcan-notify#what-it-tracks).
macOS AppleScript Calendar integration is unavailable in this Linux image.

## Deploy

Use the repository examples instead of building from source on the host:

- [Sample Compose configuration](https://github.com/Joker-KP/vulcan-notify/blob/main/deploy/docker/compose.yml)
- [Sample environment configuration](https://github.com/Joker-KP/vulcan-notify/blob/main/.env.example)
- [Full deployment and authentication guide](https://github.com/Joker-KP/vulcan-notify/blob/main/docs/deployment.md)
- [Example Ansible deployment](https://github.com/Joker-KP/vulcan-notify/blob/main/docs/deployment-ansible.md)

Save the Compose example as `compose.yml` and the environment example as `.env`
in the same directory. Edit `.env` and select a published version:

```dotenv
VULCAN_IMAGE=macjoker/vulcan-notify:0.20.0
VULCAN_API_BIND=127.0.0.1
TZ=Europe/Warsaw
# Optional credentials for automatic session recovery:
# VULCAN_LOGIN=parent@example.org
# VULCAN_PASSWORD='replace_me'
```

```bash
mkdir -p data
chmod 0700 data
chmod 0600 .env
docker compose config --quiet
docker compose --profile auth pull
docker compose up -d vulcan-api vulcan-sync
```

The normal stack runs separate API and sync containers. Manual authentication is
explicit: stop `vulcan-sync`, then run `docker compose --profile auth up vulcan-auth`.
Open noVNC at `http://127.0.0.1:6080/vnc.html` locally or through an SSH tunnel;
resume the worker after login. See the full guide for commands.

Keep `data/`: it contains the database, session and browser profile mounted at
`/app/data`. Container upgrades preserve this state. API access defaults to
loopback port 8585 and has no built-in authentication; use a trusted LAN bind
address for Home Assistant. noVNC remains loopback-only. Email/MQTT/AI are disabled
unless configured. Pin a published version or digest for controlled upgrades.

[Source and documentation](https://github.com/Joker-KP/vulcan-notify) ·
[Email configuration](https://github.com/Joker-KP/vulcan-notify/blob/main/docs/email.md)
