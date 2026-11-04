"""Routes and the request gate for running under a governance control plane.

See :mod:`arango_sparql.service.control_plane`. Off unless ``CONTROL_PLANE_URL`` is set, so
a standalone deployment is unchanged.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse
from starlette.routing import Match, Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import control_plane
from ..app import app
from ..security import control_plane_identity


def _route_key(scope: Scope) -> tuple[str, str] | None:
    """``(method, path template)`` of the route a request reaches, or ``None`` when it
    reaches no route but the page's static files (or nothing at all).

    Matched as the router matches, so a ``ROOT_PATH`` prefix is handled the same way.
    A route that matches the path but not the method counts: the request is classified by
    what it asks for, and the route answers 405 if it gets that far.
    """
    method = "GET" if scope["method"] == "HEAD" else scope["method"]
    for route in app.router.routes:
        if isinstance(route, Mount):
            continue
        match, _ = route.matches(scope)
        if match is not Match.NONE:
            return method, getattr(route, "path", "")
    return None


class ControlPlaneGate:
    """Every request that reaches a route is open, refused, or needs a verified context.

    A route on none of the lists is refused: a route added later stays closed under a
    control plane until someone classifies it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not control_plane.mode():
            await self.app(scope, receive, send)
            return
        key = _route_key(scope)
        if key is None or key in control_plane.OPEN:
            await self.app(scope, receive, send)
            return
        if sum(name.lower() == b"authorization" for name, _ in scope.get("headers", [])) > 1:
            # The gate and the rate limits must read the same credential.
            await _deny(400, "A request carries one Authorization header")(scope, receive, send)
            return
        refusal = control_plane.REFUSED.get(key)
        if refusal is None and key not in control_plane.SERVED:
            refusal = "this route is not classified for use under a control plane"
        if refusal is not None:
            await _deny(403, f"Not available in a project's context: {refusal}")(scope, receive, send)
            return
        try:
            identity = control_plane.verify(_headers(scope))
        except control_plane.ContextUnavailable as exc:
            await _deny(503, f"Cannot verify the project's context: {exc}")(scope, receive, send)
            return
        except control_plane.ContextRejected as exc:
            await _deny(401, f"Invalid project context: {exc}")(scope, receive, send)
            return
        scope.setdefault("state", {})["control_plane_identity"] = identity
        await self.app(scope, receive, send)


def _headers(scope: Scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def _deny(status: int, detail: str) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse({"detail": detail}, status_code=status, headers=headers)


# Fail at startup, not on the first request: a deployment with no tenant or source pinned,
# or without the SDK, would otherwise refuse everyone, or serve the wrong database.
if control_plane.mode() and (_problem := control_plane.configuration_problem()) is not None:
    raise RuntimeError(f"Refusing to start: {_problem}")

app.add_middleware(ControlPlaneGate)


@app.get("/control-plane/mode")
def control_plane_mode() -> dict[str, Any]:
    """Whether the service runs as a control plane's tool, and where a user opens it from.
    The page asks this first, before it has a context."""
    on = control_plane.mode()
    return {"control_plane": on, "home": control_plane.home() if on else None}


@app.get("/control-plane/context")
def control_plane_context(request: Request) -> dict[str, Any]:
    """The context this tab is in: who, which project, and the database it queries."""
    if not control_plane.mode():
        raise HTTPException(status_code=404, detail="Not running under a control plane")
    identity = control_plane_identity(request)
    binding: str | None = None
    database: str | None = None
    problem: str | None = None
    try:
        binding, database = control_plane.database_for(identity)
    except control_plane.NoDatabase as exc:
        problem = str(exc)
    return {
        "principal": identity.principal,
        "roles": list(identity.roles),
        "tenant": identity.tenant,
        "project": identity.project,
        "binding": binding,
        "database": database,
        "problem": problem,
        "expires_at": identity.expires_at,
    }
