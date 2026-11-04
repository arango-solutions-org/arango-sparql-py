"""The service as a project's query tool under a governance control plane.

When ``CONTROL_PLANE_URL`` is set, the control plane decides who a request is for. It signs a
short-lived context token for one user in one project, and the page sends it on every call as
``Authorization: Bearer``. In this mode:

* the context token is the only credential: ``/connect``, ``/connect/platform`` and every
  other way of opening a session are refused, and so is a token in the query string or the
  session header;
* a session opens on the project's database binding, with the deployment's own credential
  (``ARANGO_URL``, ``ARANGO_USER``, ``ARANGO_PASSWORD``), never one a caller supplies;
* the deployment serves one tenant (``ARANGO_SPARQL_TENANT``) and one source
  (``ARANGO_SPARQL_SOURCE``, the control plane's id for the ArangoDB deployment that
  credential reaches); a context for another tenant is refused, and a binding on another
  source is never served from this one;
* querying reads: a route that runs caller-written AQL, or shows or changes how the service
  connects, is refused, and a route not classified below is refused until it is.

The control plane's SDK (``control_plane_sdk``) verifies the token, offline, against the
control plane's published keys. It is imported only in this mode, so the service installs
and runs without it otherwise; everything else talks to the small :class:`ControlPlane`
interface, which keeps the service testable without the SDK.
"""

from __future__ import annotations

import importlib
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

#: This tool's id, as the control plane registers it for a project.
TOOL_ID = "query"

#: Routes served without a context: health checks, and what the page needs before it has
#: one (whether this mode is on, and where to send a user who arrived without a context).
OPEN: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/health"),
        ("GET", "/health/ready"),
        ("GET", "/control-plane/mode"),
    }
)

#: Routes refused in this mode, and why. Everything a session needs comes from the context.
REFUSED: dict[tuple[str, str], str] = {
    ("POST", "/connect"): "a session opens from the project's context",
    ("GET", "/connect/platform"): "a session opens from the project's context",
    ("POST", "/connect/platform"): "a session opens from the project's context",
    ("GET", "/connect/platform/diagnostics"): "a session opens from the project's context",
    ("POST", "/disconnect"): "the session belongs to the project's context",
    ("GET", "/connections"): "it lists other users' sessions",
    ("GET", "/connect/defaults"): "it shows the deployment's own connection",
    ("POST", "/execute-aql"): "caller-written AQL can change the data",
}

#: Routes served to a verified context. Each reads the project's database or touches no
#: database at all; in this mode the schema cache stays in memory (``routes/schema.py``), so
#: refreshing or re-analysing the schema writes nothing to the project's database.
SERVED: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/control-plane/context"),
        ("GET", "/openapi.json"),
        ("GET", "/docs"),
        ("GET", "/docs/oauth2-redirect"),
        ("GET", "/redoc"),
        ("GET", "/graphs"),
        ("POST", "/session/graph"),
        ("GET", "/examples"),
        ("POST", "/mapping/import-owl"),
        ("POST", "/mapping/export-owl"),
        ("POST", "/nl-translate"),
        ("POST", "/nl-explain"),
        ("POST", "/nl-execute"),
        ("POST", "/nl-samples"),
        ("GET", "/schema/introspect"),
        ("GET", "/schema/owl"),
        ("GET", "/schema/properties"),
        ("GET", "/schema/summary"),
        ("POST", "/schema/summary"),
        ("GET", "/schema/statistics"),
        ("GET", "/schema/status"),
        ("POST", "/schema/invalidate-cache"),
        ("POST", "/schema/force-reacquire"),
        ("GET", "/sparql"),
        ("POST", "/sparql"),
        ("POST", "/translate"),
        ("POST", "/execute"),
        ("POST", "/explain"),
        ("POST", "/profile"),
        ("POST", "/validate"),
    }
)


class ContextRejected(Exception):  # noqa: N818 - mirrors the SDK's TokenRejected
    """The request's context token is missing, invalid, expired, or for another tenant."""


class ContextUnavailable(Exception):  # noqa: N818
    """The control plane's keys cannot be fetched, so no context can be verified."""


class NoDatabase(Exception):  # noqa: N818
    """The project names no database this deployment can open."""


@dataclass(frozen=True)
class Identity:
    """Who a verified context is for, and the project's database bindings."""

    principal: str
    roles: tuple[str, ...]
    tenant: str
    project: str
    #: Binding name to ``(source, database)``.
    databases: Mapping[str, tuple[str, str]]
    expires_at: int


