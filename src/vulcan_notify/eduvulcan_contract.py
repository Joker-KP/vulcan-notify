"""On-demand upstream API inventory and structural compatibility checks.

No database writes, school notifications or raw traffic storage. Browser startup
is opt-in. Only reviewed GET operations with x-probe metadata are replayed;
browser discovery visits visible modules and their tabs with write requests blocked.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import fcntl
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlsplit
from zoneinfo import ZoneInfo

import aiohttp
import yaml
from jsonschema import Draft202012Validator

from vulcan_notify.auth import _make_ssl_context, cookies_for_url, load_session
from vulcan_notify.client import _BROWSER_HEADERS

HOSTS = {"student": "uczen.eduvulcan.pl", "messages": "wiadomosci.eduvulcan.pl"}
ROUTE = re.compile(r"/api/[A-Za-z][A-Za-z0-9]*$")
FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")
MAX_BODY = 5 * 1024 * 1024
MAX_ASSET_BODY = 32 * 1024 * 1024
MAX_SCRIPTS = 80
VIEW_PART = re.compile(r"[a-z][A-Za-z0-9]{0,47}$")
UNSAFE_VIEWS = {"logout", "wyloguj", "auth", "login", "delete", "usun", "nowaWiadomosc"}
# Some GETs have side effects. Listing folders is safe; opening unread message
# bodies is not. Unknown candidates are never replayed outside the application.
UNSAFE_GET = re.compile(
    r"WiadomoscSzczegoly|(?:Usun|Delete|Wyslij|Send|Zapisz|Save|Oznacz|Mark|Wyloguj|Logout)", re.I
)
Json = Any


def load_contract(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    if document.get("openapi") != "3.1.0":
        raise ValueError("Expected an OpenAPI 3.1.0 contract")
    for route, item in document["paths"].items():
        if not ROUTE.fullmatch(route):
            raise ValueError("Contract contains an invalid API path")
        operation = item.get("get")
        if operation:
            Draft202012Validator.check_schema(response_schema(operation))
    return document


def response_schema(operation: dict[str, Any]) -> dict[str, Any]:
    schema: dict[str, Any] = (
        operation["responses"]
        .get("200", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
    )
    return schema


def sanitize(value: Json, field: str = "") -> Json:
    """Replace every scalar value; never preserve names, text, IDs or tokens.

    Dynamic object keys cannot safely be published and are rejected. Dates retain
    their syntax with a fixed synthetic date, not their original values.
    """
    if isinstance(value, dict):
        if any(not FIELD.fullmatch(key) for key in value):
            raise ValueError("Dynamic response keys require manual sanitization")
        return {key: sanitize(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item, field) for item in value[:3]]
    if value is None:
        return None
    if isinstance(value, bool):
        return field == "aktywny"
    if isinstance(value, (int, float)):
        return 1 if isinstance(value, int) else 1.5
    if isinstance(value, str):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", value):
            return "2000-01-03T08:00:00+01:00"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return "2000-01-03"
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value):
            return "03.01.2000"
        if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
            return "00000000-0000-4000-8000-000000000001"
        return "SYNTHETIC" if value else ""
    raise ValueError("Unsupported response value")


def infer_schema(value: Json) -> dict[str, Any]:
    """Infer structure only. Null-only fields and empty arrays remain unknown."""
    if isinstance(value, dict):
        if any(not FIELD.fullmatch(key) for key in value):
            raise ValueError("Dynamic response keys require manual sanitization")
        return {
            "type": "object",
            "properties": {key: infer_schema(value[key]) for key in sorted(value)},
            "required": sorted(value),
            "additionalProperties": False,
        }
    if isinstance(value, list):
        items: dict[str, Any] = {}
        for item in value:
            items = merge_schema(items, infer_schema(item))
        return {"type": "array", "items": items}
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, (int, float)):
        # Integer-valued samples cannot establish that fractions are forbidden.
        return {"type": "number"}
    return {"type": "string"}


def merge_schema(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Union observed variants, retaining learned item shapes for empty arrays."""
    if not left:
        return copy.deepcopy(right)
    if not right:
        return copy.deepcopy(left)
    if left == right:
        return copy.deepcopy(left)
    if left.get("type") == right.get("type") == "object":
        properties = copy.deepcopy(left["properties"])
        for key, schema in right["properties"].items():
            properties[key] = merge_schema(properties.get(key, {}), schema)
        return {
            "type": "object",
            "properties": dict(sorted(properties.items())),
            "required": sorted(set(left.get("required", [])) & set(right.get("required", []))),
            "additionalProperties": False,
        }
    if left.get("type") == right.get("type") == "array":
        return {"type": "array", "items": merge_schema(left["items"], right["items"])}
    variants: list[dict[str, Any]] = []
    for schema in (left, right):
        for variant in schema.get("anyOf", [schema]):
            matching = next(
                (i for i, old in enumerate(variants) if old.get("type") == variant.get("type")),
                None,
            )
            if matching is None:
                variants.append(copy.deepcopy(variant))
            else:
                variants[matching] = merge_schema(variants[matching], variant)
    return {"anyOf": sorted(variants, key=lambda item: str(item.get("type", "")))}


