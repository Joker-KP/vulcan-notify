# Email notifications and change digests

With `EMAIL_ENABLED=true`, each sync prepares one HTML digest of detected student
changes. Each student has separate sections, in this order: **Oceny**,
**Frekwencja**, **Zastępstwa**, **Anulowane zajęcia**, **Dodatkowe zajęcia**,
**Sprawdziany**, **Zadania domowe**. Empty sections are omitted. Each group has its
own heading, item count, formatting and a final link to the relevant student view.

The subject lists only included, present groups, once each across all students,
with their total change count when there are at least two changes, for example:
`[eduVulcan] Oceny, Frekwencja, Zastępstwa, Dodatkowe zajęcia (sumarycznie 8 zmian)`.
`EMAIL_SUBJECT_PREFIX` remains configurable; its default is `[eduVulcan]`.
First-sync baselines, inbox messages and praise/notes do not count toward this total.
For one included change, the subject is simply `[eduVulcan] Oceny` (or the
corresponding group), without a count suffix.

Completed lessons (`RealizacjaZajec13`, `status=1`) do not trigger email
notifications or form digest groups, subjects or counts. Their stored topics can
optionally provide historical context for the AI summary, as described below.

### Choosing digest groups

`EMAIL_DIGEST_GROUPS` is a JSON object of individual on/off switches. Omitted keys
stay enabled, so the default `{}` includes all seven groups. For example, to omit
attendance and homework while keeping everything else:

```dotenv
EMAIL_DIGEST_GROUPS={"attendance":false,"homework":false}
```

| Key | Group |
| --- | --- |
| `grade` | Oceny |
| `attendance` | Frekwencja |
| `substitution` | Zastępstwa |
| `cancellation` | Anulowane zajęcia |
| `addition` | Dodatkowe zajęcia |
| `exam` | Sprawdziany |
| `homework` | Zadania domowe |

Set a key to `true` to include it or `false` to omit it. Unknown keys fail
configuration validation. The switches apply to all students and only affect
newly prepared digests: omitted groups appear in neither the body, subject,
total count nor AI input. Synchronization, SQLite data and other output channels
remain independent of these email settings. If no included changes remain, no
digest is queued or sent and AI is not called. Separate inbox-message and
praise/note notifications still work. Existing queued emails retain their stored
subject and content on retry, even after changing these switches.

### HTML templates

HTML group templates live in `src/vulcan_notify/email_templates/`: `grade.html`,
`attendance.html`, `substitution.html`, `cancellation.html`, `addition.html`,
`exam.html` and `homework.html`. Edit each independently using `$heading`, `$count`,
`$items` and `$footer`. All email types share `layout.html`, using `$heading`
and `$content` for the surrounding card, background and typography.
Templates use Python's `string.Template` (write `$$` for a literal dollar sign).
Category-specific item content is prepared in `email_digest.py`; upstream text
is escaped before entering HTML. Inline styles work in email clients without
loading external stylesheets. Restart/rebuild after editing bundled templates.
The plain-text MIME alternative is generated from the same HTML, so it does not
require a second set of templates.

Only grade values receive inline colors: 1 dark red (`#991b1b`), 2 orange (`#f97316`),
3 yellow (`#facc15`), 4 guacamole (`#7fb446`), 5 green (`#32c167`),
and 6 darker green (`#15ad4f`).
Plus/minus variants use the base grade's color; other marks remain neutral.
Updated grades color both the old and new values. Substitution emails display
the teacher pair as `sub_teacher → teacher`, correcting the school's reversed
field order for this presentation; stored fields and other outputs are unchanged.

Individual notifications have independent content templates: `message.html`
uses `$subject`, `$metadata`, `$content` and `$footer`; `remark.html` additionally uses
`$student_name`, `$student_context` and `$category`. `notification_footer.html`
styles the shared link button using `$url` and `$label`. Values are escaped or
locally sanitized before insertion. Inbox notifications retain the body opt-in,
bold sender, local dates and original allowed formatting. Their original subject
appears in the email header and as the message section heading. The card heading
shows the student name, with class and school underneath in a smaller 14px font,
matching digest styling.
The shared layout accepts an optional `$heading_context` for that second line.
The client resolves the inbox label through `/api/Skrzynki` and matches its
`globalKey` to the student's `globalKeySkrzynka` from `/api/Context`. This handles
different name order or school abbreviations in mailbox labels. The key is carried
in memory to notification rendering; no database migration is needed. Older results
without a key retain unambiguous exact name/school matching. Unknown or ambiguous
identities use a generic **Wiadomość z eduVULCAN** heading without guessing a child,
class or school; the mailbox remains in message metadata. Praise/notes always include their full content and
show the student and category in the HTML card. Plain-text alternatives for
individual notifications retain their metadata and links; inbox notifications
also include the subject and available student context in plain text.

