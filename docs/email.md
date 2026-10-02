# Email change digests

With `EMAIL_ENABLED=true`, each sync prepares one plain-text digest of detected
student changes: grades, attendance, exams, homework, schedule substitutions and
cancellations/additions. Students have separate sections.

Each newly detected eduVULCAN message generates a separate email, with its original
subject prefixed by `EMAIL_MESSAGE_SUBJECT_PREFIX` (default `[Nowa wiadomość]`),
for example `[Nowa wiadomość] Zebranie rodziców`. It includes the original sender,
date and mailbox identity. Messages are excluded from the change digest. A run with
only new messages sends individual notifications without an empty digest.

Message metadata uses Polish labels: **Nadawca**, **Data**, **Skrzynka**, and
**Załączniki** when present. The subject appears only in the email header and is
not repeated as a metadata field in the body. The sender's value is bold in HTML.
Dates use `YYYY-MM-DD HH:MM (dzień tygodnia)`, for example
`2026-09-29 18:23 (wtorek)`, in
`QUIET_HOURS_TZ` (default `Europe/Warsaw`), including daylight-saving changes.
Source offsets are respected; timestamps without an offset are interpreted as UTC,
independently of the container timezone. An unknown timezone falls back to UTC;
unparseable source dates are retained so the notification can still be delivered.

When `EMAIL_INCLUDE_MESSAGE_BODIES=true`, the HTML alternative retains the
original paragraphs, explicit line breaks, lists, tables, emphasis and supported
inline font/spacing styles. The original message HTML is sanitized locally using
[nh3](https://nh3.readthedocs.io/en/latest/). Scripts, embedded active content,
remote images and unsupported style properties are removed; attachment files are
not forwarded. Original links are retained for HTTP/HTTPS/mailto, with relative
links resolved against the inbox URL. The plain alternative preserves paragraph
boundaries, explicit breaks, list items, table rows and HTML entities rather than
simply stripping tags. Literal newlines in messages containing plain text are also
preserved. Typography may vary between email clients.

Paragraphs have zero default top/bottom margins in HTML to avoid extra gaps around
editor-generated empty paragraphs or explicit breaks. Author-specified inline
margins remain effective. The plain alternative uses one newline between adjacent
paragraphs, keeping explicit blank lines without adding another paragraph gap.
This affects newly queued notifications; retries retain their stored content.

Individual notifications also include an **Otwórz skrzynkę wiadomości** button at
the end of the HTML version, plus the same inbox URL in the plain-text version.
The client derives `https://wiadomosci.eduvulcan.pl/<tenant>/App/odebrane` from
the authenticated session (falling back to the tenant in `base_url` for older
sessions). This opens the account's received-message inbox; it does not select a
particular message or child mailbox. The browser may require authentication.
No tenant name, session cookie or token is hard-coded into the email template.

Each notification is sent individually to every configured recipient, without
exposing other recipients' addresses.

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
| `EMAIL_ENABLED` | `false` | Enables change digests, individual message notifications and queued delivery. |
| `SMTP_HOST` | empty | Required when email is enabled. |
| `SMTP_PORT` | `587` | Use `465` explicitly for implicit TLS if required by your provider. |
| `SMTP_SECURITY` | `starttls` | `starttls`, `ssl` (implicit TLS), or `none` (unencrypted relay). TLS checks certificates and hostnames. |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | unset | Optional credentials; set both together. |
| `SMTP_TIMEOUT_SECONDS` | `30` | Timeout for blocking SMTP socket operations, which run in a worker thread. |
| `EMAIL_FROM` | empty | Required sender address, optionally `Name <address>`. |
| `EMAIL_TO` | `[]` | Required recipient list. |
| `EMAIL_SUBJECT_PREFIX` | `eduVULCAN` | Subject is `<prefix>: <N> change(s)`. |
| `EMAIL_MESSAGE_SUBJECT_PREFIX` | `[Nowa wiadomość]` | Separate message email subject is `<prefix> <original subject>`; upstream line breaks are flattened. |
| `QUIET_HOURS_TZ` | `Europe/Warsaw` | Also controls displayed dates and weekdays; format is `YYYY-MM-DD HH:MM (dzień tygodnia)`. |
| `EMAIL_INCLUDE_MESSAGE_BODIES` | `false` | Include formatted HTML content and a readable text alternative; attachment files are never sent. |
| `EMAIL_AI_SUMMARY` | `false` | Replace the plain digest with an AI summary when `LLM_API_KEY` is also set. |
| `EMAIL_AI_TIMEOUT_SECONDS` | `30` | Maximum time allowed for AI preparation before using the plain digest. |

The terminal's `MESSAGE_SENDER_WHITELIST` does not filter email: every detected
new message creates a notification. Message subjects, senders, dates and mailbox names are
included even when message bodies are disabled.

For Docker, rebuild and recreate the sync service after editing configuration:

```bash
docker compose build vulcan-sync
docker compose up -d vulcan-sync
```

SMTP uses the Python standard library; HTML processing uses the bundled `nh3`
dependency and requires no additional services. Compose already passes `.env`
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
or terminal output. Only enabling `EMAIL_AI_SUMMARY` permits this change digest
to be sent to the configured model provider. Individual message notifications do
not use AI; their metadata and message bodies are excluded from the digest and its
AI input, even with `EMAIL_INCLUDE_MESSAGE_BODIES=true`. Missing configuration, errors,
timeouts and empty AI responses all leave the plain digest as the email body.
Successful AI output replaces the body; the subject retains the event count.

## Delivery and retries

Individual message notifications and the plain digest are committed to `email_outbox`
before calling AI or SMTP. Any AI replacement of the digest is saved once; retry
sends the stored version without another AI call. Digest delivery identity uses the
sync-run ID and recipient, so a later run can legitimately report the same kind of
change again. Individual message identity uses the upstream message key (or numeric
ID if the key is absent) and recipient; rediscovering that message in a different
run does not enqueue another notification.

Previously queued emails retain their stored body and subject, including older
combined digests. They are retried without rewriting or deleting their content.

SMTP acceptance is recorded separately for each recipient. Failed deliveries remain
queued and are retried on subsequent syncs, including unchanged syncs. SMTP failures
are logged using only the exception type and do not fail synchronization or prevent
other output channels. Turning off email pauses delivery of the existing queue.
Successful records retain their delivery identity and timestamps; sender, recipient,
subject and both text/HTML bodies are cleared. Pending records contain private school information.
The outbox stores the HTML alternative and inbox link when the notification is
queued, so restart/retry preserves them. Database initialization adds the nullable
`html_body` column automatically, preserving existing queued emails. Older queued
notifications remain plain text and retain their original content.

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
