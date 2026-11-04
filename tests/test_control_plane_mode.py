"""The service as a project's query tool under a governance control plane.

``arango_sparql/service/control_plane.py``: with ``CONTROL_PLANE_URL`` set, a context token
is the only credential, a session opens on the project's database with the deployment's
own credential, and only routes classified as served answer. The control plane's SDK is
not installed here; :func:`control_plane.set_instance` stands in a verifier that maps
token strings to identities, as r2g's and AOE's tests do.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount

import arango_sparql.service as svc
from arango_sparql.service import _sessions, app, control_plane

TENANT = "acme"
SOURCE = "warehouse"


def _identity(
    principal: str = "alice",
    *,
    tenant: str = TENANT,
    project: str = "library",
    databases: dict[str, tuple[str, str]] | None = None,
) -> control_plane.Identity:
    return control_plane.Identity(
        principal=principal,
        roles=("analyst",),
        tenant=tenant,
        project=project,
        databases={"main": (SOURCE, "library")} if databases is None else databases,
        expires_at=2_000_000_000,
    )


class _FakeControlPlane:
    def __init__(self, tokens: dict[str, control_plane.Identity], *, unavailable: bool = False) -> None:
        self.tokens = tokens
        self.unavailable = unavailable
        self.verified = 0
        #: Project → its mapping as the control plane serves it; a project not here has none.
        self.mappings: dict[str, str] = {}
        self.mapping_down = False
        self.mapping_reads = 0

    def verify(self, token: str) -> control_plane.Identity:
        if self.unavailable:
            raise control_plane.ContextUnavailable("keys unreachable")
        self.verified += 1
        try:
            return self.tokens[token]
        except KeyError:
            raise control_plane.ContextRejected("token rejected: signature") from None

    def project_mapping(self, token: str) -> str | None:
        try:  # the real one verifies through the SDK too; not counted as the gate's verify
            identity = self.tokens[token]
        except KeyError:
            raise control_plane.ContextRejected("token rejected: signature") from None
        if self.mapping_down:
            raise control_plane.ContextUnavailable("control plane answered 503")
        self.mapping_reads += 1
        return self.mappings.get(identity.project)


class _FakeDb:
    #: Every call that would change the database, from any test's sessions.
    writes: list[str] = []

    def __init__(self, name: str) -> None:
        self.name = name

    def has_collection(self, name: str) -> bool:
        return True

    def create_collection(self, name: str) -> Any:
        _FakeDb.writes.append(f"create {name}")
        raise AssertionError("must not create a collection in the project's database")

    def collection(self, name: str) -> Any:
        db = self

        class _Col:
            def delete(self, key: str, **_: Any) -> bool:
                _FakeDb.writes.append(f"delete {name}/{key} in {db.name}")
                return True

            def insert(self, doc: Any, **_: Any) -> Any:
                _FakeDb.writes.append(f"insert {name} in {db.name}")
                return {}

        return _Col()

    def version(self) -> str:
        return "3.12.4"

    def graphs(self) -> list[dict[str, Any]]:
        return [{"name": f"{self.name}-graph", "edge_definitions": [], "orphan_collections": []}]


class _FakeClient:
    opened: list[dict[str, Any]] = []

    def __init__(self, hosts: str, **_: Any) -> None:
        self.hosts = hosts

    def db(self, name: str, username: str = "", password: str = "", **_: Any) -> _FakeDb:
        _FakeClient.opened.append(
            {"hosts": self.hosts, "db": name, "username": username, "password": password}
        )
        return _FakeDb(name)

    def close(self) -> None:
        pass


TOKENS = {
    "tok-alice": _identity(),
    "tok-alice-renewed": _identity(),
    "tok-bob": _identity("bob"),
    "tok-other-project": _identity(project="equity", databases={"main": (SOURCE, "equity")}),
    "tok-same-db-other-project": _identity(project="catalog-copy"),
    "tok-other-tenant": _identity(tenant="globex"),
    "tok-other-source": _identity(databases={"main": ("elsewhere", "library")}),
    "tok-two-here": _identity(databases={"a": (SOURCE, "one"), "b": (SOURCE, "two")}),
}


@pytest.fixture
def governed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://control-plane:8000/api")
    monkeypatch.setenv("CONTROL_PLANE_HOME", "/")
    monkeypatch.setenv("ARANGO_SPARQL_TENANT", TENANT)
    monkeypatch.setenv("ARANGO_SPARQL_SOURCE", SOURCE)
    monkeypatch.setenv("ARANGO_URL", "http://db.internal:8529")
    monkeypatch.setenv("ARANGO_USER", "reader")
    monkeypatch.setenv("ARANGO_PASSWORD", "server-side-secret")
    monkeypatch.setattr(svc, "ArangoClient", _FakeClient)
    fake = _FakeControlPlane(dict(TOKENS))
    control_plane.set_instance(fake)
    _FakeClient.opened = []
    _FakeDb.writes = []
    _sessions.clear()
    yield fake
    control_plane.set_instance(None)
    _sessions.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- classification -------------------------------------------------------------------------


def _all_routes() -> list[tuple[str, str]]:
    keys = []
    for route in app.routes:
        if isinstance(route, Mount):
            continue
        for method in sorted(getattr(route, "methods", None) or []):
            if method != "HEAD":
                keys.append((method, route.path))  # type: ignore[attr-defined]
    return keys


def test_every_route_is_open_refused_or_served_exactly_once() -> None:
    """A new route fails here until someone classifies it, and the gate refuses it meanwhile."""
    keys = _all_routes()
    lists = (control_plane.OPEN, set(control_plane.REFUSED), control_plane.SERVED)
    for key in keys:
        assert sum(key in group for group in lists) == 1, key
    assert set(keys) == set().union(*lists)
    assert (len(control_plane.OPEN), len(control_plane.REFUSED), len(control_plane.SERVED)) == (3, 8, 30)
    assert any(isinstance(r, APIRoute) and r.path == "/execute-aql" for r in app.routes)


def test_an_unclassified_route_is_refused(governed, client, monkeypatch) -> None:
    monkeypatch.setattr(control_plane, "SERVED", control_plane.SERVED - {("POST", "/translate")})
    response = client.post(
        "/translate", json={"sparql": "SELECT * WHERE { ?s ?p ?o }"}, headers=_bearer("tok-alice")
    )
    assert response.status_code == 403
    assert "not classified" in response.json()["detail"]
    # positive control: classified, the same request passes the gate
    monkeypatch.setattr(control_plane, "SERVED", control_plane.SERVED | {("POST", "/translate")})
    response = client.post(
        "/translate", json={"sparql": "SELECT * WHERE { ?s ?p ?o }"}, headers=_bearer("tok-alice")
    )
    assert response.status_code != 403


@pytest.mark.parametrize("method,path", sorted(control_plane.REFUSED))
def test_refused_routes_are_refused_even_with_a_valid_context(governed, client, method, path) -> None:
    response = client.request(method, path, headers=_bearer("tok-alice"), json={})
    assert response.status_code == 403
    assert control_plane.REFUSED[(method, path)] in response.json()["detail"]


def test_a_refused_route_is_refused_under_a_path_prefix(governed) -> None:
    """The gate matches as the router does, so a ``ROOT_PATH`` mount does not slip past it."""
    prefixed = TestClient(app, root_path="/query")
    assert prefixed.get("/query/connect/defaults", headers=_bearer("tok-alice")).status_code == 403
    assert prefixed.get("/query/graphs").status_code == 401
    assert prefixed.get("/query/graphs", headers=_bearer("tok-alice")).status_code == 200


# --- the context is the only credential -----------------------------------------------------


def test_open_routes_need_no_context(governed, client) -> None:
    assert client.get("/health").status_code == 200
    body = client.get("/control-plane/mode").json()
    assert body == {"control_plane": True, "home": "/"}


def test_a_served_route_needs_a_context(governed, client) -> None:
    response = client.get("/graphs")
    assert response.status_code == 401
    assert "open this tool from your project" in response.json()["detail"]
    assert response.headers["www-authenticate"] == "Bearer"


def test_an_invalid_context_is_refused(governed, client) -> None:
    response = client.get("/graphs", headers=_bearer("forged"))
    assert response.status_code == 401
    assert "Invalid project context" in response.json()["detail"]


def test_a_context_for_another_tenant_is_refused(governed, client) -> None:
    response = client.get("/graphs", headers=_bearer("tok-other-tenant"))
    assert response.status_code == 401
    assert "another tenant" in response.json()["detail"]
    assert _FakeClient.opened == []


def test_a_session_token_cannot_stand_in_for_a_context(governed, client) -> None:
    """A session's key is never a credential: every token is verified as a context."""
    assert client.get("/graphs", headers=_bearer("tok-alice")).status_code == 200
    (key,) = _sessions
    assert client.get("/graphs", headers={"X-Arango-Session": key}).status_code == 401
    assert client.get("/graphs", headers=_bearer(key)).status_code == 401