def comparison_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Do not declare a type change when the baseline only observed null."""
    if schema == {"type": "null"}:
        return {}
    result = copy.deepcopy(schema)
    if "properties" in result:
        result["properties"] = {k: comparison_schema(v) for k, v in result["properties"].items()}
    if "items" in result:
        result["items"] = comparison_schema(result["items"])
    if "anyOf" in result:
        # A null alternative is known when a concrete non-null type was also
        # observed. Replacing it with {} would accept every type in the union.
        result["anyOf"] = [
            v if v == {"type": "null"} else comparison_schema(v) for v in result["anyOf"]
        ]
    return result


def public_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """OpenAPI describes accepted shapes; the observation baseline detects additions."""
    result = comparison_schema(schema)
    if "properties" in result:
        result["additionalProperties"] = True
        result["properties"] = {
            key: public_schema(value) for key, value in result["properties"].items()
        }
    if "items" in result:
        result["items"] = public_schema(result["items"])
    if "anyOf" in result:
        result["anyOf"] = [
            value if value == {"type": "null"} else public_schema(value)
            for value in result["anyOf"]
        ]
    return result


def unknown_locations(schema: dict[str, Any], location: str = "") -> list[str]:
    """Record the limits of empty/null-only observations instead of inventing types."""
    if not schema or schema == {"type": "null"}:
        return [location or "/"]
    unknown = []
    for key, value in schema.get("properties", {}).items():
        unknown.extend(unknown_locations(value, location + "/" + key))
    if "items" in schema:
        unknown.extend(unknown_locations(schema["items"], location + "/*"))
    for value in schema.get("anyOf", []):
        if value != {"type": "null"}:
            unknown.extend(unknown_locations(value, location))
    return sorted(set(unknown))


def violations(schema: dict[str, Any], value: Json) -> list[dict[str, str]]:
    """Return safe locations and rule names, never jsonschema's value-bearing text."""
    errors = Draft202012Validator(schema).iter_errors(value)
    return [
        {
            "location": "/" + "/".join(str(part) for part in error.absolute_path),
            "rule": str(error.validator),
        }
        for error in errors
    ]


@dataclass
class Response:
    status: str
    data: Json = None
    http_status: int | None = None


class ProbeClient:
    """Cookie-authenticated, bounded GETs without redirects or URL logging."""

    def __init__(self, session: dict[str, Any], delay: float = 0.4) -> None:
        parsed = urlsplit(session["base_url"])
        if (
            parsed.scheme != "https"
            or parsed.hostname != HOSTS["student"]
            or parsed.username
            or parsed.password
            or parsed.port
            or not re.fullmatch(r"/[A-Za-z0-9_-]+/?", parsed.path)
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Unexpected session base URL")
        self.session = session
        self.tenant = parsed.path.strip("/")
        self.delay = delay
        self.http: aiohttp.ClientSession | None = None

    def base(self, host: str) -> str:
        return f"https://{HOSTS[host]}/{self.tenant}"

    async def __aenter__(self) -> ProbeClient:
        self.http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, connect=10), cookie_jar=aiohttp.DummyCookieJar()
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self.http:
            await self.http.close()

    async def fetch(
        self,
        host: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        text: bool = False,
        asset: bool = False,
    ) -> Response:
        assert self.http is not None
        url = urljoin(self.base(host) + "/", path)
        parsed = urlsplit(url)
        trusted_asset = (
            asset
            and parsed.hostname
            and (
                parsed.hostname.endswith(".eduvulcan.pl")
                or parsed.hostname.endswith(".vulcan.net.pl")
            )
        )
        if (parsed.hostname != HOSTS[host] and not trusted_asset) or parsed.scheme != "https":
            raise ValueError("Request outside the expected eduVULCAN host")
        headers = {**_BROWSER_HEADERS, "Referer": self.base(host) + "/App"}
        if not asset:
            headers["Cookie"] = "; ".join(
                f"{k}={v}" for k, v in cookies_for_url(self.session, url).items()
            )
        await asyncio.sleep(self.delay)
        try:
            async with self.http.get(
                url, params=params, headers=headers, ssl=_make_ssl_context(), allow_redirects=False
            ) as response:
                status = response.status
                if status == 401 or 300 <= status < 400:
                    return Response("auth_required", http_status=status)
                if status == 403:
                    return Response("forbidden", http_status=status)
                if status != 200:
                    return Response("http_error", http_status=status)
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > (MAX_ASSET_BODY if asset else MAX_BODY):
                        return Response("response_too_large", http_status=status)
                content_type = response.headers.get("Content-Type", "").lower()
                if text:
                    return Response("ok", body.decode("utf-8", errors="replace"), status)
                if "text/html" in content_type:
                    return Response("auth_required", http_status=status)
                if "json" not in content_type:
                    return Response("unexpected_content_type", http_status=status)
                try:
                    data = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    return Response("invalid_json", http_status=status)
                return Response("ok", data, status)
        except (aiohttp.ClientError, TimeoutError):
            return Response("connection_error")


class Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.sources.extend(value for key, value in attrs if key == "src" and value)


