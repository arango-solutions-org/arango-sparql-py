"""No query content, credential or (in public mode) tenant identifier reaches a log line.

PRD §17.3: the service never writes SPARQL or AQL bodies, NL questions, LLM
completions or query results to its logs; §17.1 class C: passwords and session
tokens never; §17.2: tenant identifiers only when tenant logging is on. §3.14
names this file as the acceptance test.

Every route that takes content is driven with marker strings planted in each
place content enters: the SPARQL text, the ontology, the NL question, the
model's answer, the rows the database returns, the raw AQL, the password, the
session token and the database name. The test then reads everything the
service's handler wrote, in both formats, uvicorn's access line for each request
included, and asserts no marker appears. A seeded batch of generated queries
widens the SPARQL inputs beyond the hand-written ones.
"""

from __future__ import annotations

import random
import string
from typing import Any
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

import arango_sparql.service as svc
from arango_sparql.nl2sparql import ScriptedLLMClient
from arango_sparql.service import _sessions, app
from arango_sparql.service.routes import schema as _schema
from arango_sparql.service.routes.nl import _llm_client_factory
from arango_sparql.translate.owl import turtle_to_mapping
from tests.helpers.fake_arango import FakeDb
from tests.helpers.log_capture import captured_service_logs, emit_access_line
from tests.test_service_nl_routes import ONTOLOGY_TTL, _llm, _wrap

#: Planted content. Each is distinctive enough that finding it in a log line can
#: only mean the content itself was logged.
QUERY_MARKER = "qmk7Hq2Zp"  # SPARQL literals and AQL text
ONTOLOGY_MARKER = "omk4Rt8Wv"  # an rdfs:label in the ontology
QUESTION_MARKER = "nmk9Lc3Xs"  # the NL question
COMPLETION_MARKER = "cmk2Ja6Ye"  # the model's SPARQL answer
ROW_MARKER = "rmk5Dn1Ub"  # a value the database returns
PASSWORD_MARKER = "pmk8Gf4Ti"  # the /connect password
DATABASE_MARKER = "dbmk3Kx7Pw"  # the database name (class B)
ALL_MARKERS = (
    QUERY_MARKER,
    ONTOLOGY_MARKER,
    QUESTION_MARKER,
    COMPLETION_MARKER,
    ROW_MARKER,
    PASSWORD_MARKER,
    DATABASE_MARKER,
)

ONTOLOGY = ONTOLOGY_TTL + f'\n:Person <http://www.w3.org/2000/01/rdf-schema#label> "{ONTOLOGY_MARKER}" .\n'
# The marker opens the query (a comment) and closes it (a literal), so a line
# that logs only a prefix of the query, or only a suffix, is caught too.
QUERY = (
    f"# {QUERY_MARKER}\nPREFIX : <http://ex.org/>\n"
    f'SELECT ?s ?n WHERE {{ ?s a :Person ; :name ?n . FILTER(?n != "{QUERY_MARKER}") }} LIMIT 5'
)
MODEL_SPARQL = (
    "PREFIX : <http://ex.org/>\n"
    f'SELECT ?s ?n WHERE {{ ?s a :Person ; :name ?n . FILTER(?n != "{COMPLETION_MARKER}") }} LIMIT 5'
)
FUZZ_CASES = 200


@pytest.fixture(autouse=True)
def _isolate_sessions():
    _sessions.clear()
    yield
    _sessions.clear()


class _ServiceDb(FakeDb):
    """``tests/helpers/fake_arango.FakeDb`` (python-arango's ``AQL.execute`` /
    ``AQL.explain`` signatures, ``collections()``) plus the two
    ``StandardDatabase`` calls ``/connect`` makes. Every query returns a row
    carrying :data:`ROW_MARKER`."""

    def __init__(self, name: str) -> None:
        super().__init__(
            {"Person": [{"_key": "1", "name": ROW_MARKER}]},
            handler=lambda query, bind_vars: [{"s": "Person/1", "n": ROW_MARKER}],
            name=name,
        )

    def version(self, details: bool = False) -> str:
        return "3.12.0"

    def databases(self) -> list[str]:
        return ["_system", self.name]