def test_the_context_travels_only_as_a_bearer_token(governed, client) -> None:
    """Not in the session header either: one place for the one credential."""
    assert client.get("/graphs", headers={"X-Arango-Session": "tok-alice"}).status_code == 401
    assert client.get("/graphs", headers=_bearer("tok-alice")).status_code == 200


def test_a_request_verifies_its_context_once(governed, client) -> None:
    """The gate keeps what it verified on the request; the session reuses it."""
    client.get("/graphs", headers=_bearer("tok-alice"))
    assert governed.verified == 1


def test_a_context_in_the_query_string_is_not_accepted(governed, client) -> None:
    """Never in the address: it would land in access logs. ``/sparql`` took ``?session=``."""
    response = client.get("/sparql", params={"query": "ASK { ?s ?p ?o }", "session": "tok-alice"})
    assert response.status_code == 401
    assert _FakeClient.opened == []


def test_a_request_carries_one_authorization_header(governed) -> None:
    """The gate and the rate limits must read the same credential: with two headers, the
    gate verified the last while the limits counted the first, so junk first headers made
    every request a new client."""
    from starlette.testclient import TestClient as Raw

    raw = Raw(app)
    two = [("authorization", "Bearer junk"), ("authorization", "Bearer tok-alice")]
    assert raw.get("/graphs", headers=two).status_code == 400
    assert raw.get("/graphs", headers=[two[1]]).status_code == 200  # positive control