def literal_routes(source: str) -> set[str]:
    """Read candidates from plain or escaped JavaScript; do not execute code."""
    source = re.sub(
        r"\\(?:x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4}))",
        lambda match: chr(int(match[1] or match[2], 16)),
        source,
    ).replace(r"\/", "/")
    routes = {
        "/api/" + match[1]
        for match in re.finditer(
            r"(?:/api/|\bapi/)([A-Za-z][A-Za-z0-9]*)(?![A-Za-z0-9_/{}-])", source
        )
    }
    calls = r"(?:\.(?:get|post|put|patch|delete)|\[\s*[\"'](?:get|post|put|patch|delete)[\"']\s*\])"
    routes.update(
        "/api/" + match[1]
        for match in re.finditer(calls + r"\(\s*[\"']/?([A-Z][A-Za-z0-9]*)[\"']", source)
    )
    return routes


def script_references(source: str) -> list[str]:
    """Find literal imports/chunks; computed asset names need runtime discovery."""
    # Bare strings such as "./adapters/xhr.js" inside a bundled dependency are
    # module names, not downloadable chunks. Only follow actual load syntax.
    loader = (
        r"(?:\bimport\s*\(\s*|\b(?:import|export)\s+(?:[^;()]{0,200}?\bfrom\s*)?"
        r"|\.src\s*=\s*|\b(?:importScripts|getScript)\s*\(\s*)"
    )
    return re.findall(loader + r"[\"']([^\"'\s]+\.m?js(?:\?[^\"'\s]*)?)[\"']", source)


def trusted_script(url: str) -> bool:
    parsed = urlsplit(url)
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and (
            parsed.hostname.endswith(".eduvulcan.pl") or parsed.hostname.endswith(".vulcan.net.pl")
        )
        and parsed.path.endswith((".js", ".mjs"))
    )


async def discover(client: ProbeClient) -> tuple[dict[str, set[str]], list[dict[str, Any]]]:
    """Find literal API route references; candidates are never automatically called."""
    routes: dict[str, set[str]] = {}
    outcomes: list[dict[str, Any]] = []
    for host in HOSTS:
        app = await client.fetch(host, client.base(host) + "/App", text=True)
        if app.status != "ok":
            outcomes.append({"host": host, "status": app.status})
            continue
        parser = Scripts()
        parser.feed(app.data)
        for path in literal_routes(app.data):
            routes.setdefault(path, set()).add(host)
        queue = [urljoin(client.base(host) + "/App", src) for src in parser.sources]
        visited: set[str] = set()
        failed = False
        bundles = 0
        while queue and len(visited) < MAX_SCRIPTS:
            url = queue.pop(0)
            if url in visited or not trusted_script(url):
                continue
            visited.add(url)
            script = await client.fetch(host, url, text=True, asset=True)
            if script.status == "ok":
                bundles += 1
                for path in literal_routes(script.data):
                    routes.setdefault(path, set()).add(host)
                queue.extend(urljoin(url, src) for src in script_references(script.data))
            else:
                failed = True
        truncated = any(url not in visited and trusted_script(url) for url in queue)
        outcomes.append(
            {
                "host": host,
                "status": "partial" if failed or not bundles or truncated else "ok",
                "scripts": bundles,
                "budget_exhausted": truncated,
            }
        )
    return routes, outcomes


def browser_view(base: str, href: str) -> str | None:
    """Return only the public module name; strip tenant and encoded student keys."""
    parsed = urlsplit(urljoin(base + "/App", href))
    root = urlsplit(base + "/App")
    if parsed.scheme != "https" or parsed.netloc != root.netloc or parsed.query:
        return None
    if parsed.fragment:
        if parsed.path != root.path:
            return None
        parts = parsed.fragment.lstrip("/").split("/")
    elif parsed.path.startswith(root.path + "/"):
        parts = parsed.path[len(root.path) + 1 :].strip("/").split("/")
        if root.hostname == HOSTS["student"] and len(parts) > 1 and parts[0] != "szkola":
            # Student app routes are /App/{encoded-key}/{module}.
            parts = parts[1:]
    else:
        return None
    if not parts or len(parts) > 3 or any(not VIEW_PART.fullmatch(part) for part in parts):
        return None
    if any(part in UNSAFE_VIEWS for part in parts):
        return None
    return "/" + "/".join(parts)