class _ServiceClient:
    """``arango.ArangoClient`` as ``/connect`` uses it: ``ArangoClient(hosts=…)``,
    ``.db(name, username=…, password=…)``, ``.close()``."""

    def __init__(self, hosts: str = "", **_more: Any) -> None:
        self.hosts = hosts
        self._dbs: dict[str, _ServiceDb] = {}

    def db(
        self, name: str = "_system", username: str = "root", password: str = "", **_more: Any
    ) -> _ServiceDb:
        return self._dbs.setdefault(name, _ServiceDb(name))

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _fake_database(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(svc, "ArangoClient", _ServiceClient)
    monkeypatch.setenv("ARANGO_SPARQL_CONNECT_ALLOWED_HOSTS", "localhost,127.0.0.1")


@pytest.fixture
def scripted_model():
    """The model answers with SPARQL that carries a marker."""

    def _install(answers: int = 3) -> ScriptedLLMClient:
        client = ScriptedLLMClient([_llm(_wrap(MODEL_SPARQL)) for _ in range(answers)], latency_ms=0)
        app.dependency_overrides[_llm_client_factory] = lambda: client
        return client

    yield _install
    app.dependency_overrides.pop(_llm_client_factory, None)


def _connect(client: TestClient) -> str:
    resp = client.post(
        "/connect",
        json={
            "url": "http://localhost:8529",
            "database": DATABASE_MARKER,
            "username": "root",
            "password": PASSWORD_MARKER,
        },
    )
    assert resp.status_code == 200, resp.text
    # ``/sparql`` reads the session's schema; seed it as the protocol tests do,
    # so the request reaches translation and execution instead of discovery.
    _schema._resolve_schema_cache().put(DATABASE_MARKER, turtle_to_mapping(ONTOLOGY))
    return resp.json()["token"]


def _assert_clean(output: str, *, extra: tuple[str, ...] = ()) -> None:
    assert output.strip(), "nothing was logged: the capture is not wired to the service handler"
    for marker in (*ALL_MARKERS, *extra):
        offending = [line[:400] for line in output.splitlines() if marker in line]
        assert not offending, f"{marker!r} reached the log in {len(offending)} line(s):\n" + "\n".join(
            offending[:5]
        )


def _ok(resp: Any, route: str) -> None:
    """A route that fails before its work proves nothing about its logging."""
    assert resp.status_code == 200, f"{route} answered {resp.status_code}: {resp.text[:300]}"


def _drive_every_route(client: TestClient, token: str) -> list[str]:
    """Call each content-taking route once, successfully; return the addresses uvicorn would log."""
    session = {"X-Arango-Session": token}
    body = {"sparql": QUERY, "ontology_ttl": ONTOLOGY}
    for path in ("/translate", "/validate"):
        _ok(client.post(path, json=body), path)
    for path in ("/execute", "/explain", "/profile"):
        _ok(client.post(path, json={**body, "database": DATABASE_MARKER}, headers=session), path)
    _ok(
        client.post(
            "/execute-aql",
            json={"aql": f'FOR d IN Person FILTER d.name != "{QUERY_MARKER}" RETURN d', "bind_vars": {}},
            headers=session,
        ),
        "/execute-aql",
    )
    for path in ("/nl-translate", "/nl-execute"):
        resp = client.post(
            path,
            json={"nl": f"people not named {QUESTION_MARKER}", "ontology_ttl": ONTOLOGY},
            headers=session,
        )
        _ok(resp, path)
    _ok(
        client.post(
            "/mapping/import-owl", content=ONTOLOGY, headers={**session, "Content-Type": "text/turtle"}
        ),
        "/mapping/import-owl",
    )
    _ok(
        client.post("/mapping/export-owl", json={"ontology_ttl": ONTOLOGY}, headers=session),
        "/mapping/export-owl",
    )
    accept = {"Accept": "application/sparql-results+json"}
    get_address = "/sparql?" + urlencode({"query": QUERY, "session": token})
    _ok(client.get(get_address, headers=accept), "GET /sparql")
    _ok(
        client.post(
            "/sparql",
            content=QUERY,
            headers={**session, **accept, "Content-Type": "application/sparql-query"},
        ),
        "POST /sparql (query body)",
    )
    _ok(client.post("/sparql", data={"query": QUERY}, headers={**session, **accept}), "POST /sparql (form)")
    return [get_address, "/sparql", "/translate"]


#: Requests that fail, each with a marker where the failure's own message would
#: quote it: the token a parse error names, the IRI a schema error names, and a
#: body the 422 handler sees whole (a misspelled field, so pydantic's ``input``
#: for the missing one is the entire body).
_FAILING_REQUESTS = (
    (
        "/translate",
        {"sparql": f"SELECT * WHERE {{ ?s ?p ?o . {QUERY_MARKER} }}", "ontology_ttl": ONTOLOGY},
        422,
    ),
    (
        "/translate",
        {"sparql": f"SELECT ?s WHERE {{ ?s a <http://ex.org/{QUERY_MARKER}> }}", "ontology_ttl": ONTOLOGY},
        422,
    ),
    ("/validate", {"sparql": f"ASK {{ {QUERY_MARKER} }}"}, 200),
    ("/translate", {"sprql": QUERY, "ontology_ttl": ONTOLOGY}, 422),
    ("/nl-translate", {"question": f"people not named {QUESTION_MARKER}", "ontology_ttl": ONTOLOGY}, 422),
)


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "key-value"])
@pytest.mark.parametrize("public_mode", [True, False], ids=["public", "private"])
def test_failing_requests_do_not_quote_their_content(
    monkeypatch: pytest.MonkeyPatch, json_mode: bool, public_mode: bool
) -> None:
    with captured_service_logs(monkeypatch, json_mode=json_mode, public_mode=public_mode) as buf:
        client = TestClient(app)
        token = _connect(client)
        for path, body, status in _FAILING_REQUESTS:
            resp = client.post(path, json=body, headers={"X-Arango-Session": token})
            assert resp.status_code == status, f"{path} {body}: {resp.status_code} {resp.text[:200]}"
            if path == "/translate" and "sparql" in body and status == 422:
                for path2 in ("/execute", "/explain", "/profile"):
                    resp = client.post(
                        path2, json={**body, "database": DATABASE_MARKER}, headers={"X-Arango-Session": token}
                    )
                    assert resp.status_code in (400, 422), f"{path2}: {resp.status_code} {resp.text[:200]}"
    output = buf.getvalue()
    _assert_clean(output.replace(DATABASE_MARKER, "") if not public_mode else output, extra=(token,))
    assert "Pydantic 422" in output and "'loc'" in output.replace('"loc"', "'loc'"), output[-800:]


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "key-value"])
@pytest.mark.parametrize("public_mode", [True, False], ids=["public", "private"])
def test_no_content_or_credential_reaches_the_log(
    monkeypatch: pytest.MonkeyPatch, scripted_model, json_mode: bool, public_mode: bool
) -> None:
    scripted_model(answers=12)
    with captured_service_logs(monkeypatch, json_mode=json_mode, public_mode=public_mode) as buf:
        client = TestClient(app)
        token = _connect(client)
        for address in _drive_every_route(client, token):
            emit_access_line(address)
    output = buf.getvalue()
    if public_mode:
        _assert_clean(output, extra=(token, token[:8]))
    else:
        # Outside public mode tenant logging is on by default (§17.2), so the
        # database name may appear; nothing else may.
        _assert_clean(output.replace(DATABASE_MARKER, ""), extra=(token, token[:8]))
        assert DATABASE_MARKER in output, "tenant logging is on: the database should be named"


