"""Connection management endpoints — ``/connect``, ``/disconnect``,
``/connections``, ``/connect/defaults``.

Mirror of ``arango_cypher.service.routes.connect`` with the standard
``arango_cypher`` → ``arango_sparql`` env-var renames (``ARANGO_DB``,
``ARANGO_USER``, ``ARANGO_PASSWORD`` are shared across the sister
projects so those keep their canonical names).
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Any

from arango import ArangoClient
from arango.database import StandardDatabase
from fastapi import Depends, HTTPException, Request

from ..._env import (
    read_arango_database,
    read_arango_password,
    read_arango_url,
    read_arango_username,
)
from ..._privacy import tenant_name
from ..app import _PUBLIC_MODE, _svc_logger, app
from ..models import (
    BindGraphRequest,
    ConnectRequest,
    ConnectResponse,
    PlatformConnectRequest,
    PlatformStatus,
)
from ..observability import log_endpoint_timing
from ..platform_auth import (
    DEPLOYMENT_CA_ENV,
    DEPLOYMENT_ENDPOINT_ENV,
    PLATFORM_AUTH_ENV,
    PlatformTokenError,
    SidecarError,
    choose_database,
    default_database,
    deployment_ca,
    describe_endpoint,
    describe_tls_verify,
    endpoint_answer,
    forwarded_token,
    open_platform_database,
    platform_auth_enabled,
    platform_endpoint,
    platform_tls_verify,
    probe_endpoint,
    sidecar_address,
    sidecar_identity,
    sidecar_token,
    token_facts,
)
from ..security import (
    _check_connect_target,
    _describe_connect_error,
    _evict_lru,
    _get_session,
    _prune_expired,
    _require_session_in_public_mode,
    _service_pkg_candidates,
    _Session,
    _sessions,
)


def _resolve_arango_client():
    """Return the currently patched service-level ArangoClient, if any.

    Tests do ``monkeypatch.setattr("arango_sparql.service.ArangoClient", FakeClient)``
    to drive ``/connect`` against a stub. This helper walks the live
    ``arango_sparql.service`` package objects (see
    :func:`arango_sparql.service.security._service_pkg_candidates`) and
    returns the first override; falls through to the real
    :class:`arango.ArangoClient` when nothing has been patched.
    """
    fallback = None
    for pkg in _service_pkg_candidates():
        client_cls = getattr(pkg, "ArangoClient", None)
        if client_cls is None:
            continue
        if client_cls is not ArangoClient:
            return client_cls
        fallback = client_cls

    return fallback or ArangoClient


@app.post("/connect", response_model=ConnectResponse)
def connect(req: ConnectRequest):
    """Authenticate to ArangoDB; returns a session token."""
    t0 = time.perf_counter()
    _check_connect_target(req.url)
    try:
        url = req.url.rstrip("/")
        client = _resolve_arango_client()(hosts=url)
        db = client.db(req.database, username=req.username, password=req.password)
        # Force an authenticated round-trip — python-arango's ``.db()``
        # is lazy and doesn't validate credentials until the first call.
        db.version()
    except Exception as e:
        detail = _describe_connect_error(e)
        _svc_logger.warning(
            "connect failed for db=%r user=%r: %s",
            tenant_name(req.database),
            req.username,
            detail,
        )
        log_endpoint_timing(
            "/connect",
            round((time.perf_counter() - t0) * 1000, 1),
            status="error",
            database=req.database,
            error_type=type(e).__name__,
        )
        raise HTTPException(
            status_code=400,
            detail=f"Connection failed: {detail}",
        ) from e

    _evict_lru()
    token = secrets.token_urlsafe(32)
    _sessions[token] = _Session(token=token, db=db, client=client)

    try:
        databases = list(client.db("_system", username=req.username, password=req.password).databases())
    except Exception:
        # ``databases()`` requires _system access; if the connecting user
        # is database-scoped we still want the session — surface the
        # only db we know they can see rather than failing the connect.
        databases = [req.database]

    log_endpoint_timing(
        "/connect",
        round((time.perf_counter() - t0) * 1000, 1),
        database=req.database,
        databases_visible=len(databases),
    )
    return ConnectResponse(token=token, databases=databases, database=req.database)


def _platform_unavailable_reason(request: Request) -> str | None:
    """Why this request cannot open a platform session, or ``None``."""
    if not platform_auth_enabled():
        return f"platform sessions are disabled ({PLATFORM_AUTH_ENV})"
    if platform_endpoint() is None:
        return f"no cluster endpoint configured ({DEPLOYMENT_ENDPOINT_ENV} or ARANGO_URL)"
    if forwarded_token(request) is None:
        return "the request carried no platform login (the service is not behind the platform gateway)"
    return None


@app.get("/connect/platform", response_model=PlatformStatus)
def platform_status(request: Request):
    """Whether the Workbench can skip the connect dialog.

    True when the service knows its cluster's endpoint *and* the request
    arrived through the platform gateway carrying the user's platform JWT.
    The UI calls this on load and, when available, opens a session with
    ``POST /connect/platform`` instead of asking for credentials. Discloses
    no credential and no endpoint — only the database it would open.
    """
    reason = _platform_unavailable_reason(request)
    return PlatformStatus(available=reason is None, database=default_database(), reason=reason)


@app.post("/connect/platform", response_model=ConnectResponse)
def connect_platform(req: PlatformConnectRequest, request: Request):
    """Open a session as the platform user who sent this request.

    The session authenticates with the gateway-forwarded JWT rather than a
    password, so it sees exactly what the user's platform permissions allow
    and the service stores no credential. The coordinator validates the JWT
    on the first call; a rejected one is a 401 the UI turns into "sign in
    again". The target is server configuration only — never the request —
    so the SSRF guard ``/connect`` applies to user-supplied URLs is not
    needed here.
    """
    t0 = time.perf_counter()
    database = req.database or default_database()

    def _fail(status: int, error: str, message: str, *, error_type: str) -> HTTPException:
        log_endpoint_timing(
            "/connect/platform",
            round((time.perf_counter() - t0) * 1000, 1),
            status="error",
            database=database,
            error_type=error_type,
        )
        return HTTPException(status_code=status, detail={"error": error, "message": message})

    reason = _platform_unavailable_reason(request)
    endpoint = platform_endpoint()
    token = forwarded_token(request)
    if reason is not None or endpoint is None or token is None:
        raise _fail(
            404,
            "platform_session_unavailable",
            f"Cannot use the platform login: {reason}. Connect with credentials instead.",
            error_type="unavailable",
        )

    verify = platform_tls_verify()
    # Deviation from the arango-cypher-py mirror: that project reads
    # ``_svc.ArangoClient`` directly here; this repo routes every client build
    # through ``_resolve_arango_client()`` (see ``/connect`` above) so the test
    # monkeypatch seam is identical across both connect paths.
    client = _resolve_arango_client()(hosts=endpoint, verify_override=verify)

    def _open(name: str) -> StandardDatabase:
        try:
            return open_platform_database(client, name, token)
        except PlatformTokenError as e:
            client.close()
            _svc_logger.warning("platform connect refused for db=%r: %s", name, e)
            raise _fail(
                401,
                "platform_login_rejected",
                f"Cannot open a session with your platform login: {e}. Sign in to the platform again.",
                error_type="unusable_token",
            ) from e

    # The databases *this user* may open — ``/_api/database/user``, not
    # ``_system.databases()``, which needs _system access a platform user
    # usually lacks. ``None`` when the listing itself fails.
    #
    # Deviation from the arango-cypher-py mirror: that project wraps the call
    # in its ``_arango_sync.sync(...)`` typing guard; this repo has no such
    # module, and its ``/connect`` already lists with a plain ``.databases()``,
    # so the direct ``list(...)`` keeps the two connect paths consistent.
    def _accessible(db: StandardDatabase) -> list[str] | None:
        try:
            names: list[str] = list(db.databases_accessible_to_user())
        except Exception as exc:
            _svc_logger.warning("listing accessible databases failed: %s", exc)
            return None
        return sorted(names)

    accessible: list[str] | None = None
    if req.database is None:
        accessible = _accessible(_open("_system"))
        database = choose_database(accessible)

    db = _open(database)
    try:
        # Force an authenticated round-trip — ``.db()`` is lazy, so the
        # coordinator only validates the forwarded JWT on the first call.
        db.version()
    except Exception as e:
        client.close()
        code = getattr(e, "http_code", None)
        detail = _describe_connect_error(e)
        _svc_logger.warning(
            "platform connect failed for db=%r (HTTP %s): %s", tenant_name(database), code, detail
        )
        if code in (401, 403):
            raise _fail(
                401,
                "platform_login_rejected",
                f"The cluster refused your platform login for database {database!r}. "
                "Sign in to the platform again, or pick a database you can access.",
                error_type="rejected",
            ) from e
        if code == 404:
            raise _fail(
                404,
                "unknown_database",
                f"Database {database!r} does not exist on this cluster.",
                error_type="unknown_database",
            ) from e
        cause = probe_endpoint(endpoint, token, verify)
        _svc_logger.warning(
            "platform endpoint %s unreachable (tls_verify=%s): %s",
            describe_endpoint(endpoint),
            "bundle" if isinstance(verify, str) else verify,
            cause,
        )
        raise _fail(
            502,
            "cluster_unreachable",
            f"Could not reach the cluster at {describe_endpoint(endpoint)} for database "
            f"{database!r}: {detail} — {cause}",
            error_type=type(e).__name__,
        ) from e

    if accessible is None:
        accessible = _accessible(db)
    databases = list(accessible or [])
    if database not in databases:
        databases.append(database)

    # Who the caller is, as the sidecar (which validates the token) says.
    # Background work for this session runs as this user; unknown is fine.
    user = sidecar_identity(token)

    _evict_lru()
    session_token = secrets.token_urlsafe(32)
    _sessions[session_token] = _Session(
        token=session_token, db=db, client=client, platform_token=token, platform_user=user
    )

    log_endpoint_timing(
        "/connect/platform",
        round((time.perf_counter() - t0) * 1000, 1),
        database=database,
        requested=req.database is not None,
        databases_visible=len(databases),
        user_identified=user is not None,
        tls=describe_tls_verify(verify),
    )
    return ConnectResponse(token=session_token, databases=databases, database=database, user=user)


#: Lifetime of the token the diagnostics mint to test the sidecar.
_DIAGNOSTIC_TOKEN_LIFETIME_S = 60


@app.get("/connect/platform/diagnostics")
def platform_diagnostics(request: Request) -> dict[str, Any]:
    """What the platform provides this container, and whether each piece works.

    Mirror of arango-cypher-py's endpoint. For checking a BYOC deployment: the
    injected endpoint and CA, the TLS policy in use and a direct request under
    it, the forwarded login, and the integration sidecar (who the caller is,
    and a short-lived token minted for them, tried against the endpoint).
    Reports facts about tokens (claim names, lifetime), never a token or a
    claim value. In a browser, sign in to the platform at ``/ui/`` first.
    """
    endpoint = platform_endpoint()
    verify = platform_tls_verify()
    token = forwarded_token(request)
    report: dict[str, Any] = {
        "platform_auth": platform_auth_enabled(),
        "endpoint": {
            "injected": bool(os.getenv(DEPLOYMENT_ENDPOINT_ENV, "").strip()),
            "address": describe_endpoint(endpoint) if endpoint else None,
        },
        "tls": {
            "ca_injected": bool(os.getenv(DEPLOYMENT_CA_ENV, "").strip()),
            "ca_usable": deployment_ca() is not None,
            "policy": describe_tls_verify(verify),
        },
        "forwarded_login": token_facts(token) if token else None,
        "sidecar": {"address_injected": sidecar_address() is not None},
    }
    if endpoint and token:
        report["tls"]["direct_request"] = endpoint_answer(endpoint, token, verify)
        ca = deployment_ca()
        if ca and verify != ca:
            report["tls"]["direct_request_with_injected_ca"] = endpoint_answer(endpoint, token, ca)
    if token and sidecar_address():
        user = sidecar_identity(token)
        report["sidecar"]["identity_found"] = user is not None
        if user:
            try:
                minted = sidecar_token(user, _DIAGNOSTIC_TOKEN_LIFETIME_S)
            except SidecarError as exc:
                report["sidecar"]["create_token"] = str(exc)
            else:
                report["sidecar"]["minted_token"] = token_facts(minted)
                if endpoint:
                    report["sidecar"]["minted_token_request"] = endpoint_answer(endpoint, minted, verify)
    return report


@app.post("/disconnect")
def disconnect(session: _Session = Depends(_get_session)):
    """Tear down the session and release the python-arango client."""
    t0 = time.perf_counter()
    _sessions.pop(session.token, None)
    session.client.close()
    log_endpoint_timing(
        "/disconnect",
        round((time.perf_counter() - t0) * 1000, 1),
    )
    return {"status": "disconnected"}


@app.get("/graphs")
def list_graphs(session: _Session = Depends(_get_session)):
    """List the ArangoDB named graphs visible to this session's database.

    Lets the UI offer a graph-scope picker so schema acquisition (and the
    NL suggestions / OWL view derived from it) can be restricted to the
    collections of one graph — useful when other applications share the
    database with collections irrelevant to the queries at hand. A
    database with no named graphs (or a ``graphs()`` call that fails for a
    permission-scoped user) returns an empty list rather than erroring;
    "no graphs" is a normal state the picker hides.
    """
    t0 = time.perf_counter()
    graphs: list[dict[str, object]] = []
    try:
        raw = session.db.graphs()
    except Exception as exc:  # noqa: BLE001 — degrade to "no graphs"
        _svc_logger.info("graphs() failed for db=%r: %s", tenant_name(getattr(session.db, "name", "?")), exc)
        raw = []

    for g in raw or []:
        name = g.get("name") if isinstance(g, dict) else None
        if not name:
            continue
        edge_defs = g.get("edgeDefinitions") or g.get("edge_definitions") or []
        vertex: set[str] = set()
        edges: set[str] = set()
        for ed in edge_defs:
            if not isinstance(ed, dict):
                continue
            coll = ed.get("collection")
            if coll:
                edges.add(coll)
            for v in ed.get("from") or []:
                vertex.add(v)
            for v in ed.get("to") or []:
                vertex.add(v)
        orphans = g.get("orphanCollections") or g.get("orphan_collections") or []
        for o in orphans:
            vertex.add(o)
        graphs.append(
            {
                "name": name,
                "edgeCollections": sorted(edges),
                "vertexCollections": sorted(vertex),
                "orphanCollections": sorted(orphans),
                "collectionCount": len(vertex | edges),
            }
        )

    graphs.sort(key=lambda g: str(g["name"]))
    log_endpoint_timing(
        "/graphs",
        round((time.perf_counter() - t0) * 1000, 1),
        count=len(graphs),
    )
    return {"graphs": graphs}


@app.post("/session/graph")
def bind_session_graph(
    req: BindGraphRequest,
    session: _Session = Depends(_get_session),
):
    """Bind (or clear) the session's ArangoDB named-graph scope.

    ``graphName: null`` clears the scope back to "all collections". A
    non-null name is validated against the live database so a typo is a
    clean 404 rather than a silently-empty scoped schema. The bound name
    is consumed by the schema routes on the next introspect /
    force-reacquire to down-select the mapping bundle.
    """
    t0 = time.perf_counter()
    graph_name = req.graphName or None
    if graph_name is not None:
        try:
            exists = session.db.has_graph(graph_name)
        except Exception as exc:  # noqa: BLE001 — treat lookup failure as unknown
            _svc_logger.info("has_graph(%r) failed: %s", tenant_name(graph_name), exc)
            exists = False
        if not exists:
            log_endpoint_timing(
                "/session/graph",
                round((time.perf_counter() - t0) * 1000, 1),
                status="error",
                bound=False,
            )
            raise HTTPException(
                status_code=404,
                detail={"error": "unknown_graph", "graphName": graph_name},
            )
    session.graph_name = graph_name
    log_endpoint_timing(
        "/session/graph",
        round((time.perf_counter() - t0) * 1000, 1),
        bound=graph_name is not None,
    )
    return {"graph_name": graph_name, "bound": graph_name is not None}


@app.get("/connections")
def list_connections(_auth: _Session | None = Depends(_require_session_in_public_mode)):
    """List active sessions (admin / debug). Requires auth in public mode."""
    t0 = time.perf_counter()
    _prune_expired()
    payload = {
        "active": len(_sessions),
        "sessions": [
            {
                "token_prefix": s.token[:8] + "...",
                "created_at": s.created_at,
                "last_used": s.last_used,
                "expired": s.expired,
            }
            for s in _sessions.values()
        ],
    }
    log_endpoint_timing(
        "/connections",
        round((time.perf_counter() - t0) * 1000, 1),
        active=payload["active"],
    )
    return payload


@app.get("/connect/defaults")
def connect_defaults():
    """Return ``.env`` default values for pre-filling the connect dialog.

    Builds a URL from ``ARANGO_URL`` (preferred) or ``ARANGO_HOST`` /
    ``ARANGO_PORT`` / ``ARANGO_PROTOCOL``. The password is omitted by
    default — the field is still present so the UI's connect dialog can
    bind against it but the value is the empty string so a curious
    anonymous caller can't pull a credential out of the .env on a
    single-user dev box. Operators who want the legacy "auto-fill the
    password" convenience set ``ARANGO_SPARQL_EXPOSE_DEFAULTS_PASSWORD=1``.

    Disabled entirely when ``ARANGO_SPARQL_PUBLIC_MODE=true``.
    """
    if _PUBLIC_MODE:
        raise HTTPException(status_code=404, detail="Not available in public mode")

    t0 = time.perf_counter()
    arango_url = read_arango_url(caller="connect_defaults") or ""
    if not arango_url:
        # ``ARANGO_HOST`` / ``ARANGO_PORT`` / ``ARANGO_PROTOCOL`` only
        # exist on ``arango-sparql-py`` historically — they have no
        # canonical/legacy split, so the direct ``os.getenv`` reads are
        # the right shape here. ``_env.read_arango_url`` is the single
        # site that reconciles ``ARANGO_URL`` (canonical) with any
        # future legacy alias; the host-port composer below stays as a
        # fallback for operators who configure piecewise.
        host = os.getenv("ARANGO_HOST", "localhost")
        port = os.getenv("ARANGO_PORT", "8529")
        protocol = os.getenv("ARANGO_PROTOCOL", "http")
        arango_url = f"{protocol}://{host}:{port}"

    expose_pw = os.getenv("ARANGO_SPARQL_EXPOSE_DEFAULTS_PASSWORD", "").lower() in (
        "1",
        "true",
        "yes",
    )
    if expose_pw:
        # ``read_arango_password`` returns ``None`` when neither
        # ``ARANGO_PASSWORD`` nor ``ARANGO_PASS`` is set; collapse to the
        # empty string so the JSON shape stays str-typed for the UI.
        password_value = read_arango_password(caller="connect_defaults") or ""
    else:
        password_value = ""
    payload = {
        "url": arango_url.rstrip("/"),
        "database": read_arango_database(default="_system", caller="connect_defaults") or "_system",
        "username": read_arango_username(default="root", caller="connect_defaults") or "root",
        "password": password_value,
    }
    log_endpoint_timing(
        "/connect/defaults",
        round((time.perf_counter() - t0) * 1000, 1),
        expose_pw=expose_pw,
    )
    return payload