Group links use the authenticated tenant and URL-encoded student key from the
sync result. Grades open `/oceny`, attendance `/frekwencja`, schedule changes
`/planZajec`, and exams/homework `/sprawdzianyZadaniaDomowe`, all under the
student's `/App/<key>/` URL. The browser may require authentication. Older or
synthetic results without a portal URL omit the link instead of inventing one.

Each newly detected eduVULCAN message generates a separate email, with its original
subject prefixed by `EMAIL_MESSAGE_SUBJECT_PREFIX` (default `[Nowa wiadomość]`),
for example `[Nowa wiadomość] Zebranie rodziców`. It includes the original sender,
date and mailbox identity. Messages are excluded from the change digest. A run with
only new messages sends individual notifications without an empty digest.

Each new praise or behavior note also creates a separate email per recipient,
with `EMAIL_REMARK_SUBJECT_PREFIX` (default `[Uwagi]`) followed by the student's
name and upstream category, e.g. `[Uwagi] Jan: Pochwała`. The student name appears
in the subject; the body contains
the author, date, category, optional points and full note content,
with locally sanitized HTML and a plain-text alternative. Note content is included
regardless of `EMAIL_INCLUDE_MESSAGE_BODIES`, which controls inbox messages only.
The **Otwórz pochwały i uwagi** button and text link open
`<authenticated student base_url>/App/<URL-encoded student key>/pochwalyUwagi`.
The browser may require authentication. Notes are excluded from the change digest
and its AI input. Deduplication uses student key + upstream note ID + recipient;
retries reuse the persisted content and Message-ID.

The first successful notes sync per student stores a baseline without notifying,
including after upgrading an existing installation. An empty successful list also
initializes the baseline; a failed request does not. Subsequent unseen IDs notify.
Edits update stored content without another email. Missing notes are soft-deleted,
and restoring an already known ID does not create a new notification.

Message metadata uses Polish labels: **Autor**, **Data**, **Skrzynka**, and
**Załączniki** when present. The subject appears in the email header and as a
heading in the body, rather than a metadata field. The sender's value is bold in HTML.
Dates use `YYYY-MM-DD HH:MM (dzień tygodnia)`, for example
`2026-09-29 18:23 (wtorek)`, in
`TZ` (default `Europe/Warsaw`, shared with logs, Docker and scheduling), including
daylight-saving changes. Source offsets are respected; timestamps without an offset
are interpreted as UTC, independently of the container timezone. Invalid configured
IANA zones fail startup validation. Defensive rendering falls back to UTC if the
settings object is later modified. Unparseable source dates are retained so the
notification can still be delivered.

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

## Authentication failure alerts

With the same `EMAIL_ENABLED=true` and validated SMTP configuration, the CLI
sends `<EMAIL_SUBJECT_PREFIX> Błąd logowania — wymagana nowa sesja` when a saved
session is unavailable/expired and automatic recovery fails, including failure
to save the new session. `auto_login()` still tries persistent Chromium reuse
before credential login. An expired session during synchronization also notifies
when recovery fails or its one retried sync still reports an expired session.
Without configured eduVULCAN credentials, the alert explains that browser
recovery was not attempted. Explicit interactive `auth` failures also notify;
cancelling the command does not create an alert. The diagnostic `test` command
does not send alerts.

`auth_failure.html` shares `layout.html` with the other notifications; its text
alternative is derived from the same HTML. It includes the detection time in `TZ`,
the effect on synchronization, noVNC commands, an SSH tunnel example, opening
the student's Dziennik, the five-minute login timeout, expected session/profile
paths, and restarting the sync worker. It also explains permissions/disk checks
for failed session writes and the non-Docker `auth` command. It includes no raw
exception text, credentials, cookies, student data or AI processing. Digest
group filters and message-body settings do not affect these alerts.

The outbox and `sync_state["email:auth_failure"]` are committed together, using
one outage identity and per-recipient deduplication. Repeated failed syncs and
process/container restarts retry pending deliveries without generating another
alert for recipients already notified. Successful interactive authentication or
a sync that completes without session expiry clears the outage marker, allowing
the next authentication outage to notify again. Queued alerts retain their original
detection time/content even if access has since recovered; `email-retry` can send
them without upstream authentication. Failed SMTP or database operations are
logged by exception type and do not replace the authentication failure exit status.
No schema migration or additional configuration is required. Run one delivery
owner, as with the other email notifications.

## Test notification with synthetic data

Generate three grades, three substitutions, three additional lessons, three
absences, three exams and three homework items without authenticating to eduVULCAN:

```bash
# Persist synthetic data and save HTML/text previews; no email is queued or sent.
uv run python -m vulcan_notify.demo_email

# Generate a new set and send the real digest to EMAIL_TO using the SMTP settings.
uv run python -m vulcan_notify.demo_email --send

# Retry a failed delivery using the stored notification, without generating new data.
uv run python -m vulcan_notify.demo_email --retry
```