def _random_literal(rng: random.Random) -> str:
    alphabet = string.ascii_letters + string.digits + " _-éß漢✓"
    body = "".join(rng.choice(alphabet) for _ in range(rng.randint(4, 24)))
    return f"fz{rng.randrange(10**9):09d}{body}".replace('"', "")


_SHAPES = (
    'SELECT ?s WHERE {{ ?s ?p "{lit}" }}',
    'SELECT ?s WHERE {{ ?s ?p ?o FILTER(CONTAINS(STR(?o), "{lit}")) }}',
    'ASK {{ ?s ?p "{lit}"@en }}',
    "SELECT ?s WHERE {{ ?s <http://ex.org/{iri}> ?o }}",
    'SELECT (COUNT(*) AS ?c) WHERE {{ ?s ?p ?o FILTER(?o = "{lit}") }}',
    'SELECT ?s WHERE {{ VALUES ?o {{ "{lit}" }} ?s ?p ?o }}',
    'SELECT ?s WHERE {{ broken "{lit}"',  # a parse error: the message must not echo it either
)


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "key-value"])
def test_generated_queries_never_reach_the_log(monkeypatch: pytest.MonkeyPatch, json_mode: bool) -> None:
    rng = random.Random(178)
    planted: list[str] = []
    with captured_service_logs(monkeypatch, json_mode=json_mode, public_mode=True) as buf:
        client = TestClient(app)
        for _ in range(FUZZ_CASES):
            lit = _random_literal(rng)
            iri = "p" + "".join(rng.choice(string.ascii_lowercase) for _ in range(12))
            query = rng.choice(_SHAPES).format(lit=lit, iri=iri)
            planted.extend((lit, iri))
            path = rng.choice(("/translate", "/validate"))
            client.post(path, json={"sparql": query, "ontology_ttl": ONTOLOGY})
            emit_access_line("/sparql?" + urlencode({"query": query}))
    output = buf.getvalue()
    leaked = [value for value in planted if value in output]
    assert not leaked, f"{len(leaked)} generated values reached the log, e.g. {leaked[:3]}"
    _assert_clean(output)


def test_an_imported_mapping_names_the_session_by_hash_not_by_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """``/mapping/import-owl`` stamps the mapping's ``source_notes`` with who imported
    it; that note is returned to callers and kept with the mapping, so it carries the
    hashed session id (PRD §17.2), never part of the token (§17.1 class C)."""
    from arango_sparql.service.observability import hash_session_token

    client = TestClient(app)
    token = _connect(client)
    resp = client.post(
        "/mapping/import-owl",
        content=ONTOLOGY,
        headers={"X-Arango-Session": token, "Content-Type": "text/turtle"},
    )
    assert resp.status_code == 200, resp.text
    returned = resp.text
    assert token[:8] not in returned and token not in returned
    assert hash_session_token(token) in returned
