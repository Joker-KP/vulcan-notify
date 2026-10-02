# eduVULCAN API contract

[`openapi.yaml`](openapi.yaml) is a reverse-engineered OpenAPI 3.1 description of
the upstream web API. It covers all 30 GET endpoints in the older
[`eduvulcan-api.md`](../eduvulcan-api.md) reference. It is not an official provider
specification, and one account cannot establish what every school or role exposes.
The current inventory contains 55 paths: 54 GET operations and one POST-only
candidate. The expanded live navigation crawl added 18 GET endpoints.
The inventory includes reviewed GET operations and browser-discovered operations,
whose standalone probes stay disabled pending review. Request-only observations
have unknown response schemas; a discovered GET is not automatically a verified
or safe-to-replay GET.

## On-demand jobs

Run these from the repository root after `uv sync --extra dev`. Neither command is
scheduled by the application. They do not synchronize SQLite, publish school
notifications or overwrite the session file. Chromium starts only with the
explicit `--browser-discover` option.

```bash
# Explicitly collect/update the reviewed baseline and sanitized fixtures.
uv run vulcan-notify api-gather --session data/session.json \
  --browser-discover \
  --report data/eduvulcan-gather-report.json

# Read the baseline and compare it with current upstream responses.
uv run vulcan-notify api-check --session data/session.json \
  --browser-discover \
  --report data/eduvulcan-check-report.json

# Additionally require every endpoint to be available and have probe inputs.
uv run vulcan-notify api-check --session data/session.json --require-complete

# Offline validation, including replay through the actual application parsers.
uv run pytest tests/test_eduvulcan_contract.py -q
```

`--session` defaults to `Settings.session_file` (`SESSION_FILE`, including `.env`),
`--spec` to `docs/eduvulcan/openapi.yaml`, `--fixtures` to
`tests/fixtures/eduvulcan`, and `--report` to
`data/eduvulcan-contract-report.json`. Separate report filenames preserve the
gather and check results. `--days` defaults to 30 (range 1–366); `--samples`
defaults to 3 (range 1–20) and bounds period/detail requests per student.
`--no-discover` checks the inventory without fetching frontend scripts.
`--browser-discover` additionally visits the visible navigation modules and every
enabled `role=tab` control on the student and messages sites in a transient browser
seeded with saved cookies. Collapsed submenu links are opened directly when
necessary. It records route names,
methods and query-parameter names, never their values. JSON responses already
received by the browser are sampled in memory and sanitized for new GET fixtures
and schemas; raw bodies are never written. It
uses headed Chromium by default, matching authentication recovery; export
`VULCAN_BROWSER_HEADLESS=true` to opt into headless operation. Playwright Chromium
and a display for headed mode must be available. It blocks non-GET API requests,
known side-effecting GETs and all message-body GETs. It never clicks rows, action
buttons, pagination or attachment/resource links, submits credentials, or saves
browser storage. Service workers are blocked so they cannot bypass interception.
New or changed observed request metadata fails
the check and is written only by gathering. Previously observed routes missing
from a successful browser observation are reported separately; this can also
reflect a module or account setting change rather than a provider API break.
The crawl defaults to at most 60 modules per host and 20 tabs per module;
`--browser-max-views` (1–200) and `--browser-max-tabs` (1–100) can raise those limits.
Reached limits, disabled tabs, navigation failures and failed response capture
are reported as incomplete discovery and produce a nonzero exit code.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Check found no changes in sampled responses, or gathering completed without failures. |
| 1 | Drift, an unbaselined response, a new frontend route, an unexpected request/discovery failure, or incomplete gathering. Successful gather observations are still saved. |
| 2 | Invalid configuration, missing/unreadable session, concurrent contract job, or another startup/input error. |

A check can exit 0 with `complete: false`: detail endpoints may lack sample IDs,
and a route previously recorded as returning 404 may still be unavailable.
These outcomes are printed and remain in the JSON report. `--require-complete`
makes them fail. Authentication failures, 403, rate limiting and server errors
always fail; they are never accepted just because they failed before.

Reports contain endpoint paths, anonymous student scope indexes, HTTP status,
validation locations/rules, discovery outcomes and coverage gaps. They contain
no response values, URLs with query parameters, cookies or student names.
They can be consumed by a future scheduler or notification adapter using the
exit code and `changes`/`results` fields. These jobs currently send no notifications.
`discovery` lists visited public view names and tab counts; encoded profile keys
are stripped. `browser_requests` includes `blocked-methods` where applicable,
and `unsampled_gets` lists inventory GETs with no response sample in that run.