The default database is `data/email-demo.db`, separate from the application database.
The script rejects `DB_PATH` and existing databases without its demo marker.
It uses a fictional student and the normal `sync_student()` pipeline: a silent
baseline followed by 18 detected changes. Substitutions modify three baseline
lessons; additional lessons create three new schedule rows. Each new invocation
without `--retry` creates another fictional student and data set. `--seed 42`
makes the sample content reproducible; IDs remain unique across invocations.

With all groups enabled, the six categories produce **one digest per configured
recipient**, rather than 18 separate emails. With `EMAIL_DIGEST_GROUPS` set, only
included groups appear in the preview and email, although all 18 changes remain
in the demo database. Sending requires at least one included group.
The subject and body use the normal email renderer, including
its current language/formatting and optional `EMAIL_AI_SUMMARY`. The fictional
student name and example details identify the test; the normal subject prefix is
preserved. Inbox messages and praise/notes are not part of this six-category test.

Previews are saved to `data/email-demo.html` (open in a browser) and
`data/email-demo.txt`. Sending also saves
`data/email-demo.eml` with the actual queued email headers and body before SMTP
delivery clears them from the outbox. Open this file in a mail client to inspect
the message. These files include configured email addresses and are kept in the
git-ignored `data/` directory by default. SMTP failure leaves the notification in
the test database and exits nonzero; `--retry` preserves its body and Message-ID.
A new `--send` is refused while previous test deliveries remain queued.

For Docker, rebuild the image to include the module, then run against the separate
test database in the persistent volume:

```bash
docker compose build vulcan-sync
docker compose run --rm --no-deps --entrypoint uv vulcan-sync \
  run python -m vulcan_notify.demo_email --db /app/data/email-demo.db --send
```

Use `--retry` instead of `--send` to retry in Docker. `--db` overrides the demo
database path; previews are written beside it. A lock prevents concurrent demo
runs against the same test database. The production worker can continue running
because its database and notification queue are separate.

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
| `EMAIL_SUBJECT_PREFIX` | `[eduVulcan]` | Subject is `<prefix> <present groups> (sumarycznie <N> zmian)`, with Polish count inflection. |
| `EMAIL_DIGEST_GROUPS` | `{}` (all enabled) | JSON object of group switches; omitted keys stay enabled. Filtering applies to digest body, subject, count and AI input; one change has no subject count suffix. |
| `EMAIL_MESSAGE_SUBJECT_PREFIX` | `[Nowa wiadomość]` | Separate message email subject is `<prefix> <original subject>`; upstream line breaks are flattened. |
| `EMAIL_REMARK_SUBJECT_PREFIX` | `[Uwagi]` | Separate praise/note email subject is `<prefix> <student>: <category>`. |
| `TZ` | `Europe/Warsaw` | Shared runtime/display timezone; dates use `YYYY-MM-DD HH:MM (dzień tygodnia)`. `QUIET_HOURS_TZ` remains a legacy fallback. |
| `EMAIL_INCLUDE_MESSAGE_BODIES` | `false` | Include formatted HTML content and a readable text alternative; attachment files are never sent. |
| `EMAIL_AI_SUMMARY` | `false` | Add an AI summary above the change groups when `LLM_API_KEY` is also set. |
| `LLM_INCLUDE_LESSONS` | `false` | Include stored lesson topics as AI context for students with included digest changes. Also applies to CLI change summaries. |
| `LLM_LESSONS_DAYS` | `7` | Positive number of local calendar days including today for lesson-topic context. |
| `EMAIL_AI_TIMEOUT_SECONDS` | `30` | Maximum time allowed for AI preparation before using the grouped digest alone. |

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

## Optional AI summary

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
timeouts and empty AI responses all leave the grouped digest intact.
Successful AI output adds a summary above the groups in both MIME alternatives;
all change details, headings and links remain. AI output is escaped as text in HTML.
The subject retains the detected groups and event count.

To also summarize what the students have been learning, enable:

```dotenv
LLM_INCLUDE_LESSONS=true
LLM_LESSONS_DAYS=7
```

The AI receives stored lesson topics from SQLite for students with included changes
in this digest. The range uses the **lesson date**, from midnight six days ago
through the current instant for the default seven days, in `TZ`. Future lessons,
soft-deleted rows, retired profiles and blank topics are excluded. The range is
independent of `SYNC_COMPLETED_LESSONS_DAYS`; AI sees only history already fetched,
so choose a synchronization window large enough to cover the desired period.
The default prompt groups topics by student and subject, combines related topics
and omits routine activities such as lunch, commuting and breaks. This selection
explicitly excludes daily **obiad**, weekly **Przejazd na basen** and
**Przejazd na Jasną** (commuting to sports classes), including equivalent wording.
Actual educational or sports lesson content is retained; summaries do not list
the omitted routine activities. This applies to both `[default]` learning context
and `[lessons]` standalone/mixed summaries. Selection is performed by the model
and can be customized in the corresponding profile in `prompts.toml`.
It describes recorded topics, without assuming skills or learning outcomes.