async def walk_browser_views(
    page: Any,
    base: str,
    host: str,
    *,
    max_views: int,
    max_tabs: int,
    drain: Any = None,
) -> list[dict[str, Any]]:
    """Follow navigation links and role=tab controls, never rows/action buttons."""
    from playwright.async_api import Error as BrowserError

    outcomes: list[dict[str, Any]] = []
    queue: list[tuple[str, str]] = []
    seen: set[str] = set()

    async def links() -> None:
        hrefs = await page.locator("nav a[href], [role=navigation] a[href]").evaluate_all(
            "els => els.map(el => el.getAttribute('href'))"
        )
        for href in hrefs:
            view = browser_view(base, href)
            if view and view not in seen:
                seen.add(view)
                queue.append((view, href))

    async def settle() -> None:
        await page.wait_for_load_state("networkidle", timeout=20000)
        await page.wait_for_timeout(2000)
        if drain:
            await drain()

    await page.locator("nav a[href], [role=navigation] a[href]").first.wait_for(timeout=15000)
    await links()
    visited = 0
    while queue and visited < max_views:
        view, href = queue.pop(0)
        visited += 1
        entry: dict[str, Any] = {"host": host, "view": view, "status": "ok", "tabs": 0}
        stage = "navigation"
        try:
            if drain:
                await drain()
            # Use the actual menu link so React preserves the selected student
            # and filter state; full page loads unnecessarily reset both.
            anchors = page.locator("nav a[href], [role=navigation] a[href]")
            target = None
            for index in range(await anchors.count()):
                anchor = anchors.nth(index)
                if await anchor.get_attribute("href") == href:
                    target = anchor
                    break
            if target is not None and await target.is_visible():
                await target.click(timeout=10000)
            else:
                # Collapsed school submenus keep links in the DOM but hide them.
                # The collected URL already includes the selected profile key.
                await page.goto(
                    urljoin(base + "/App", href), wait_until="networkidle", timeout=30000
                )
                await page.locator("nav a[href], [role=navigation] a[href]").first.wait_for(
                    timeout=15000
                )
                await page.wait_for_timeout(5000)
            await settle()
            stage = "tabs"
            tabs = page.get_by_role("tab")
            count = await tabs.count()
            for index in range(min(count, max_tabs)):
                tab = tabs.nth(index)
                if await tab.is_disabled():
                    entry["status"] = "partial_disabled_tab"
                    continue
                if await tab.get_attribute("aria-selected") != "true":
                    await tab.click(timeout=10000)
                    await settle()
                entry["tabs"] += 1
            if count > max_tabs:
                entry["status"] = "partial_tab_budget"
            await links()
        except BrowserError:
            entry["status"] = "browser_error"
            entry["stage"] = stage
        outcomes.append(entry)
        print(f"Browser discovery {host} {view}: {entry['status']}", flush=True)
    if queue:
        outcomes.append({"host": host, "status": "partial_view_budget", "remaining": len(queue)})
    return outcomes