## Authentication and Docker

The student and messages applications are on separate hosts:

- `https://uczen.eduvulcan.pl/{tenant}`
- `https://wiadomosci.eduvulcan.pl/{tenant}`

The jobs reuse `session.json` and select cookies for each host. The tenant is
resolved at runtime from the student base URL; no actual tenant or student keys
are embedded in the specification. The security scheme names the session cookie,
but the entire stored SSO cookie set is needed in practice.

Stop the sync worker before a manual job or authentication recovery. The contract
jobs lock one another through `eduvulcan-contract.lock` beside the session file;
the regular worker does not participate in that lock. An expired session is
reported without silently launching interactive authentication. Use the existing
authentication workflow when needed, preserving the persistent browser profile.

The Docker image includes the contract and its runtime validation dependencies.
Mount the repository contract directory and fixtures when gathering so that the
results survive the one-off container. For example, after building the image:

```bash
docker compose stop vulcan-sync

docker compose run --rm --no-deps \
  -v "$PWD/docs/eduvulcan:/app/docs/eduvulcan" \
  -v "$PWD/tests/fixtures/eduvulcan:/app/tests/fixtures/eduvulcan" \
  vulcan-sync uv run vulcan-notify api-gather \
  --browser-discover \
  --report /app/data/eduvulcan-gather-report.json

docker compose run --rm --no-deps \
  -v "$PWD/docs/eduvulcan:/app/docs/eduvulcan:ro" \
  vulcan-sync uv run vulcan-notify api-check \
  --browser-discover \
  --report /app/data/eduvulcan-check-report.json

docker compose start vulcan-sync
```

For interactive recovery, stop the worker, run
`docker compose --profile auth up vulcan-auth`, then restart the worker. Container
ownership may require gathering inside Docker rather than using the volume's
files directly from the host. No migration or additional environment settings
are required.

## Discovery and evidence

The gather job probes only reviewed GET operations with enabled `x-probe`
metadata. It discovers student keys from Context and obtains period/detail IDs
from the appropriate list endpoint, retaining each student's scope. Message
detail requests use only messages already marked read. List polling is bounded
to the first 20 messages; this is not a full inbox export or pagination test.
Date ranges frame Europe/Warsaw local midnights and are serialized as UTC.

Frontend discovery scans the two `/App` pages and recursively follows literal
JavaScript imports and script loaders, up to 80 scripts per host. It recognizes
escaped API paths and bracket-form HTTP methods as well as plain literals. Public scripts on provider-owned
`*.eduvulcan.pl` and `*.vulcan.net.pl` hosts are fetched without session cookies;
other hosts and redirects are not followed. Requests are sequential, delayed,
have a 30-second timeout and body limits of 5 MiB for APIs and 32 MiB for scripts.
Computed chunk names and obfuscated endpoint strings cannot be reliably recovered
with static scanning. Incomplete script discovery is reported.
The current frontend obfuscates request names, so runtime browser discovery is
recommended. It waits for delayed SPA startup requests, then walks navigation
modules and their tabs. It scans scripts actually loaded during navigation,
including lazy chunks, and drains response capture before changing pages. Browser
samples are checked even when a standalone probe already sampled that endpoint,
so additional tab-specific response shapes are not missed.

This covers visible modules of the active browser profile, not every server route.
Reviewed standalone probes still cover all active students returned by Context.
The browser does not change students, enumerate all date/filter combinations,
open individual records or exercise forms. Other schools, roles, optional modules,
detail/resource workflows and unexposed server routes may have additional GETs.
The report states this scope; successful discovery does not prove global API completeness.

New route candidates are stored as OpenAPI path items with `x-discovery-hosts`
and a description, without an assumed HTTP operation. They are never blindly
called. Review their method, parameters, scope and possible side effects before
adding an operation and probe. A check reports new candidates without editing
the specification.
If the browser observes a GET, gathering adds a disabled operation. Successful
JSON observations additionally produce schemas and sanitized fixtures; requests
without captured responses remain explicitly unverified. Browser discovery
can check its response again without replaying the request outside the App.
POST routes remain metadata only. An observed method is not evidence that it is
safe to execute independently.

Each reviewed GET operation has:

- `x-host`: student or messages host.
- `x-probe`: scope, fixed parameters, dates and request dependencies.
- `x-evidence`: documentation or live observation provenance.
- `x-observed-schema`: cumulative structural evidence used for drift detection.
- `x-observed-unavailable`, when applicable: a previously observed 404.
- `x-browser-request`, when runtime discovery is requested: observed HTTP methods
  and query-parameter names, plus blocked methods. This does not establish response semantics or grant
  permission to replay unreviewed operations.