Topics are additional AI context, rather than notification events. Baselines,
unchanged runs, lesson-only changes and digests with all groups disabled still
create no digest and make no AI request. Only the generated overview is included
above the usual digest groups; raw lesson-topic rows are not added to the email.
Retries reuse the saved summary and do not query lessons or AI again. Leaving
`LLM_INCLUDE_LESSONS=false` retains the change-only AI input.

A separate summary of topics can be generated explicitly without a new sync:

```bash
uv run vulcan-notify summarize --type lessons
uv run vulcan-notify summarize --type lessons --days 14
```

This requires `LLM_API_KEY`, uses the `[lessons]` prompt profile and
defaults to `LLM_LESSONS_DAYS`. It works independently of email and the
context switch, reading active students' stored topics. CLI `--type sync` uses
`LLM_INCLUDE_LESSONS` and `LLM_LESSONS_DAYS` for optional topic
context; its `--days` continues to set the separate change-history range.

### Mixed summary email

```bash
uv run vulcan-notify summarize --type mix --days 7
```

`mix` reads local SQLite without authentication or a new eduVULCAN sync. It
independently runs the same AI profiles and inputs as `--type messages` and
`--type lessons`, passing `--days` to both data queries. The default is 7 for both,
independent of `LLM_LESSONS_DAYS`. Message selection retains the existing
`messages` date query; lessons use local calendar days including today, in `TZ`.

The command requires `LLM_API_KEY`, `EMAIL_ENABLED=true` and valid SMTP/sender/
recipient settings. `EMAIL_AI_SUMMARY`, `LLM_INCLUDE_LESSONS`,
`EMAIL_INCLUDE_MESSAGE_BODIES` and `EMAIL_DIGEST_GROUPS` do not control this
explicit summary: stored message bodies are included in its messages AI input,
just as for `--type messages`.
The `[messages]` prompt requests one concise, practical overview across the whole
batch, combining related facts by topic and highlighting dates, deadlines and
required actions. It avoids separate per-message summaries/checklists and repeated recaps.

Each configured recipient receives one email using `layout.html`, with at most
two sections, in this order:

1. **Wiadomości**, rendered by `summary_messages.html`.
2. **Przeprowadzone zajęcia**, rendered by `summary_lessons.html`.

Both subtemplates accept `$content` and `$footer`. The `[messages]` and `[lessons]`
prompts request Markdown, also for their standalone CLI summaries. In mixed
emails, `markdown-it-py` converts that Markdown into HTML with inline email styles
for headings, lists, emphasis and tables. Raw HTML is escaped and the rendered
fragment goes through the existing local sanitizer; active content, unsafe links
and remote images are removed. AI headings cannot replace the card/section h1/h2.
The readable text MIME alternative is derived from the same HTML, including URLs.

Each section ends with ordinary links using `summary_footer.html` (`$url`, `$label`).
The messages link opens the unified inbox. Completed lessons have a link
for each active stored student, labeled **Otwórz przeprowadzone zajęcia** without
student names/classes. URLs use stable profile keys; retired profiles are excluded.
Public URLs come from the
saved session's tenant/base URL: `/App/odebrane` on the messages host and
`/App/<URL-encoded student key>/realizacjaZajec` on the student host. Reading the
session does not validate it upstream or start a browser. If it is missing or
invalid, summaries still work and the links are omitted. The browser may
require authentication. URLs and formatted bodies persist for SMTP retries.

Missing source data, empty AI results or a failed/timed-out AI request omit that
section. Each AI request uses `EMAIL_AI_TIMEOUT_SECONDS`. If neither section has
a result, no new email is queued and the command exits nonzero.

For 7 days, the subject is `<EMAIL_SUBJECT_PREFIX> Podsumowanie tygodnia`;
other ranges use `<EMAIL_SUBJECT_PREFIX> Podsumowanie (ostatnie N dni)`.
The email body retains its original day-range context. Generated
content is committed to the per-recipient outbox before SMTP delivery. Failed
SMTP delivery leaves it queued and exits nonzero; use `vulcan-notify email-retry`
to resend the saved bodies and Message-ID without another AI request. Running
`summarize --type mix` again creates a fresh summary email. This command also
drains any previously queued emails.

## Delivery and retries

Individual message notifications and both digest alternatives are committed to `email_outbox`
before calling AI or SMTP. Any AI addition to the digest is saved once; retry
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