async def discover_browser(
    client: ProbeClient,
    *,
    max_views: int = 60,
    max_tabs: int = 20,
) -> tuple[
    dict[str, set[str]],
    dict[str, dict[str, list[str]]],
    list[dict[str, Any]],
    dict[str, dict[str, list[Json]]],
]:
    """Observe runtime route metadata when the provider obfuscates its bundles.

    Follow visible navigation and tabs for the active browser profile on both
    hosts. Block non-GET API requests and side-effecting GETs, including message
    bodies. Do not enter credentials, click rows/action buttons or save storage.
    Lazy-loaded scripts and JSON bodies stay in memory until sanitized.
    """
    from playwright.async_api import Error as BrowserError
    from playwright.async_api import Request, Route, async_playwright
    from playwright.async_api import Response as BrowserResponse

    from vulcan_notify.auth import _browser_headless

    routes: dict[str, set[str]] = {}
    metadata: dict[str, dict[str, list[str]]] = {}
    outcomes: list[dict[str, Any]] = []
    samples: dict[str, dict[str, list[Json]]] = {}
    sample_shapes: dict[tuple[str, str], set[str]] = {}
    pending: set[asyncio.Task[None]] = set()
    blocked_requests: set[Request] = set()
    active_requests: set[Request] = set()

    async def drain() -> None:
        deadline = asyncio.get_running_loop().time() + 30
        while pending or active_requests:
            if pending:
                await asyncio.gather(*list(pending))
            else:
                await asyncio.sleep(0.1)
            if asyncio.get_running_loop().time() >= deadline:
                outcomes.append({"host": "browser", "status": "response_capture_timeout"})
                break

    async def capture(response: BrowserResponse) -> None:
        request = response.request
        parsed = urlsplit(request.url)
        host = next((key for key, name in HOSTS.items() if parsed.hostname == name), None)
        if request.resource_type == "script" and trusted_script(request.url):
            try:
                source_host = urlsplit(request.frame.url).hostname
            except BrowserError:
                return
            owner = next((key for key, name in HOSTS.items() if source_host == name), None)
            if owner and response.status == 200:
                try:
                    body = await asyncio.wait_for(response.body(), timeout=10)
                    if len(body) <= MAX_ASSET_BODY:
                        for path in literal_routes(body.decode("utf-8", errors="replace")):
                            routes.setdefault(path, set()).add(owner)
                except (BrowserError, TimeoutError):
                    outcomes.append({"host": owner, "status": "script_capture_failed"})
            return
        if host is None or request.method != "GET" or "/api/" not in parsed.path:
            return
        path = "/api/" + parsed.path.split("/api/", 1)[1]
        if not ROUTE.fullmatch(path):
            return
        if response.status != 200:
            outcomes.append(
                {
                    "host": host,
                    "endpoint": path,
                    "status": "http_error",
                    "http_status": response.status,
                }
            )
            return
        if "json" not in response.headers.get("content-type", "").lower():
            outcomes.append({"host": host, "endpoint": path, "status": "non_json_response"})
            return
        try:
            body = await asyncio.wait_for(response.body(), timeout=10)
            if len(body) <= MAX_BODY:
                value = json.loads(body)
                shape = json.dumps(infer_schema(value), sort_keys=True)
                known = sample_shapes.setdefault((path, host), set())
                if shape not in known:
                    known.add(shape)
                    samples.setdefault(path, {}).setdefault(host, []).append(value)
            else:
                outcomes.append({"host": host, "endpoint": path, "status": "response_too_large"})
        except (BrowserError, ValueError, TimeoutError):
            outcomes.append({"host": host, "endpoint": path, "status": "response_capture_failed"})

    def schedule(response: BrowserResponse) -> None:
        task = asyncio.create_task(capture(response))
        pending.add(task)
        task.add_done_callback(pending.discard)

    def record(request: Request) -> None:
        parsed = urlsplit(request.url)
        host = next((key for key, name in HOSTS.items() if parsed.hostname == name), None)
        if host is None or "/api/" not in parsed.path:
            return
        path = "/api/" + parsed.path.split("/api/", 1)[1]
        if not ROUTE.fullmatch(path):
            outcomes.append({"host": host, "status": "unsupported_api_path"})
            return
        if request.method == "GET":
            active_requests.add(request)
        routes.setdefault(path, set()).add(host)
        entry = metadata.setdefault(path, {"methods": [], "query-parameters": []})
        entry["methods"] = sorted(set(entry["methods"]) | {request.method.lower()})
        names = {name for name, _ in parse_qsl(parsed.query) if FIELD.fullmatch(name)}
        entry["query-parameters"] = sorted(set(entry["query-parameters"]) | names)

    def failed(request: Request) -> None:
        active_requests.discard(request)
        parsed = urlsplit(request.url)
        host = next((key for key, name in HOSTS.items() if parsed.hostname == name), None)
        if (
            host
            and "/api/" in parsed.path
            and request.method == "GET"
            and request not in blocked_requests
        ):
            path = "/api/" + parsed.path.split("/api/", 1)[1]
            if ROUTE.fullmatch(path):
                outcomes.append({"host": host, "endpoint": path, "status": "request_failed"})

    async def guard(route: Route) -> None:
        request = route.request
        parsed = urlsplit(request.url)
        if (
            parsed.hostname in HOSTS.values()
            and "/api/" in parsed.path
            and (
                request.method not in {"GET", "HEAD", "OPTIONS"}
                or (UNSAFE_GET.search(parsed.path) and not parsed.path.endswith("/api/Usuniete"))
            )
        ):
            # Record attempted methods even when the request is blocked.
            record(request)
            blocked_requests.add(request)
            path = "/api/" + parsed.path.split("/api/", 1)[1]
            if path in metadata:
                entry = metadata[path]
                entry["blocked-methods"] = sorted(
                    set(entry.get("blocked-methods", [])) | {request.method.lower()}
                )
            await route.abort()
            return
        await route.continue_()

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=_browser_headless(), args=["--no-sandbox", "--disable-gpu"]
            )
            try:
                context = await browser.new_context(locale="pl-PL", service_workers="block")
                await context.add_cookies(client.session.get("cookies", []))
                await context.route("**/*", guard)
                context.on("request", record)
                context.on("response", schedule)
                context.on("requestfailed", failed)
                context.on("requestfinished", lambda request: active_requests.discard(request))
                for host in HOSTS:
                    page = await context.new_page()
                    try:
                        await page.goto(
                            client.base(host) + "/App", wait_until="networkidle", timeout=30000
                        )
                        # The obfuscated SPA starts some API calls on timers after
                        # its script downloads have gone idle. Observe a bounded
                        # window rather than closing before initialization.
                        await page.wait_for_timeout(5000)
                        seen = any(host in hosts for hosts in routes.values())
                        outcomes.append({"host": host, "status": "ok" if seen else "partial"})
                        outcomes.extend(
                            await walk_browser_views(
                                page,
                                client.base(host),
                                host,
                                max_views=max_views,
                                max_tabs=max_tabs,
                                drain=drain,
                            )
                        )
                        # Finish response capture before destroying this page.
                        await drain()
                    except BrowserError:
                        outcomes.append({"host": host, "status": "browser_error"})
                    finally:
                        await page.close()
                if pending:
                    await asyncio.gather(*pending)
            finally:
                await browser.close()
    except BrowserError:
        outcomes.append({"host": "browser", "status": "browser_unavailable"})
    return routes, metadata, outcomes, samples


