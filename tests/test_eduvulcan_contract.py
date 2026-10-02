"""Offline upstream contracts, sanitized fixture replay and on-demand jobs.

These tests never load the household's session or contact eduVULCAN.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest
from openapi_spec_validator import validate

from vulcan_notify import eduvulcan_contract as contract
from vulcan_notify.client import VulcanClient
from vulcan_notify.models import ClassificationPeriod, Student

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs/eduvulcan/openapi.yaml"
FIXTURES = ROOT / "tests/fixtures/eduvulcan"


@pytest.fixture
def document() -> dict:
    return contract.load_contract(SPEC)


def test_openapi_and_documented_inventory(document):
    validate(document)
    documented = re.findall(
        r"^### GET (/api/[^?\s]+)", (ROOT / "docs/eduvulcan-api.md").read_text(), re.M
    )
    assert set(documented) <= set(document["paths"])
    operations = [item["get"] for item in document["paths"].values() if "get" in item]
    assert len({op["operationId"] for op in operations}) == len(operations)
    for operation in operations:
        assert operation["x-host"] in contract.HOSTS
        if "x-observed-schema" in operation:
            contract.Draft202012Validator.check_schema(operation["x-observed-schema"])
        parameters = {p["name"] for p in operation["parameters"]}
        probe = operation["x-probe"]
        expected = set(probe.get("constants", {}))
        if probe["scope"] == "student":
            expected.add("key")
        if probe.get("dates"):
            expected.update(["dataOd", "dataDo"])
        if probe.get("diary"):
            expected.add("idDziennik")
        if probe.get("dependency"):
            expected.add(probe["dependency"]["parameter"])
        if probe.get("enabled", False):
            assert parameters == expected


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_sanitized_fixture_contracts(document, fixture):
    sample = json.loads(fixture.read_text())
    operation = document["paths"][sample["endpoint"]]["get"]
    for response in sample["responses"]:
        assert not contract.violations(contract.response_schema(operation), response)
        if sample["source"] == "sanitized-live":
            assert not contract.violations(
                contract.comparison_schema(operation["x-observed-schema"]), response
            )


PARSERS = {
    "Context": ("get_students", ()),
    "OkresyKlasyfikacyjne": ("get_periods", ("student",)),
    "Oceny": ("get_grades_and_summaries", ("student", "period")),
    "Frekwencja": ("get_attendance", ("student", "start", "end")),
    "SprawdzianyTablica": ("get_exams", ("student",)),
    "ZadaniaDomoweTablica": ("get_homework", ("student",)),
    "SprawdzianSzczegoly": ("get_exam_detail", ("student", 1)),
    "ZadanieDomoweSzczegoly": ("get_homework_detail", ("student", 1)),
    "PlanZajec": ("get_schedule", ("student", "start", "end")),
    "Odebrane": ("get_messages", ()),
    "WiadomoscSzczegoly": ("get_message_detail", ("synthetic-message-key",)),
}


@pytest.mark.parametrize("name", PARSERS)
async def test_real_client_parsers_accept_sanitized_fixtures(name):
    sample = json.loads((FIXTURES / (name + ".json")).read_text())
    values = {
        "student": Student(
            key="SYNTHETIC",
            name="Example",
            class_name="A",
            school="Example",
            diary_id=1,
            mailbox_key="SYNTHETIC",
        ),
        "period": ClassificationPeriod(
            id=1, number=1, date_from="2000-01-01", date_to="2000-06-30"
        ),
    }
    method, arguments = PARSERS[name]
    client = VulcanClient(
        {"base_url": "https://uczen.eduvulcan.pl/example", "tenant": "example", "cookies": []}
    )
    for response in sample["responses"]:
        client._request = AsyncMock(return_value=response)
        client._request_url = AsyncMock(return_value=response)
        result = await getattr(client, method)(*(values.get(arg, arg) for arg in arguments))
        if name == "Context":
            assert len(result) == sum(s.get("aktywny", True) for s in response["uczniowie"])
        elif name == "Oceny":
            grades, summaries = result
            assert len(summaries) == len(response["ocenyPrzedmioty"])
            assert all(grade.column_id for grade in grades)
        elif name == "WiadomoscSzczegoly":
            assert result == response.get("tresc")
        elif isinstance(response, list):
            assert len(result) == len(response)
        elif name == "Frekwencja":
            assert len(result) == len(response["oddzialy"])
        else:
            assert result == response


def test_structural_changes_ignore_content_but_detect_fields_and_types():
    baseline = contract.infer_schema({"items": [{"id": 1, "text": "old"}]})
    assert not contract.violations(baseline, {"items": [{"id": 99, "text": "new"}]})
    for changed in [
        {"items": [{"id": 1, "text": "new", "added": True}]},
        {"items": [{"text": "new"}]},
        {"items": [{"id": "1", "text": "new"}]},
    ]:
        assert contract.violations(baseline, changed)
    assert not contract.violations(baseline, {"items": []})


def test_union_optional_null_and_empty_array_evidence():
    first = contract.infer_schema([{"id": 1, "description": None}])
    second = contract.infer_schema([{"id": 2, "description": "text", "optional": True}])
    union = contract.merge_schema(first, second)
    assert not contract.violations(union, [{"id": 3, "description": None}])
    assert not contract.violations(union, [{"id": 3, "description": "text", "optional": False}])
    assert contract.merge_schema(union, contract.infer_schema([])) == union
    assert contract.comparison_schema({"type": "null"}) == {}
    for schema in [contract.comparison_schema(union), contract.public_schema(union)]:
        assert not contract.violations(schema, [{"id": 3, "description": None}])
        assert contract.violations(schema, [{"id": 3, "description": 17}])


def test_sanitizer_removes_every_scalar_secret_and_rejects_dynamic_keys():
    private = {
        "name": "PRIVATE_NAME",
        "tresc": "PRIVATE_BODY",
        "key": "PRIVATE_TOKEN",
        "id": 123456,
        "data": "2026-10-02T12:30:00+02:00",
        "weight": 2.5,
        "active": True,
        "nested": ["PRIVATE_ADDRESS"],
        "nullable": None,
    }
    safe = json.dumps(contract.sanitize(private))
    for value in ["PRIVATE", "123456", "2026-10-02", "2.5"]:
        assert value not in safe
    assert contract.infer_schema(contract.sanitize(private)) == contract.infer_schema(private)
    with pytest.raises(ValueError, match="Dynamic"):
        contract.sanitize({"person@example.com": "value"})


def test_reports_do_not_contain_response_values():
    failures = contract.violations(contract.infer_schema({"value": 1}), {"value": "PRIVATE"})
    assert failures == [{"location": "/value", "rule": "type"}]
    assert "PRIVATE" not in json.dumps(failures)


def test_gather_retains_all_row_shapes_and_does_not_probe_candidates(document, tmp_path):
    samples = {
        "/api/SprawdzianyTablica": [
            [
                {
                    "id": i,
                    "data": "2000-01-03T00:00:00+01:00",
                    "przedmiot": "PRIVATE",
                    "rodzaj": 1,
                    **({"optional": True} if i == 4 else {}),
                }
                for i in range(1, 5)
            ]
        ]
    }
    spec, fixtures = tmp_path / "openapi.yaml", tmp_path / "fixtures"
    contract.write_gather(document, samples, {"/api/NewCandidate": {"student"}}, spec, fixtures)
    gathered = contract.load_contract(spec)
    assert "get" not in gathered["paths"]["/api/NewCandidate"]
    observed = gathered["paths"]["/api/SprawdzianyTablica"]["get"]["x-observed-schema"]
    assert "optional" in observed["items"]["properties"]
    assert "PRIVATE" not in spec.read_text()
    assert "PRIVATE" not in (fixtures / "SprawdzianyTablica.json").read_text()


def test_dependency_ids_are_scoped_to_each_student_and_only_read_messages(document):
    students = [{"key": "FIRST", "idDziennik": 1}, {"key": "SECOND", "idDziennik": 2}]
    operation = document["paths"]["/api/Oceny"]["get"]
    cache = {
        ("/api/OkresyKlasyfikacyjne", 1): [{"id": 11}],
        ("/api/OkresyKlasyfikacyjne", 2): [{"id": 22}],
    }
    requests = contract.requests_for(operation, students, cache, {}, 3)
    assert requests == [
        (1, {"key": "FIRST", "idOkresKlasyfikacyjny": 11}),
        (2, {"key": "SECOND", "idOkresKlasyfikacyjny": 22}),
    ]
    operation = document["paths"]["/api/WiadomoscSzczegoly"]["get"]
    cache = {
        ("/api/Odebrane", 0): [
            {"apiGlobalKey": "UNREAD", "przeczytana": False},
            {"apiGlobalKey": "READ", "przeczytana": True},
        ]
    }
    assert contract.requests_for(operation, students, cache, {}, 3) == [
        (0, {"apiGlobalKey": "READ"})
    ]


def test_combined_exam_homework_probe_has_observed_date_parameters(document):
    operation = document["paths"]["/api/SprawdzianyZadaniaDomowe"]["get"]
    dates = {"dataOd": "2000-01-01T00:00:00Z", "dataDo": "2000-02-01T00:00:00Z"}
    assert contract.requests_for(operation, [{"key": "SYNTHETIC"}], {}, dates, 3) == [
        (1, {"key": "SYNTHETIC", **dates})
    ]


class FakeProbe:
    responses: ClassVar[dict[str, contract.Response]] = {}
    calls: ClassVar[list[tuple[str, dict | None]]] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def base(self, host):
        return "https://" + contract.HOSTS[host] + "/example"

    async def fetch(self, host, path, params=None, **kwargs):
        route = "/api/" + path.split("/api/")[1]
        self.calls.append((route, params))
        return self.responses.get(route, contract.Response("http_error", http_status=500))


@pytest.fixture
def job_args(tmp_path, document, monkeypatch):
    document["paths"] = {path: document["paths"][path] for path in ["/api/Context", "/api/Uwagi"]}
    for path, value in [
        (
            "/api/Context",
            {
                "uczniowie": [
                    {
                        "key": "FIRST",
                        "uczen": "Example",
                        "oddzial": "A",
                        "jednostka": "Example",
                        "idDziennik": 1,
                    }
                ]
            },
        ),
        ("/api/Uwagi", [{"id": 1, "text": "old"}]),
    ]:
        document["paths"][path]["get"]["x-observed-schema"] = contract.infer_schema(value)
        document["paths"][path]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ] = contract.public_schema(contract.infer_schema(value))
    spec = tmp_path / "openapi.yaml"
    spec.write_text(contract.yaml.safe_dump(document, sort_keys=False))
    session = tmp_path / "test.session.json"
    session.write_text('{"base_url":"https://uczen.eduvulcan.pl/example","cookies":[]}')
    FakeProbe.responses = {
        "/api/Context": contract.Response(
            "ok",
            {
                "uczniowie": [
                    {
                        "key": "SECOND",
                        "uczen": "Changed",
                        "oddzial": "B",
                        "jednostka": "Changed",
                        "idDziennik": 2,
                    }
                ]
            },
            200,
        ),
        "/api/Uwagi": contract.Response("ok", [{"id": 2, "text": "new"}], 200),
    }
    FakeProbe.calls = []
    monkeypatch.setattr(contract, "ProbeClient", FakeProbe)
    return argparse.Namespace(
        command="api-check",
        spec=spec,
        session=session,
        fixtures=tmp_path / "fixtures",
        report=tmp_path / "report.json",
        days=30,
        samples=3,
        no_discover=True,
        browser_discover=False,
        require_complete=False,
    )


async def test_check_is_read_only_and_content_changes_pass(job_args):
    original = job_args.spec.read_bytes()
    assert await contract.run_job(job_args) == 0
    assert job_args.spec.read_bytes() == original
    assert not job_args.fixtures.exists()
    assert json.loads(job_args.report.read_text())["changes"] == []


async def test_check_detects_schema_drift_without_overwriting_baseline(job_args):
    original = job_args.spec.read_bytes()
    FakeProbe.responses["/api/Uwagi"] = contract.Response(
        "ok", [{"id": "moved", "text": "new"}], 200
    )
    assert await contract.run_job(job_args) == 1
    assert job_args.spec.read_bytes() == original
    assert json.loads(job_args.report.read_text())["changes"][0]["status"] == "structure_changed"


@pytest.mark.parametrize(
    "status,http",
    [("forbidden", 403), ("http_error", 429), ("http_error", 500), ("auth_required", 401)],
)
async def test_auth_server_and_permission_failures_never_pass(job_args, status, http):
    FakeProbe.responses["/api/Uwagi"] = contract.Response(status, http_status=http)
    assert await contract.run_job(job_args) == 1


async def test_known_404_is_reported_as_incomplete_and_strict_check_fails(job_args):
    document = contract.load_contract(job_args.spec)
    document["paths"]["/api/Uwagi"]["get"]["x-observed-unavailable"] = {
        "status": "http_error",
        "http_status": 404,
    }
    job_args.spec.write_text(contract.yaml.safe_dump(document))
    FakeProbe.responses["/api/Uwagi"] = contract.Response("http_error", http_status=404)
    assert await contract.run_job(job_args) == 0
    assert json.loads(job_args.report.read_text())["complete"] is False
    job_args.require_complete = True
    assert await contract.run_job(job_args) == 1


async def test_new_frontend_candidates_fail_check_without_being_called(job_args, monkeypatch):
    job_args.no_discover = False
    monkeypatch.setattr(
        contract, "discover", AsyncMock(return_value=({"/api/New": {"student"}}, []))
    )
    assert await contract.run_job(job_args) == 1
    assert "/api/New" not in [path for path, _ in FakeProbe.calls]


@pytest.mark.parametrize(
    "base",
    [
        "https://evil.test/example",
        "http://uczen.eduvulcan.pl/example",
        "https://uczen.eduvulcan.pl/example?token=private",
    ],
)
def test_unexpected_session_origins_are_rejected(base):
    with pytest.raises(ValueError):
        contract.ProbeClient({"base_url": base, "cookies": []})


async def test_transport_does_not_forward_cookies_or_follow_asset_redirects():
    client = contract.ProbeClient(
        {
            "base_url": "https://uczen.eduvulcan.pl/example",
            "cookies": [
                {"name": "PRIVATE", "value": "TOKEN", "domain": ".vulcan.net.pl"},
            ],
        },
        delay=0,
    )
    response = MagicMock(status=302)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    client.http = MagicMock()
    client.http.get.return_value = context
    result = await client.fetch(
        "student", "https://static.vulcan.net.pl/app.js", text=True, asset=True
    )
    assert result.status == "auth_required"
    kwargs = client.http.get.call_args.kwargs
    assert kwargs["allow_redirects"] is False
    assert "Cookie" not in kwargs["headers"]


async def test_browser_discovery_records_only_routes_methods_and_parameter_names(monkeypatch):
    import playwright.async_api as browser_api

    context = MagicMock()
    context.add_cookies = AsyncMock()
    context.route = AsyncMock()
    callbacks = {}
    context.on.side_effect = lambda event, callback: callbacks.update({event: callback})

    class Page:
        async def goto(self, url, **kwargs):
            host = "uczen.eduvulcan.pl" if "uczen." in url else "wiadomosci.eduvulcan.pl"
            request = MagicMock(method="GET", resource_type="fetch")
            request.url = f"https://{host}/example/api/New?key=PRIVATE_STUDENT&token=PRIVATE_TOKEN"
            callbacks["request"](request)
            response = MagicMock(
                request=request, status=200, headers={"content-type": "application/json"}
            )
            response.body = AsyncMock(return_value=b'{"value":"PRIVATE_BODY"}')
            callbacks["response"](response)
            callbacks["requestfinished"](request)

        async def close(self):
            pass

        async def wait_for_timeout(self, duration):
            pass

    context.new_page = AsyncMock(side_effect=lambda: Page())
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()
    playwright = MagicMock()
    playwright.chromium.launch = AsyncMock(return_value=browser)
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=playwright)
    manager.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(browser_api, "async_playwright", lambda: manager)
    monkeypatch.setattr(contract, "walk_browser_views", AsyncMock(return_value=[]))
    client = contract.ProbeClient({"base_url": "https://uczen.eduvulcan.pl/example", "cookies": []})
    routes, metadata, outcomes, samples = await contract.discover_browser(client)
    assert routes == {"/api/New": {"student", "messages"}}
    assert metadata == {"/api/New": {"methods": ["get"], "query-parameters": ["key", "token"]}}
    assert all(item["status"] == "ok" for item in outcomes)
    assert "PRIVATE" not in json.dumps(metadata)
    assert samples == {
        "/api/New": {
            "student": [{"value": "PRIVATE_BODY"}],
            "messages": [{"value": "PRIVATE_BODY"}],
        }
    }
    browser.close.assert_awaited_once()
    guard = context.route.call_args.args[1]
    for method, path, blocked in [
        ("POST", "New", True),
        ("DELETE", "New", True),
        ("GET", "WiadomoscSzczegoly", True),
        ("GET", "OznaczPrzeczytana", True),
        ("GET", "RealizacjaZajec13", False),
        ("GET", "Usuniete", False),
    ]:
        request = MagicMock(method=method, resource_type="fetch")
        request.url = f"https://uczen.eduvulcan.pl/example/api/{path}?key=PRIVATE"
        route = MagicMock(request=request)
        route.abort = AsyncMock()
        route.continue_ = AsyncMock()
        await guard(route)
        assert route.abort.await_count == int(blocked)
        assert route.continue_.await_count == int(not blocked)


async def test_browser_request_changes_fail_check_and_gather_stores_metadata(job_args, monkeypatch):
    job_args.no_discover = False
    job_args.browser_discover = True
    monkeypatch.setattr(contract, "discover", AsyncMock(return_value=({}, [])))
    routes = {"/api/Uwagi": {"student"}}
    metadata = {"/api/Uwagi": {"methods": ["get"], "query-parameters": ["key", "newParameter"]}}
    monkeypatch.setattr(
        contract, "discover_browser", AsyncMock(return_value=(routes, metadata, [], {}))
    )
    assert await contract.run_job(job_args) == 1
    report = json.loads(job_args.report.read_text())
    assert report["changes"][0]["status"] == "request_metadata_changed"
    job_args.command = "api-gather"
    assert await contract.run_job(job_args) == 0
    gathered = contract.load_contract(job_args.spec)
    assert gathered["paths"]["/api/Uwagi"]["x-browser-request"] == metadata["/api/Uwagi"]


async def test_previously_observed_browser_route_missing_is_reported(job_args, monkeypatch):
    job_args.no_discover = False
    job_args.browser_discover = True
    document = contract.load_contract(job_args.spec)
    document["paths"]["/api/Uwagi"]["x-browser-request"] = {
        "methods": ["get"],
        "query-parameters": ["key"],
    }
    job_args.spec.write_text(contract.yaml.safe_dump(document))
    monkeypatch.setattr(contract, "discover", AsyncMock(return_value=({}, [])))
    monkeypatch.setattr(contract, "discover_browser", AsyncMock(return_value=({}, {}, [], {})))
    assert await contract.run_job(job_args) == 1
    assert any(
        item["status"] == "browser_route_not_observed"
        for item in json.loads(job_args.report.read_text())["changes"]
    )


def test_browser_gets_have_schemas_and_fixtures_without_enabling_replay(document, tmp_path):
    metadata = {"/api/New": {"methods": ["get", "post"], "query-parameters": ["key"]}}
    browser_samples = {"/api/New": {"student": [{"value": "PRIVATE_BODY"}]}}
    contract.register_browser_gets(document, metadata, browser_samples)
    operation = document["paths"]["/api/New"]["get"]
    assert operation["x-probe"] == {"enabled": False, "scope": "student"}
    spec, fixtures = tmp_path / "spec.yaml", tmp_path / "fixtures"
    contract.write_gather(
        document,
        {"/api/New": browser_samples["/api/New"]["student"]},
        {"/api/New": {"student"}},
        spec,
        fixtures,
    )
    stored = contract.load_contract(spec)["paths"]["/api/New"]["get"]
    assert contract.response_schema(stored)["properties"]["value"] == {"type": "string"}
    assert "PRIVATE_BODY" not in spec.read_text()
    assert "PRIVATE_BODY" not in (fixtures / "New.json").read_text()


def test_literal_routes_cover_escaped_names_and_bracket_methods():
    source = r"""client.get("RealizacjaZajec13"); client["get"]("Uwagi");
    const a="\/api\/Kolekcje", b="\x2fapi\x2fZasoby", c="\u002fapi\u002fTematy";
    client.post("Save"); const computed = prefix + name;"""
    assert contract.literal_routes(source) == {
        "/api/RealizacjaZajec13",
        "/api/Uwagi",
        "/api/Kolekcje",
        "/api/Zasoby",
        "/api/Tematy",
        "/api/Save",
    }


def test_script_references_ignore_bundled_module_names():
    assert contract.script_references(
        'const modules=["index.js","./adapters/xhr.js"]; '
        'import("./lazy.js"); export {x} from "./extra.mjs"; script.src="/loaded.js";'
    ) == ["./lazy.js", "./extra.mjs", "/loaded.js"]


async def test_script_discovery_recurses_imports_and_does_not_fetch_other_origins():
    client = MagicMock()
    client.base.side_effect = lambda host: f"https://{contract.HOSTS[host]}/example"
    calls = []

    async def fetch(host, url, **kwargs):
        calls.append(url)
        if url.endswith("/App"):
            return contract.Response(
                "ok", '<script src="https://static.vulcan.net.pl/main.js"></script>'
            )
        if url.endswith("main.js"):
            return contract.Response(
                "ok", 'import("./lazy.js"); import("https://evil.test/secret.js");'
            )
        return contract.Response("ok", 'api.get("RealizacjaZajec13")')

    client.fetch = fetch
    routes, outcomes = await contract.discover(client)
    assert routes == {"/api/RealizacjaZajec13": {"student", "messages"}}
    assert all(outcome["scripts"] == 2 and outcome["status"] == "ok" for outcome in outcomes)
    assert not any("evil.test" in url for url in calls)


@pytest.mark.parametrize(
    "href, expected",
    [
        ("/example/App/PRIVATE_ENCODED_KEY/realizacjaZajec", "/realizacjaZajec"),
        ("/example/App/privatekey/szkola/informacje", "/szkola/informacje"),
        ("/example/App/szkola/nauczyciele", "/szkola/nauczyciele"),
        ("#/pochwalyUwagi", "/pochwalyUwagi"),
        ("/example/App/PRIVATE/logout", None),
        ("/example/App/PRIVATE/oceny?key=SECRET", None),
        ("https://evil.test/example/App/PRIVATE/oceny", None),
        ("/example/App/PRIVATE/123456", None),
        ("https://uczen.eduvulcan.pl/example", None),
    ],
)
def test_browser_views_strip_profile_keys_and_reject_actions_and_external_links(href, expected):
    assert contract.browser_view("https://uczen.eduvulcan.pl/example", href) == expected


class NavigationPage:
    """Small fake of menu links and tabs, without school content or action buttons."""

    def __init__(self):
        self.hrefs = [
            "/example/App/PRIVATE/realizacjaZajec",
            "/example/App/PRIVATE/pochwalyUwagi",
            "https://evil.test/oceny",
            "/example/App/PRIVATE/logout",
        ]
        self.visits = []
        self.tab_clicks = []
        self.current_tab = 0
        self.anchors = MagicMock()
        self.anchors.first.wait_for = AsyncMock()
        self.anchors.evaluate_all = AsyncMock(return_value=self.hrefs)
        self.anchors.count = AsyncMock(return_value=len(self.hrefs))
        self.anchors.nth.side_effect = self.anchor
        self.tabs = MagicMock()
        self.tabs.count = AsyncMock(return_value=2)
        self.tabs.nth.side_effect = self.tab
        self.wait_for_load_state = AsyncMock()
        self.wait_for_timeout = AsyncMock()

    def locator(self, selector):
        assert selector == "nav a[href], [role=navigation] a[href]"
        return self.anchors

    def get_by_role(self, role):
        assert role == "tab"
        return self.tabs

    def anchor(self, index):
        anchor = MagicMock()
        anchor.get_attribute = AsyncMock(return_value=self.hrefs[index])
        anchor.is_visible = AsyncMock(return_value=True)

        async def click(**kwargs):
            self.visits.append(self.hrefs[index])
            self.current_tab = 0

        anchor.click = click
        return anchor

    def tab(self, index):
        tab = MagicMock()
        tab.is_disabled = AsyncMock(return_value=False)
        tab.get_attribute = AsyncMock(return_value="true" if index == self.current_tab else "false")

        async def click(**kwargs):
            self.tab_clicks.append((self.visits[-1], index))
            self.current_tab = index

        tab.click = click
        return tab


async def test_module_discovery_visits_both_realization_tabs_and_never_action_links():
    page = NavigationPage()
    outcomes = await contract.walk_browser_views(
        page, "https://uczen.eduvulcan.pl/example", "student", max_views=60, max_tabs=20
    )
    assert len(page.visits) == 2
    assert [item["view"] for item in outcomes] == ["/realizacjaZajec", "/pochwalyUwagi"]
    assert all(item["tabs"] == 2 and item["status"] == "ok" for item in outcomes)
    assert len(page.tab_clicks) == 2
    assert "PRIVATE" not in json.dumps(outcomes)


async def test_module_and_tab_budgets_report_incomplete_coverage():
    page = NavigationPage()
    outcomes = await contract.walk_browser_views(
        page, "https://uczen.eduvulcan.pl/example", "student", max_views=1, max_tabs=1
    )
    assert outcomes[0]["status"] == "partial_tab_budget"
    assert outcomes[1]["status"] == "partial_view_budget"


async def test_hidden_submenu_falls_back_to_its_url_and_drains_before_navigation():
    page = NavigationPage()
    original = page.anchor

    def anchor(index):
        value = original(index)
        value.is_visible = AsyncMock(return_value=False)
        return value

    page.anchors.nth.side_effect = anchor
    drain = AsyncMock()

    async def goto(url, **kwargs):
        assert drain.await_count > 0
        page.visits.append(url)
        page.current_tab = 0

    page.goto = goto
    outcomes = await contract.walk_browser_views(
        page,
        "https://uczen.eduvulcan.pl/example",
        "student",
        max_views=60,
        max_tabs=20,
        drain=drain,
    )
    assert all(item["status"] == "ok" for item in outcomes)
    assert len(page.visits) == 2


async def test_request_only_get_is_recorded_without_inventing_response_and_later_gather_fills_it(
    job_args,
    monkeypatch,
):
    job_args.no_discover = False
    job_args.browser_discover = True
    job_args.command = "api-gather"
    routes = {"/api/NewGet": {"messages"}}
    metadata = {"/api/NewGet": {"methods": ["get"], "query-parameters": []}}
    monkeypatch.setattr(contract, "discover", AsyncMock(return_value=({}, [])))
    monkeypatch.setattr(
        contract, "discover_browser", AsyncMock(return_value=(routes, metadata, [], {}))
    )
    assert await contract.run_job(job_args) == 1
    operation = contract.load_contract(job_args.spec)["paths"]["/api/NewGet"]["get"]
    assert operation["x-host"] == "messages"
    assert operation["x-evidence"]["source"] == "browser-request-only"
    assert "content" not in operation["responses"]["200"]
    assert not (job_args.fixtures / "NewGet.json").exists()
    monkeypatch.setattr(
        contract,
        "discover_browser",
        AsyncMock(
            return_value=(
                routes,
                metadata,
                [],
                {"/api/NewGet": {"messages": [{"value": "PRIVATE"}]}},
            )
        ),
    )
    job_args.command = "api-check"
    assert await contract.run_job(job_args) == 1
    assert json.loads(job_args.report.read_text())["changes"][0]["status"] == "unbaselined"
    job_args.command = "api-gather"
    assert await contract.run_job(job_args) == 0
    operation = contract.load_contract(job_args.spec)["paths"]["/api/NewGet"]["get"]
    assert contract.response_schema(operation)["properties"]["value"] == {"type": "string"}
    assert "PRIVATE" not in (job_args.fixtures / "NewGet.json").read_text()


async def test_browser_variants_are_checked_even_when_endpoint_was_probed(job_args, monkeypatch):
    job_args.no_discover = False
    job_args.browser_discover = True
    monkeypatch.setattr(contract, "discover", AsyncMock(return_value=({}, [])))
    monkeypatch.setattr(
        contract,
        "discover_browser",
        AsyncMock(
            return_value=(
                {},
                {},
                [],
                {"/api/Uwagi": {"student": [[{"id": "changed-type", "text": "PRIVATE"}]]}},
            )
        ),
    )
    original = job_args.spec.read_bytes()
    assert await contract.run_job(job_args) == 1
    assert job_args.spec.read_bytes() == original
    report = json.loads(job_args.report.read_text())
    assert any(item["status"] == "structure_changed" for item in report["changes"])
    assert "PRIVATE" not in json.dumps(report)


async def test_incomplete_discovery_never_reports_complete(job_args, monkeypatch):
    job_args.no_discover = False
    monkeypatch.setattr(
        contract,
        "discover",
        AsyncMock(
            return_value=({}, [{"host": "student", "status": "partial", "budget_exhausted": True}])
        ),
    )
    assert await contract.run_job(job_args) == 1
    assert json.loads(job_args.report.read_text())["complete"] is False
