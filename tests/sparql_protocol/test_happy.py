"""End-to-end happy-path tests for the W3C SPARQL Protocol endpoint
(PRD §5.2).

Coverage:

* ``GET /sparql`` (no query) returns a parseable Turtle Service
  Description with the right ``Content-Type``.
* ``GET /sparql?query=…`` translates and executes a SELECT query
  against a fake ArangoDB and returns SPARQL Results JSON by
  default.
* ``POST /sparql`` with ``application/sparql-query`` and with
  ``application/x-www-form-urlencoded`` both work.
* ASK queries return the W3C ``boolean`` body shape in JSON / XML
  / CSV / TSV.
* Observability headers (``X-Response-Time``,
  ``X-Schema-Warnings-Count``, ``X-Aql-Bindings-Count``,
  ``Vary: Accept``, ``Access-Control-Expose-Headers``) are stamped
  onto every response.
* ``?showAQL=true`` adds an ``X-Aql-Query-B64`` header carrying the
  emitted AQL.
* Service Description includes named graphs sourced from the cached
  schema bundle.

Fixtures live in :mod:`tests.sparql_protocol.conftest`.
"""

from __future__ import annotations

import base64
import json
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from rdflib import Graph, Namespace

from arango_sparql.service import _sessions
from tests.helpers.http import assert_varies_on_accept

from .conftest import ASK_QUERY, SELECT_QUERY, set_aql_rows

SD = Namespace("http://www.w3.org/ns/sparql-service-description#")


# ---------------------------------------------------------------------------
# GET /sparql (no query) — Service Description
# ---------------------------------------------------------------------------


def test_get_sparql_no_query_returns_service_description(client: TestClient, session_token: str) -> None:
    resp = client.get(
        "/sparql",
        headers={"X-Arango-Session": session_token},
    )
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/turtle")
    g = Graph()
    g.parse(data=resp.text, format="turtle")
    assert SD.SPARQL11Query in set(g.objects(predicate=SD.supportedLanguage))
    iris = {str(o) for o in g.objects(predicate=SD.name)}
    assert "urn:arango-sparql:graph:Person" in iris


