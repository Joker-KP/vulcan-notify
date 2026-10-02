# Email change digests

With `EMAIL_ENABLED=true`, each sync prepares one plain-text digest of the changes
detected in that run: grades, attendance, exams, homework, schedule substitutions,
cancellations/additions, and new messages. Students have separate sections; messages
retain their mailbox identity. The digest is sent individually to each configured
recipient, without exposing other recipients' addresses.

Baseline synchronization and runs without new changes create no digest. Enabling
email on an existing installation starts with the next detected changes, without
sending historical records. Partial/degraded runs send the changes they successfully
detected; session-expiry recovery delivers those changes before retrying the sync.

## Configuration

Add your SMTP settings to `.env`:

```dotenv
EMAIL_ENABLED=true
SMTP_HOST=smtp.example.org
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USERNAME=school@example.org
SMTP_PASSWORD=your_smtp_password
EMAIL_FROM="Some One <school@example.org>"
EMAIL_TO=["parent@example.org"]
```

`EMAIL_FROM` accepts a bare address or a sender name, such as
`"Some One <school@example.org>"`. The name appears in the email's `From` header;
SMTP uses only `school@example.org` as the envelope sender. Unicode names are
supported. Names containing commas need inner quotes, for example:

```dotenv
EMAIL_FROM='"One, Some" <school@example.org>'
```

Every `EMAIL_TO` entry must be a bare email address. `EMAIL_TO` is a JSON array,
including when there is only one recipient. Leave both authentication
settings unset for a relay that does not require authentication.

| Setting | Default | Behavior |
| --- | --- | --- |
| `EMAIL_ENABLED` | `false` | Enables digest preparation and queued delivery. |
| `SMTP_HOST` | empty | Required when email is enabled. |
| `SMTP_PORT` | `587` | Use `465` explicitly for implicit TLS if required by your provider. |
| `SMTP_SECURITY` | `starttls` | `starttls`, `ssl` (implicit TLS), or `none` (unencrypted relay). TLS checks certificates and hostnames. |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | unset | Optional credentials; set both together. |
| `SMTP_TIMEOUT_SECONDS` | `30` | Timeout for blocking SMTP socket operations, which run in a worker thread. |
| `EMAIL_FROM` | empty | Required sender address, optionally `Name <address>`. |
| `EMAIL_TO` | `[]` | Required recipient list. |
| `EMAIL_SUBJECT_PREFIX` | `eduVULCAN` | Subject is `<prefix>: <N> change(s)`. |
| `EMAIL_INCLUDE_MESSAGE_BODIES` | `false` | Include HTML-to-text message content; attachment files are never sent. |
| `EMAIL_AI_SUMMARY` | `false` | Replace the plain digest with an AI summary when `LLM_API_KEY` is also set. |
| `EMAIL_AI_TIMEOUT_SECONDS` | `30` | Maximum time allowed for AI preparation before using the plain digest. |

The terminal's `MESSAGE_SENDER_WHITELIST` does not filter email: the digest covers
all detected new messages. Message subjects, senders, dates and mailbox names are
included even when message bodies are disabled.

For Docker, rebuild and recreate the sync service after editing configuration:

```bash
docker compose build vulcan-sync
docker compose up -d vulcan-sync
```

No SMTP packages or additional services are needed. Compose already passes `.env`
to the application, and the outbox lives in SQLite on the existing persistent
`/app/data` volume. The API container does not send email.

## Optional AI replacement

```dotenv
EMAIL_AI_SUMMARY=true
LLM_API_KEY=your_api_key
# Optional: LLM_BASE_URL, LLM_MODEL, PROMPTS_FILE
```

This reuses `summarizer.summarize()` and the `[default]` profile in `prompts.toml`.
It receives the plain digest for the current run, rather than a historical query
or terminal output. Only enabling `EMAIL_AI_SUMMARY` permits this email input to
be sent to the configured model provider. `EMAIL_INCLUDE_MESSAGE_BODIES` also
controls whether message bodies reach the model. Missing configuration, errors,
timeouts and empty AI responses all leave the plain digest as the email body.
Successful AI output replaces the body; the subject retains the event count.

## Delivery and retries

The plain digest is committed to `email_outbox` before calling AI or SMTP. Any AI
replacement is saved once; retry sends the stored version without another AI call.
Delivery identity uses the sync-run ID and recipient. Re-delivering the same result
does not enqueue duplicate messages, while a later run can legitimately report the
same kind of change again.

SMTP acceptance is recorded separately for each recipient. Failed deliveries remain
queued and are retried on subsequent syncs, including unchanged syncs. SMTP failures
are logged using only the exception type and do not fail synchronization or prevent
other output channels. Turning off email pauses delivery of the existing queue.
Successful records retain their delivery identity and timestamps; sender, recipient,
subject and body are cleared. Pending records contain private school information.

To retry without authenticating to eduVULCAN or regenerating summaries:

```bash
uv run vulcan-notify email-retry
```

In Docker, stop the worker to avoid overlapping delivery processes, run the retry,
then restart it (also restart it if the retry exits nonzero):

```bash
docker compose stop vulcan-sync
docker compose run --rm --no-deps --entrypoint uv vulcan-sync run vulcan-notify email-retry
docker compose start vulcan-sync
```

`email-retry` exits nonzero when email is disabled or deliveries remain pending.
Quiet-hour MQTT heartbeats do not drain email; automatic retries resume with syncs.
Queued envelopes keep the original sender/recipient even if `.env` changes; SMTP
transport credentials come from the current configuration.

SMTP acceptance does not prove delivery to the inbox. A dropped connection after
the server accepts the message, or a crash before the success is recorded in SQLite,
can cause a duplicate on retry. Retries preserve `Message-ID` and `Date`, but SMTP
does not guarantee deduplication. Entity persistence and email enqueue are separate
commits, so a crash between them can lose an unqueued notification. Run only one
sync/delivery process at a time; the outbox does not coordinate concurrent workers.