def test_rate_limits_count_the_user_not_the_token(governed, client, monkeypatch) -> None:
    """A renewed token is the same user: it does not get a fresh budget."""
    from arango_sparql.service import security

    monkeypatch.setattr(svc, "_compute_bucket", security._TokenBucket(2))
    body = {"sparql": "ASK { ?s ?p ?o }"}
    answers = [
        client.post("/validate", json=body, headers=_bearer(t)).status_code
        for t in ("tok-alice", "tok-alice", "tok-alice-renewed")
    ]
    assert answers == [200, 200, 429]
    assert client.post("/validate", json=body, headers=_bearer("tok-bob")).status_code == 200


def test_the_schema_cache_writes_nothing_to_the_projects_database(governed, client) -> None:
    """The service only reads the project's database: refreshing the schema drops the
    in-memory copy and writes nothing there."""
    response = client.post("/schema/invalidate-cache", headers=_bearer("tok-alice"))
    assert response.status_code == 200
    assert response.json()["persistent_dropped"] is False
    assert _FakeDb.writes == []


def test_routes_without_a_required_session_still_get_the_contexts(governed, client) -> None:
    """``/translate`` and the plain-English routes take a session when there is one, to use
    the database's schema: under a control plane that is the context's."""
    client.post("/translate", json={"sparql": "ASK { ?s ?p ?o }"}, headers=_bearer("tok-alice"))
    assert [o["db"] for o in _FakeClient.opened] == ["library"]