def register_browser_gets(
    document: dict[str, Any],
    metadata: dict[str, dict[str, list[str]]],
    samples: dict[str, dict[str, list[Json]]],
    routes: dict[str, set[str]] | None = None,
) -> None:
    """Document observed GET methods, including unsampled/blocked candidates."""
    for path, request in metadata.items():
        if "get" not in request["methods"]:
            continue
        by_host = samples.get(path, {})
        item = document["paths"].setdefault(path, {})
        if "get" in item:
            continue
        hosts = set(by_host) or (routes or {}).get(path, {"student"})
        host = "student" if "student" in hosts else "messages"
        names = metadata[path]["query-parameters"]
        item["get"] = {
            "operationId": path.rsplit("/", 1)[1],
            "summary": path.rsplit("/", 1)[1],
            "description": "Browser-observed GET; autonomous probing requires review.",
            "x-host": host,
            "x-probe": {"enabled": False, "scope": "student" if "key" in names else "account"},
            "parameters": [
                {"name": name, "in": "query", "required": False, "schema": {"type": "string"}}
                for name in names
            ],
            "responses": {
                "200": {
                    "description": "Response format and structure not yet verified.",
                }
            },
        }
        if by_host:
            item["get"]["responses"]["200"] = {
                "description": "Observed JSON response.",
                "content": {"application/json": {"schema": {}}},
            }
        else:
            item["get"]["x-evidence"] = {"source": "browser-request-only"}
        if host == "messages":
            item["get"]["servers"] = [
                {
                    "url": "https://wiadomosci.eduvulcan.pl/{tenant}",
                    "variables": {"tenant": {"default": "exampledistrict"}},
                }
            ]