Standard response schemas describe known accepted shapes. The separate
observation schema detects new properties, missing commonly observed properties,
and incompatible types. Numbers, strings, dates, IDs, ordering and row counts are
not compared by value. Optional properties seen only on some rows stay optional;
nullable variants are retained. Null-only fields and item shapes of empty arrays
remain unknown and are listed under `unknown_structure` in the report.

Gathering accumulates observed variants and never erases previously learned item
shapes merely because a collection becomes empty. It does not remove old paths.
Review the Git diff when updating the baseline; intentionally retire outdated
fields/routes after investigating them. The check command never changes contracts
or fixtures. Responses contradicting the existing reviewed response schema are
reported and excluded from gathering until the schema is reviewed.

## Sanitized fixtures

Fixture envelopes identify the endpoint and their source:
`synthetic-from-documentation` or `sanitized-live`. Every scalar value in a live
fixture is replaced, including text, credentials/tokens, identifiers, numeric
values, booleans and dates. Collections are reduced to three entries for fixture
size; schema inference examines every returned row. Dynamic object keys requiring
manual anonymization are rejected. Nonempty existing fixtures are preserved when
a later response is empty.

No raw bodies, HAR captures, browser storage, cookies or household configuration
belong in Git. The contract tests always operate offline and never read the real
session. Synthetic detail examples do not establish live endpoint availability.

## Findings and remaining uncertainty

The initial live collection reached 27 of the 30 documented endpoints with
successful JSON responses. Empty responses establish transport and collection
shape, not item semantics.

Runtime discovery found seven additional routes: `EgzaminyKoncoweTablica`,
`InformacjeTablica`, `DyzurniTablica`, `ZdjecieTablica`, `SzczesliwyNumerTablica`,
`WazneDzisiajTablica` and `StatystykiLogowan`. The last route is POST, and the lucky
number widget uses both GET and POST. These operations are not added to normal
school synchronization and are not independently replayed by the probe runner.

The expanded crawl visited all 18 visible menu views for the current account
(14 student views and four message folders), including each enabled tab. It
added these GET endpoints, with sanitized JSON fixtures and disabled standalone
probes:

- `Osiagniecia`, `Egzaminy`, `EgzaminyZewnetrzne`, `OcenyDiagnostyczne`.
- `ZglaszanieNieobecnosciKonfiguracja`, `ZgloszoneNieobecnosci`.
- `RealizacjaZajec13`: `/realizacjaZajec`, `status=2` for planned and `status=1`
  for completed lessons. Completed rows contain `tematOpis`, `online`,
  `existsKolekcjePoLekcji`, `kolekcjePoLekcji` and `zasoby`. Collections were empty
  and resources null in the sample; their populated structures remain unknown.
- `Informacje`, `Nauczyciele`, `FormularzWysylanie`, `FormularzeSzablony`.
- `Zebrania`, `PodrecznikiLataSzkolne`, `PodrecznikiUcznia`,
  `NajczesciejZadawanePytania`.
- `Wyslane`, `Usuniete`, `Kopie`: folder lists, without opening individual messages.

The original browser discovery opened only landing pages. Static scanning did
not expose these obfuscated route names, and it never triggered module or tab
requests. The expanded crawler addresses that gap; it still cannot enumerate
unexposed server routes or workflows outside its documented scope.

- `SprawdzianyZadaniaDomowe` returned 404 with the former key-only probe. The web
  view supplies `dataOd` and `dataDo`; adding that date range was verified to
  return HTTP 200. The probe now includes dates. Its empty sample establishes
  collection transport, while the populated item shape remains unknown.
- No current exam/homework list IDs were available for `SprawdzianSzczegoly` and
  `ZadanieDomoweSzczegoly`. Their fixtures remain synthetic.
- Grade subject fields including `ocenaOkresowa`, `nauczyciele` and
  `kolumnyOcenyCzastkowe` can be null. The contract includes those variants.
- Message details were fetched for already-read messages. Full pagination,
  attachments, write operations, and undocumented side effects remain unverified.
- Attendance category meanings and exam type meanings remain observations rather
  than verified enumerations. Unknown numeric codes are permitted.

The report generated by each run is the current authority on coverage; this
initial collection is not a promise that every endpoint remains available.

References: [OpenAPI 3.1 specification](https://spec.openapis.org/oas/v3.1.1.html),
[JSON Schema validation](https://python-jsonschema.readthedocs.io/en/stable/validate/).