def test_an_unreachable_control_plane_answers_503(governed, client) -> None:
    governed.unavailable = True
    assert client.get("/graphs", headers=_bearer("tok-alice")).status_code == 503


# --- the session ----------------------------------------------------------------------------


def test_the_session_opens_on_the_projects_database_with_the_servers_credential(governed, client) -> None:
    response = client.get("/graphs", headers=_bearer("tok-alice"))
    assert response.status_code == 200
    assert response.json()["graphs"][0]["name"] == "library-graph"
    assert _FakeClient.opened == [
        {
            "hosts": "http://db.internal:8529",
            "db": "library",
            "username": "reader",
            "password": "server-side-secret",
        }
    ]


LIB = "https://example.org/library#"
#: Library's mapping as the control plane assembles it (FR-14.7 there): the project's IRIs,
#: with the collection, field and edge collection that hold each.
LIBRARY_MAPPING = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix phys: <http://arangodb.com/schema/physical#> .
@prefix : <{LIB}> .

<{LIB}Loan> a owl:Class ;
    phys:collectionName "loans" .
<{LIB}Member> a owl:Class ;
    phys:collectionName "members" ;
    rdfs:subClassOf <{LIB}Agent> .
<{LIB}fullName> a owl:DatatypeProperty ;
    rdfs:domain <{LIB}Member> ;
    phys:attributeName "name" .
<{LIB}borrowedBy> a owl:ObjectProperty ;
    rdfs:domain <{LIB}Loan> ;
    rdfs:range <{LIB}Member> ;
    phys:edgeCollectionName "borrowed_by" .
"""


def _sparql(text: str) -> dict[str, str]:
    return {"sparql": f"PREFIX : <{LIB}>\n{text}"}


def test_a_projects_mapping_is_the_sessions_schema(governed, client) -> None:
    """The project's IRIs translate to where the data lives: a class to its collection, a
    property to its field (not its local name), a relationship to its edge collection."""
    governed.mappings["library"] = LIBRARY_MAPPING
    r = client.post(
        "/translate",
        json=_sparql("SELECT ?n WHERE { ?l a :Loan ; :borrowedBy ?m . ?m :fullName ?n }"),
        headers=_bearer("tok-alice"),
    )
    assert r.status_code == 200, r.text
    aql, binds = r.json()["aql"], json.dumps(r.json()["bind_vars"])
    assert "loans" in binds and "borrowed_by" in binds and "fullName" not in aql + binds
    assert ".name" in aql or '"name"' in binds
    body = client.get("/schema/introspect", headers=_bearer("tok-alice")).json()
    assert body["mapping"]["source"]["kind"] == "imported_owl"
    names = {e["name"] for e in body["mapping"]["conceptualSchema"]["entities"]}
    assert names == {"Loan", "Member"}  # for the NL prompt and the editor
    # positive control: without a mapping, the same IRIs are not the project's
    governed.mappings.clear()
    _sessions.clear()
    r = client.post(
        "/translate", json=_sparql("SELECT ?l WHERE { ?l a :Loan }"), headers=_bearer("tok-alice")
    )
    assert "loans" not in json.dumps(r.json().get("bind_vars", {}))


def test_the_projects_mapping_is_read_again_after_a_while(governed, client, monkeypatch) -> None:
    governed.mappings["library"] = LIBRARY_MAPPING
    client.get("/graphs", headers=_bearer("tok-alice"))
    client.get("/graphs", headers=_bearer("tok-alice"))
    assert governed.mapping_reads == 1
    monkeypatch.setattr(control_plane, "MAPPING_REFRESH_SECONDS", 0.0)
    client.get("/graphs", headers=_bearer("tok-alice"))
    assert governed.mapping_reads == 2


def test_an_unreadable_mapping_is_503_until_one_was_read(governed, client, monkeypatch) -> None:
    """Never an introspected schema in its place: its terms would not be the project's."""
    governed.mappings["library"] = LIBRARY_MAPPING
    governed.mapping_down = True
    r = client.get("/graphs", headers=_bearer("tok-alice"))
    assert r.status_code == 503 and "mapping" in r.json()["detail"]
    governed.mapping_down = False
    assert client.get("/graphs", headers=_bearer("tok-alice")).status_code == 200
    # once read, a session keeps it through an outage
    monkeypatch.setattr(control_plane, "MAPPING_REFRESH_SECONDS", 0.0)
    governed.mapping_down = True
    assert client.get("/graphs", headers=_bearer("tok-alice")).status_code == 200
    (session,) = _sessions.values()
    assert session.project_mapping is not None


