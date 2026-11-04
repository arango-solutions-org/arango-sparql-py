"""The service as a project's query tool under a governance control plane.

``arango_sparql/service/control_plane.py``: with ``CONTROL_PLANE_URL`` set, a context token
is the only credential, a session opens on the project's database with the deployment's
own credential, and only routes classified as served answer. The control plane's SDK is
not installed here; :func:`control_plane.set_instance` stands in a verifier that maps
token strings to identities, as r2g's and AOE's tests do.
"""

from __future__ import annotations

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

    def verify(self, token: str) -> control_plane.Identity:
        if self.unavailable:
            raise control_plane.ContextUnavailable("keys unreachable")
        self.verified += 1
        try:
            return self.tokens[token]
        except KeyError:
            raise control_plane.ContextRejected("token rejected: signature") from None


class _FakeDb:
    def __init__(self, name: str) -> None:
        self.name = name

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


# --- configuration --------------------------------------------------------------------------


def test_the_mode_needs_a_tenant_a_source_and_the_sdk(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://control-plane:8000/api")
    monkeypatch.delenv("ARANGO_SPARQL_TENANT", raising=False)
    monkeypatch.setenv("ARANGO_SPARQL_SOURCE", SOURCE)
    assert "ARANGO_SPARQL_TENANT" in (control_plane.configuration_problem() or "")
    monkeypatch.setenv("ARANGO_SPARQL_TENANT", TENANT)
    monkeypatch.delenv("ARANGO_SPARQL_SOURCE")
    assert "ARANGO_SPARQL_SOURCE" in (control_plane.configuration_problem() or "")
    monkeypatch.setenv("ARANGO_SPARQL_SOURCE", SOURCE)
    monkeypatch.setitem(sys.modules, "control_plane_sdk", None)  # import fails
    assert "SDK" in (control_plane.configuration_problem() or "")
    monkeypatch.setitem(sys.modules, "control_plane_sdk", types.ModuleType("control_plane_sdk"))
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