class ControlPlane(Protocol):
    def verify(self, token: str) -> Identity:
        """Verify a context token offline. Raises :class:`ContextRejected`, or
        :class:`ContextUnavailable` when the control plane's keys cannot be fetched."""


def mode() -> bool:
    """True when the service runs as a control plane's tool."""
    return bool(os.environ.get("CONTROL_PLANE_URL", "").strip())


def tenant() -> str:
    return os.environ.get("ARANGO_SPARQL_TENANT", "").strip()


def source() -> str:
    return os.environ.get("ARANGO_SPARQL_SOURCE", "").strip()


def home() -> str:
    """Where a user who arrived without a context opens the project from."""
    return os.environ.get("CONTROL_PLANE_HOME", "").strip() or "/"


def configuration_problem() -> str | None:
    """Why the service cannot run in this mode, or ``None``. Checked at startup, so a
    deployment without a tenant or source fails before it serves anyone."""
    if not tenant():
        return (
            "CONTROL_PLANE_URL is set but ARANGO_SPARQL_TENANT is not: name the tenant this deployment serves"
        )
    if not os.environ.get("ARANGO_USER", "").strip():
        return (
            "CONTROL_PLANE_URL is set but ARANGO_USER is not: name the database user the service "
            "reads the projects' databases as (a read-only user is enough)"
        )
    if not source():
        return (
            "CONTROL_PLANE_URL is set but ARANGO_SPARQL_SOURCE is not: name the control plane's "
            "source that ARANGO_URL reaches"
        )
    try:
        importlib.import_module("control_plane_sdk")
    except ImportError:
        return (
            "CONTROL_PLANE_URL is set but the control plane's Python SDK (control_plane_sdk) is not installed"
        )
    return None


def token_from(headers: Mapping[str, str]) -> str | None:
    """The context token a request carries, as ``Authorization: Bearer``. Never the query
    string, where it would be written to access logs, and never the session header: one
    place for the one credential."""
    scheme, _, value = headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return None


def database_for(identity: Identity) -> tuple[str, str]:
    """The binding this deployment serves for the project: ``(name, database)``.

    Only a binding on this deployment's source is served; a database of the same name on
    another source is a different database. Raises :class:`NoDatabase` when the project has
    no such binding, or more than one (the page cannot choose between them yet).
    """
    here = sorted((name, db) for name, (src, db) in identity.databases.items() if src == source())
    if not here:
        raise NoDatabase(f"project {identity.project!r} has no database on source {source()!r}")
    if len(here) > 1:
        names = ", ".join(name for name, _ in here)
        raise NoDatabase(
            f"project {identity.project!r} has several databases on source {source()!r}: {names}"
        )
    return here[0]


class _Sdk:
    """The control plane's SDK behind :class:`ControlPlane`."""

    def __init__(self, url: str) -> None:
        self._sdk = importlib.import_module("control_plane_sdk")
        self._client = self._sdk.ControlPlane(url, tool=TOOL_ID)

    def verify(self, token: str) -> Identity:
        try:
            verified = self._client.verify(token)
        except self._sdk.Unavailable as exc:
            raise ContextUnavailable(str(exc)) from exc
        except self._sdk.ControlPlaneError as exc:
            raise ContextRejected(str(exc)) from exc
        ctx: Any = verified.context
        return Identity(
            principal=ctx.principal.id,
            roles=tuple(ctx.principal.roles),
            tenant=ctx.tenant,
            project=ctx.project,
            databases={name: (b.source, b.database) for name, b in ctx.databases.items()},
            expires_at=int(verified.expires_at),
        )


_instance: ControlPlane | None = None
_lock = threading.Lock()


def get() -> ControlPlane:
    global _instance
    with _lock:
        if _instance is None:
            _instance = _Sdk(os.environ["CONTROL_PLANE_URL"])
        return _instance


def set_instance(instance: ControlPlane | None) -> None:
    """Replace the control plane (tests), or forget it so the next call builds one."""
    global _instance
    with _lock:
        _instance = instance


def verify(headers: Mapping[str, str]) -> Identity:
    """The verified context a request carries, for this deployment's tenant. Raises
    :class:`ContextRejected`."""
    token = token_from(headers)
    if token is None:
        raise ContextRejected("a context token is required: open this tool from your project")
    identity = get().verify(token)
    if identity.tenant != tenant():
        raise ContextRejected("the context is for another tenant")
    return identity