def test_the_context_route_says_who_and_where(governed, client) -> None:
    body = client.get("/control-plane/context", headers=_bearer("tok-alice")).json()
    assert body["principal"] == "alice"
    assert body["project"] == "library"
    assert (body["binding"], body["database"], body["problem"]) == ("main", "library", None)
    assert "server-side-secret" not in str(body)


def test_a_renewed_context_keeps_its_session(governed, client) -> None:
    client.get("/graphs", headers=_bearer("tok-alice"))
    client.get("/graphs", headers=_bearer("tok-alice-renewed"))
    assert len(_FakeClient.opened) == 1
    assert len(_sessions) == 1


def test_users_and_projects_get_their_own_sessions(governed, client) -> None:
    for token in ("tok-alice", "tok-bob", "tok-other-project"):
        assert client.get("/graphs", headers=_bearer(token)).status_code == 200
    assert [o["db"] for o in _FakeClient.opened] == ["library", "library", "equity"]
    assert len(_sessions) == 3
    # two projects on one database keep their own sessions (a graph scope is per session)
    assert client.get("/graphs", headers=_bearer("tok-same-db-other-project")).status_code == 200
    assert len(_sessions) == 4


def test_a_binding_on_another_source_is_never_served_from_this_one(governed, client) -> None:
    """A database named ``library`` on another deployment is a different database."""
    response = client.get("/graphs", headers=_bearer("tok-other-source"))
    assert response.status_code == 409
    assert "no database on source 'warehouse'" in response.json()["detail"]
    assert _FakeClient.opened == []
    body = client.get("/control-plane/context", headers=_bearer("tok-other-source")).json()
    assert body["database"] is None and "no database" in body["problem"]


def test_several_bindings_on_this_source_are_refused_rather_than_guessed(governed, client) -> None:
    response = client.get("/graphs", headers=_bearer("tok-two-here"))
    assert response.status_code == 409
    assert "several databases" in response.json()["detail"]


def test_the_protocol_endpoint_uses_the_context_not_the_env_default(governed, client) -> None:
    client.get("/sparql", params={"query": "ASK { ?s ?p ?o }"}, headers=_bearer("tok-alice"))
    assert [o["db"] for o in _FakeClient.opened] == ["library"]


# --- logs -------------------------------------------------------------------------------------


def test_credentials_in_an_address_never_reach_the_access_log() -> None:
    """A tool's first link carries ``?ctx=`` and ``/sparql`` takes ``?session=``: uvicorn's
    access log would otherwise write both in full."""
    import logging

    from arango_sparql.service.observability import AccessLogRedactFilter, redact_query

    assert redact_query("/?ctx=eyJ.a.b&tab=x") == "/?ctx=[redacted]&tab=x"
    assert redact_query("/sparql?query=ASK&session=tok&%63tx=t2&TOKEN=t3") == (
        "/sparql?query=ASK&session=[redacted]&%63tx=[redacted]&TOKEN=[redacted]"
    )
    assert redact_query("/graphs") == "/graphs"
    assert redact_query("/?context=keep") == "/?context=keep"
    access = logging.getLogger("uvicorn.access")
    assert any(isinstance(f, AccessLogRedactFilter) for f in access.filters)
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/?ctx=secret", "1.1", 200),
        None,
    )
    assert all(f.filter(record) for f in access.filters)
    assert "secret" not in record.getMessage()


# --- configuration --------------------------------------------------------------------------