def test_get_sparql_no_query_works_without_session_in_default_mode(
    client: TestClient, fake_arango: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default (non-public) mode: an unauthenticated GET /sparql
    falls back to the env-default connection so a developer's
    ``curl /sparql`` Just Works (PRD §5.2 session-binding paragraph).
    """

    monkeypatch.setenv("ARANGO_URL", "http://localhost:8529")
    monkeypatch.setenv("ARANGO_DB", "_system")
    resp = client.get("/sparql")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/turtle")


def test_service_description_advertises_vary_accept(client: TestClient, session_token: str) -> None:
    resp = client.get(
        "/sparql",
        headers={"X-Arango-Session": session_token},
    )
    assert_varies_on_accept(resp)


# ---------------------------------------------------------------------------
# GET /sparql?query — SELECT happy path
# ---------------------------------------------------------------------------


def test_get_sparql_select_default_returns_results_json(client: TestClient, session_token: str) -> None:
    set_aql_rows(
        session_token,
        [
            {"x": "http://ex.org/Alice"},
            {"x": "http://ex.org/Bob"},
        ],
    )
    resp = client.get(
        "/sparql",
        params={"query": SELECT_QUERY},
        headers={"X-Arango-Session": session_token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["Content-Type"].startswith("application/sparql-results+json")
    payload = json.loads(resp.text)
    assert payload["head"]["vars"] == ["x"]
    assert len(payload["results"]["bindings"]) == 2
    assert payload["results"]["bindings"][0]["x"]["type"] == "uri"


def test_schema_warnings_count_the_translations_own(client: TestClient, session_token: str) -> None:
    # ``ex:nickname`` is not in the mapping: resolving it while translating
    # raises W_SCHEMA_UNMAPPED_IRI. The header counted the resolver's
    # advisories before translation, so it said 0 (#157).
    set_aql_rows(session_token, [])
    resp = client.get(
        "/sparql",
        params={"query": "SELECT ?x ?n WHERE { ?x a <http://ex.org/Person> ; <http://ex.org/nickname> ?n }"},
        headers={"X-Arango-Session": session_token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Schema-Warnings-Count"] == "1"


def test_select_observability_headers_are_stamped(client: TestClient, session_token: str) -> None:
    set_aql_rows(session_token, [{"x": "http://ex.org/A"}])
    resp = client.get(
        "/sparql",
        params={"query": SELECT_QUERY},
        headers={"X-Arango-Session": session_token},
    )
    assert resp.status_code == 200
    assert "X-Response-Time" in resp.headers
    assert resp.headers["X-Schema-Warnings-Count"] == "0"
    assert resp.headers["X-Aql-Bindings-Count"] == "1"
    assert_varies_on_accept(resp)
    expose = resp.headers.get("Access-Control-Expose-Headers", "")
    for h in (
        "X-Response-Time",
        "X-Schema-Warnings-Count",
        "X-Aql-Bindings-Count",
        "X-Aql-Query-B64",
        "Warning",
    ):
        assert h in expose


def test_select_show_aql_emits_base64_header(client: TestClient, session_token: str) -> None:
    set_aql_rows(session_token, [])
    resp = client.get(
        "/sparql",
        params={"query": SELECT_QUERY, "showAQL": "true"},
        headers={"X-Arango-Session": session_token},
    )
    assert resp.status_code == 200
    encoded = resp.headers.get("X-Aql-Query-B64")
    assert encoded is not None and encoded
    aql = base64.b64decode(encoded).decode("utf-8")
    assert "FOR" in aql.upper()
    session = _sessions[session_token]
    assert session.db.aql.calls
    assert session.db.aql.calls[0][0] == aql


def test_select_omits_show_aql_header_when_param_missing(client: TestClient, session_token: str) -> None:
    set_aql_rows(session_token, [])
    resp = client.get(
        "/sparql",
        params={"query": SELECT_QUERY},
        headers={"X-Arango-Session": session_token},
    )
    assert "X-Aql-Query-B64" not in resp.headers


# ---------------------------------------------------------------------------
# POST /sparql — both content-type forms
# ---------------------------------------------------------------------------


def test_post_application_sparql_query_body(client: TestClient, session_token: str) -> None:
    set_aql_rows(session_token, [{"x": "http://ex.org/A"}])
    resp = client.post(
        "/sparql",
        content=SELECT_QUERY.encode("utf-8"),
        headers={
            "Content-Type": "application/sparql-query",
            "X-Arango-Session": session_token,
        },
    )
    assert resp.status_code == 200, resp.text
    payload = json.loads(resp.text)
    assert len(payload["results"]["bindings"]) == 1


def test_post_form_urlencoded_body(client: TestClient, session_token: str) -> None:
    set_aql_rows(session_token, [{"x": "http://ex.org/A"}])
    resp = client.post(
        "/sparql",
        data={"query": SELECT_QUERY},
        headers={"X-Arango-Session": session_token},
    )
    assert resp.status_code == 200, resp.text
    payload = json.loads(resp.text)
    assert len(payload["results"]["bindings"]) == 1


def test_post_form_urlencoded_missing_query_returns_400(client: TestClient, session_token: str) -> None:
    resp = client.post(
        "/sparql",
        data={"notquery": SELECT_QUERY},
        headers={"X-Arango-Session": session_token},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "E_SPARQL_PARSE"


def test_post_with_no_body_returns_400(client: TestClient, session_token: str) -> None:
    resp = client.post(
        "/sparql",
        headers={"X-Arango-Session": session_token},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "E_SPARQL_PARSE"


# ---------------------------------------------------------------------------
# ASK
# ---------------------------------------------------------------------------


def test_ask_true_in_json(client: TestClient, session_token: str) -> None:
    """Visitor emits ``RETURN LENGTH(<inner>) > 0`` so the cursor
    yields a single ``True`` row when at least one match exists.
    """

    set_aql_rows(session_token, [True])
    resp = client.get(
        "/sparql",
        params={"query": ASK_QUERY},
        headers={"X-Arango-Session": session_token},
    )
    assert resp.status_code == 200
    payload = json.loads(resp.text)
    assert payload == {"head": {}, "boolean": True}


def test_ask_false_in_json(client: TestClient, session_token: str) -> None:
    set_aql_rows(session_token, [False])
    resp = client.get(
        "/sparql",
        params={"query": ASK_QUERY},
        headers={"X-Arango-Session": session_token},
    )
    payload = json.loads(resp.text)
    assert payload["boolean"] is False


def test_ask_empty_cursor_treated_as_false(client: TestClient, session_token: str) -> None:
    set_aql_rows(session_token, [])
    resp = client.get(
        "/sparql",
        params={"query": ASK_QUERY},
        headers={"X-Arango-Session": session_token},
    )
    payload = json.loads(resp.text)
    assert payload["boolean"] is False


@pytest.mark.parametrize(
    "media_type,parser",
    [
        ("application/sparql-results+xml", "xml"),
        ("text/csv", "csv"),
        ("text/tab-separated-values", "tsv"),
    ],
)
def test_ask_in_other_formats(
    client: TestClient,
    session_token: str,
    media_type: str,
    parser: str,
) -> None:
    set_aql_rows(session_token, [True])
    resp = client.get(
        "/sparql",
        params={"query": ASK_QUERY},
        headers={
            "X-Arango-Session": session_token,
            "Accept": media_type,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["Content-Type"].startswith(media_type)
    if parser == "xml":
        ns = "http://www.w3.org/2005/sparql-results#"
        root = ET.fromstring(resp.text)
        assert root.find(f"{{{ns}}}boolean").text == "true"
    elif parser == "csv":
        assert resp.text == "_askResult\r\ntrue\r\n"
    elif parser == "tsv":
        assert resp.text == "?_askResult\ntrue\n"


# ---------------------------------------------------------------------------
# AQL execution receives max_runtime
# ---------------------------------------------------------------------------


def test_execute_passes_max_runtime_kwarg(client: TestClient, session_token: str) -> None:
    set_aql_rows(session_token, [])
    resp = client.get(
        "/sparql",
        params={"query": SELECT_QUERY},
        headers={"X-Arango-Session": session_token},
    )
    assert resp.status_code == 200
    session = _sessions[session_token]
    assert session.db.aql.calls
    _aql, _binds, kwargs = session.db.aql.calls[0]
    assert kwargs.get("max_runtime") == 30.0


def test_execute_max_runtime_respects_env(
    client: TestClient,
    session_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPARQL_PROTOCOL_TIMEOUT_SECONDS", "5")
    set_aql_rows(session_token, [])
    client.get(
        "/sparql",
        params={"query": SELECT_QUERY},
        headers={"X-Arango-Session": session_token},
    )
    session = _sessions[session_token]
    _aql, _binds, kwargs = session.db.aql.calls[0]
    assert kwargs.get("max_runtime") == 5.0