def date_params(days: int) -> dict[str, str]:
    today = datetime.now(ZoneInfo("Europe/Warsaw")).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start, end = today - timedelta(days=days), today + timedelta(days=14)
    return {
        "dataOd": start.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "dataDo": end.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def requests_for(
    operation: dict[str, Any],
    students: list[dict[str, Any]],
    cache: dict[tuple[str, int], Json],
    dates: dict[str, str],
    limit: int,
) -> list[tuple[int, dict[str, Any]]]:
    probe = operation["x-probe"]
    scopes = list(enumerate(students, 1)) if probe["scope"] == "student" else [(0, {})]
    requests = []
    for index, student in scopes:
        params = dict(probe.get("constants", {}))
        if probe["scope"] == "student":
            params["key"] = student["key"]
        if probe.get("diary"):
            params["idDziennik"] = student["idDziennik"]
        if probe.get("dates"):
            params.update(dates)
        dependency = probe.get("dependency")
        if dependency:
            data = cache.get((dependency["path"], index))
            if not isinstance(data, list):
                continue
            if dependency.get("read_only"):
                data = [
                    item
                    for item in data
                    if isinstance(item, dict) and item.get("przeczytana") is True
                ]
            for item in data[:limit]:
                if isinstance(item, dict) and dependency["field"] in item:
                    requests.append(
                        (index, {**params, dependency["parameter"]: item[dependency["field"]]})
                    )
        else:
            requests.append((index, params))
    return requests


async def probe_all(
    client: ProbeClient,
    document: dict[str, Any],
    *,
    days: int = 30,
    limit: int = 3,
) -> tuple[list[dict[str, Any]], dict[str, list[Json]]]:
    results: list[dict[str, Any]] = []
    samples: dict[str, list[Json]] = {}
    cache: dict[tuple[str, int], Json] = {}
    context = await client.fetch("student", client.base("student") + "/api/Context")
    operation = document["paths"]["/api/Context"]["get"]
    context_errors = (
        violations(response_schema(operation), context.data) if context.status == "ok" else []
    )
    if context.status != "ok" or context_errors:
        results.append(
            {
                "endpoint": "/api/Context",
                "status": context.status if not context_errors else "contract_changed",
                "http_status": context.http_status,
                "violations": context_errors,
            }
        )
        return results, samples
    students = [s for s in context.data["uczniowie"] if s.get("aktywny", True)]
    samples["/api/Context"] = [context.data]
    results.append({"endpoint": "/api/Context", "status": "ok", "scope": 0})
    dates = date_params(days)
    # Explicit order establishes dependencies regardless of YAML serialization.
    operations = sorted(
        (
            (path, item["get"])
            for path, item in document["paths"].items()
            if "get" in item and path != "/api/Context"
        ),
        key=lambda pair: (bool(pair[1].get("x-probe", {}).get("dependency")), pair[0]),
    )
    for path, operation in operations:
        if "x-probe" not in operation or not operation["x-probe"].get("enabled", False):
            results.append({"endpoint": path, "status": "skipped_unreviewed"})
            continue
        requests = requests_for(operation, students, cache, dates, limit)
        if not requests:
            results.append({"endpoint": path, "status": "skipped_no_dependency"})
        for index, params in requests:
            host = operation["x-host"]
            response = await client.fetch(host, client.base(host) + path, params)
            result: dict[str, Any] = {
                "endpoint": path,
                "scope": index,
                "status": response.status,
                "http_status": response.http_status,
            }
            if response.status == "ok":
                cache[(path, index)] = response.data
                samples.setdefault(path, []).append(response.data)
                result["empty"] = response.data == [] or response.data == {}
                errors = violations(response_schema(operation), response.data)
                if errors:
                    result.update(status="contract_changed", violations=errors)
            results.append(result)
    for path, item in document["paths"].items():
        if "get" not in item:
            results.append({"endpoint": path, "status": "skipped_unreviewed"})
    return results, samples


def check_samples(document: dict[str, Any], samples: dict[str, list[Json]]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for path, values in samples.items():
        operation = document["paths"][path]["get"]
        baseline = operation.get("x-observed-schema")
        if baseline is None:
            changes.append({"endpoint": path, "status": "unbaselined"})
            continue
        for value in values:
            errors = violations(comparison_schema(baseline), value)
            if errors:
                changes.append(
                    {"endpoint": path, "status": "structure_changed", "violations": errors}
                )
    return changes


def write_gather(
    document: dict[str, Any],
    samples: dict[str, list[Json]],
    routes: dict[str, set[str]],
    spec: Path,
    fixtures: Path,
) -> None:
    """Gather is an explicit baseline update; check never calls this function."""
    for path, hosts in sorted(routes.items()):
        item = document["paths"].setdefault(
            path,
            {
                "description": (
                    "Frontend route candidate; HTTP method and request contract unverified."
                ),
            },
        )
        item["x-discovery-hosts"] = sorted(set(item.get("x-discovery-hosts", [])) | hosts)
    fixtures.mkdir(parents=True, exist_ok=True)
    for path, values in samples.items():
        operation = document["paths"][path]["get"]
        observed: dict[str, Any] = {}
        # Work on sanitized data so schema generation cannot publish dynamic keys.
        safe_values = [sanitize(value) for value in values]
        # Infer from every row, not only the bounded fixture sample.
        for value in values:
            observed = merge_schema(observed, infer_schema(value))
        # An empty collection must not erase a previously learned item schema.
        previous = operation.get("x-observed-schema", {})
        if previous:
            observed = merge_schema(previous, observed)
        operation["x-observed-schema"] = observed
        operation["responses"].setdefault("200", {}).setdefault("content", {}).setdefault(
            "application/json", {}
        )["schema"] = public_schema(merge_schema(response_schema(operation), observed))
        operation["x-evidence"] = {
            "source": "live-session",
            "observed-at": datetime.now(UTC).isoformat(),
            "samples": len(values),
        }
        # Never overwrite useful nonempty fixtures with an empty-only observation.
        filename = fixtures / (operation["operationId"] + ".json")
        if filename.exists() and all(value == [] or value == {} for value in safe_values):
            continue
        filename.write_text(
            json.dumps(
                {"endpoint": path, "source": "sanitized-live", "responses": safe_values},
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    spec.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")


async def run_job(args: argparse.Namespace) -> int:
    document = load_contract(args.spec)
    session = load_session(args.session)
    async with ProbeClient(session) as client:
        results, samples = await probe_all(client, document, days=args.days, limit=args.samples)
        routes: dict[str, set[str]] = {}
        discovery: list[dict[str, Any]] = []
        request_metadata: dict[str, dict[str, list[str]]] = {}
        browser_samples: dict[str, dict[str, list[Json]]] = {}
        if samples and not args.no_discover:
            routes, discovery = await discover(client)
            if args.browser_discover:
                (
                    browser_routes,
                    request_metadata,
                    browser_outcomes,
                    browser_samples,
                ) = await discover_browser(
                    client,
                    max_views=getattr(args, "browser_max_views", 60),
                    max_tabs=getattr(args, "browser_max_tabs", 20),
                )
                for path, hosts in browser_routes.items():
                    routes.setdefault(path, set()).update(hosts)
                discovery.extend(browser_outcomes)
    new_routes = sorted(set(routes) - set(document["paths"]))
    if args.command == "api-gather":
        register_browser_gets(document, request_metadata, browser_samples, routes)
    for path, metadata in request_metadata.items():
        if (
            "get" in metadata["methods"]
            and path not in browser_samples
            and not any(result["endpoint"] == path for result in results)
        ):
            results.append({"endpoint": path, "status": "skipped_no_response"})
    for path, by_host in browser_samples.items():
        operation = document["paths"].get(path, {}).get("get")
        if operation and operation["x-host"] in by_host:
            values = by_host[operation["x-host"]]
            # The browser can expose additional variants (e.g. status=1 vs 2)
            # even when a standalone probe already sampled this endpoint.
            samples.setdefault(path, []).extend(values)
            errors = [
                error for value in values for error in violations(response_schema(operation), value)
            ]
            for result in results:
                if result["endpoint"] == path and result["status"].startswith("skipped_"):
                    result.update(
                        status="contract_changed" if errors else "ok",
                        source="browser-observation",
                        violations=errors,
                    )
            if errors or not any(result["endpoint"] == path for result in results):
                results.append(
                    {
                        "endpoint": path,
                        "status": "contract_changed" if errors else "ok",
                        "source": "browser-observation",
                        "violations": errors,
                    }
                )
    changes = check_samples(document, samples) if args.command == "api-check" else []
    if args.command == "api-check":
        changes.extend(
            {"endpoint": path, "status": "unbaselined_get"}
            for path in browser_samples
            if "get" not in document["paths"].get(path, {})
        )
    request_changes = (
        [
            {"endpoint": path, "status": "request_metadata_changed"}
            for path, metadata in request_metadata.items()
            if path in document["paths"]
            and document["paths"][path].get("x-browser-request") != metadata
        ]
        if args.command == "api-check"
        else []
    )
    if (
        args.command == "api-check"
        and args.browser_discover
        and not args.no_discover
        and all(outcome["status"] == "ok" for outcome in discovery)
    ):
        request_changes.extend(
            {"endpoint": path, "status": "browser_route_not_observed"}
            for path, item in document["paths"].items()
            if "x-browser-request" in item and path not in request_metadata
        )
    changes.extend(request_changes)
    failures = []
    for result in results:
        if result["status"] == "ok":
            continue
        if args.command == "api-check" and not args.require_complete:
            if result["status"].startswith("skipped_"):
                continue
            operation = document["paths"][result["endpoint"]]["get"]
            previous = operation.get("x-observed-unavailable")
            # Only a recorded 404 is a known unavailable route. Auth failures,
            # rate limits and server faults must always fail the check.
            if (
                result["status"] == "http_error"
                and result.get("http_status") == 404
                and previous == {"status": "http_error", "http_status": 404}
            ):
                continue
        failures.append(result)
    incomplete = any(result["status"] != "ok" for result in results)
    failed_discovery = [result for result in discovery if result["status"] != "ok"]
    report = {
        "command": args.command,
        "checked_at": datetime.now(UTC).isoformat(),
        "results": results,
        "changes": changes,
        "new_routes": new_routes,
        "discovery": discovery,
        "browser_requests": request_metadata,
        "unsampled_gets": sorted(
            path
            for path, item in document["paths"].items()
            if "get" in item and path not in samples
        ),
        "complete": not incomplete and not failed_discovery,
        "discovery_scope": "visible modules and tabs of active browser profile"
        if args.browser_discover
        else "literal frontend references",
        "sampled_endpoints": len(samples),
        "unknown_structure": {
            path: unknown_locations(document["paths"][path]["get"].get("x-observed-schema", {}))
            for path in samples
        },
    }
    code = 1 if failures or failed_discovery else 0
    if changes or (new_routes and args.command == "api-check"):
        code = 1
    if args.command == "api-gather" and samples:
        for path, metadata in request_metadata.items():
            document["paths"].setdefault(
                path,
                {
                    "description": "Browser-observed route; request semantics require review.",
                },
            )["x-browser-request"] = metadata
        for result in results:
            operation = document["paths"][result["endpoint"]].get("get")
            if operation is None:
                continue
            if result["status"] == "http_error" and result.get("http_status") == 404:
                operation["x-observed-unavailable"] = {"status": "http_error", "http_status": 404}
            elif result["status"] == "ok":
                operation.pop("x-observed-unavailable", None)
        # Refuse to bless responses which contradict the reviewed core contract.
        valid_samples = {
            path: values
            for path, values in samples.items()
            if all(
                not violations(response_schema(document["paths"][path]["get"]), v) for v in values
            )
        }
        write_gather(document, valid_samples, routes, args.spec, args.fixtures)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for result in results + changes:
        print(f"{result['endpoint']}: {result['status']}")
    print(f"Report: {args.report}; new candidates: {len(new_routes)}; exit: {code}")
    if incomplete:
        print("Coverage incomplete: see skipped and unavailable endpoints in the report.")
    if args.command == "api-check" and code == 0:
        print("No contract changes detected in the sampled responses.")
    return code


def main(argv: list[str] | None = None) -> None:
    from vulcan_notify.config import settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["api-gather", "api-check"])
    parser.add_argument("--session", type=Path, default=settings.session_file)
    parser.add_argument("--spec", type=Path, default=Path("docs/eduvulcan/openapi.yaml"))
    parser.add_argument("--fixtures", type=Path, default=Path("tests/fixtures/eduvulcan"))
    parser.add_argument("--report", type=Path, default=Path("data/eduvulcan-contract-report.json"))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument(
        "--samples", type=int, default=3, help="Maximum detail IDs/periods per student"
    )
    discovery_options = parser.add_mutually_exclusive_group()
    discovery_options.add_argument(
        "--no-discover", action="store_true", help="Skip frontend route discovery"
    )
    discovery_options.add_argument(
        "--browser-discover",
        action="store_true",
        help="Also observe runtime routes in a transient cookie-authenticated browser",
    )
    parser.add_argument(
        "--browser-max-views",
        type=int,
        default=60,
        help="Maximum visible modules visited per host (1..200)",
    )
    parser.add_argument(
        "--browser-max-tabs",
        type=int,
        default=20,
        help="Maximum tabs visited per module (1..100)",
    )
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="Fail checks on any skipped or previously unavailable endpoint",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.samples <= 20 or not 1 <= args.days <= 366:
        parser.error("--samples must be 1..20 and --days must be 1..366")
    if not 1 <= args.browser_max_views <= 200 or not 1 <= args.browser_max_tabs <= 100:
        parser.error("--browser-max-views must be 1..200 and --browser-max-tabs must be 1..100")
    try:
        # Serialize the explicit jobs sharing a session. Normal synchronization
        # is independently owned; stop its worker before a manual contract job.
        lock_path = args.session.parent / "eduvulcan-contract.lock"
        with lock_path.open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            code = asyncio.run(run_job(args))
    except (OSError, ValueError, KeyError, yaml.YAMLError) as exc:
        # Exception text can contain private file contents or response values.
        print(f"Contract job could not run ({type(exc).__name__}); check inputs and session.")
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