def test_the_mode_needs_a_tenant_a_source_and_the_sdk(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://control-plane:8000/api")
    monkeypatch.delenv("ARANGO_SPARQL_TENANT", raising=False)
    monkeypatch.setenv("ARANGO_SPARQL_SOURCE", SOURCE)
    monkeypatch.setenv("ARANGO_USER", "reader")
    assert "ARANGO_SPARQL_TENANT" in (control_plane.configuration_problem() or "")
    monkeypatch.setenv("ARANGO_SPARQL_TENANT", TENANT)
    monkeypatch.delenv("ARANGO_SPARQL_SOURCE")
    assert "ARANGO_SPARQL_SOURCE" in (control_plane.configuration_problem() or "")
    monkeypatch.setenv("ARANGO_SPARQL_SOURCE", SOURCE)
    monkeypatch.delenv("ARANGO_USER")
    assert "ARANGO_USER" in (control_plane.configuration_problem() or "")
    monkeypatch.setenv("ARANGO_USER", "reader")
    monkeypatch.setitem(sys.modules, "control_plane_sdk", None)  # import fails
    assert "SDK" in (control_plane.configuration_problem() or "")
    old = types.ModuleType("control_plane_sdk")
    old.ControlPlane = type("ControlPlane", (), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "control_plane_sdk", old)
    assert "project_mapping" in (control_plane.configuration_problem() or "")
    new = types.ModuleType("control_plane_sdk")
    new.ControlPlane = type("ControlPlane", (), {"project_mapping": lambda self, v: None})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "control_plane_sdk", new)
    assert control_plane.configuration_problem() is None


def test_without_a_control_plane_nothing_changes(monkeypatch, client) -> None:
    monkeypatch.delenv("CONTROL_PLANE_URL", raising=False)
    assert client.get("/control-plane/mode").json() == {"control_plane": False, "home": None}
    assert client.get("/connect/defaults").status_code == 200
    assert client.get("/graphs").status_code == 401  # the tool's own session rule, as before
    assert client.get("/control-plane/context").status_code == 404


# --- the SDK adapter ------------------------------------------------------------------------


def test_the_sdk_adapter_maps_a_verified_context(monkeypatch) -> None:
    sdk = types.ModuleType("control_plane_sdk")

    class ControlPlaneError(Exception):
        pass

    class Unavailable(ControlPlaneError):
        pass

    class ControlPlane:
        def __init__(self, url: str, *, tool: str) -> None:
            self.url, self.tool = url, tool

        def verify(self, token: str) -> Any:
            if token == "down":
                raise Unavailable("no keys")
            if token != "good":
                raise ControlPlaneError("bad signature")
            binding = types.SimpleNamespace(source=SOURCE, database="library")
            ctx = types.SimpleNamespace(
                principal=types.SimpleNamespace(id="alice", roles=("analyst",)),
                tenant=TENANT,
                project="library",
                databases={"main": binding},
            )
            return types.SimpleNamespace(context=ctx, expires_at=123)

    sdk.ControlPlaneError, sdk.Unavailable, sdk.ControlPlane = ControlPlaneError, Unavailable, ControlPlane  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "control_plane_sdk", sdk)
    adapter = control_plane._Sdk("http://control-plane:8000/api")
    assert adapter._client.tool == "query"
    assert adapter.verify("good") == control_plane.Identity(
        principal="alice",
        roles=("analyst",),
        tenant=TENANT,
        project="library",
        databases={"main": (SOURCE, "library")},
        expires_at=123,
    )
    with pytest.raises(control_plane.ContextRejected):
        adapter.verify("bad")
    with pytest.raises(control_plane.ContextUnavailable):
        adapter.verify("down")
    # The project's mapping, through the SDK with the same context; its errors, mapped.
    ControlPlane.project_mapping = lambda self, verified: f"ttl for {verified.context.project}"  # type: ignore[attr-defined]
    assert adapter.project_mapping("good") == "ttl for library"
    with pytest.raises(control_plane.ContextRejected):
        adapter.project_mapping("bad")
    with pytest.raises(control_plane.ContextUnavailable):
        adapter.project_mapping("down")
