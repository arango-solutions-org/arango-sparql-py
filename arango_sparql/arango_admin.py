"""ArangoDB administrative helpers shared by the service, the test
harness, and the demo-provisioning script.

The single concern here is **database provisioning**: ArangoDB never
auto-creates a database, so connecting to a configured ``ARANGO_DB`` that
doesn't exist yet fails with a "database not found". This module owns the
"create it if missing" logic in one place so the service startup hook,
the integration / W3C-live fixtures, and ``scripts/ensure_database.py``
all agree on the behaviour (mirrors how :mod:`arango_sparql._env`
centralises credential resolution).

Creating a database requires connecting to the ``_system`` database with
a user that has server-level rights (``root`` in the local-dev default),
because in ArangoDB the database catalogue lives in ``_system``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ._env import (
    read_arango_database,
    read_arango_password,
    read_arango_url,
    read_arango_username,
)
from ._privacy import tenant_name

_logger = logging.getLogger("arango_sparql")

# Databases that must never be auto-created (they always exist) and that
# the bootstrap should treat as "nothing to do". ``_system`` is the
# ArangoDB catalogue database; provisioning data into it is exactly what
# this whole feature steers operators away from.
_RESERVED_DATABASES = frozenset({"_system", ""})


def ensure_database(
    client: Any,
    name: str,
    *,
    username: str,
    password: str,
    system_database: str = "_system",
) -> bool:
    """Create database *name* if it does not already exist.

    Connects to *system_database* (``_system`` by default) with the
    supplied credentials — the catalogue that owns ``has_database`` /
    ``create_database`` — and creates *name* when it is absent.

    Returns ``True`` when the database was created, ``False`` when it
    already existed (so callers can log "created" vs "exists"). Raises
    whatever the driver raises on connection / permission failure; the
    best-effort wrapper :func:`ensure_configured_database` is the layer
    that swallows those for the service-startup path.

    Parameters
    ----------
    client:
        An ``arango.ArangoClient`` (or a test double exposing the same
        ``.db(...).has_database/create_database`` surface).
    name:
        The database to ensure. ``_system`` / empty are rejected — they
        either always exist or are never a valid target.
    username, password:
        Credentials with rights on *system_database*.
    """
    if name in _RESERVED_DATABASES:
        raise ValueError(
            f"refusing to ensure reserved/empty database name {name!r}; "
            "_system always exists and is not a valid provisioning target"
        )
    sys_db = client.db(system_database, username=username, password=password)
    if sys_db.has_database(name):
        return False
    sys_db.create_database(name)
    return True


def ensure_configured_database(
    *,
    client_factory: Any = None,
    logger: logging.Logger | None = None,
) -> str | None:
    """Best-effort: ensure the ``ARANGO_DB`` from the environment exists.

    Reads ``ARANGO_URL`` / ``ARANGO_DB`` / ``ARANGO_USER`` /
    ``ARANGO_PASSWORD`` via :mod:`arango_sparql._env` and creates the
    configured database if it is missing. Designed for the service
    startup hook and the provisioning script, so **every failure is
    swallowed and logged** rather than raised — a transient ArangoDB
    outage must never stop the translation-only service from booting.

    Returns one of:

    * ``"created"`` — the database did not exist and was created.
    * ``"exists"``  — the database was already present.
    * ``None``      — nothing was attempted (no/``_system`` ``ARANGO_DB``)
      or the attempt failed (logged at WARNING).

    Parameters
    ----------
    client_factory:
        Optional ``(hosts: str) -> ArangoClient`` callable, injected by
        tests. Defaults to the real :class:`arango.ArangoClient`.
    logger:
        Logger to report through; defaults to the ``arango_sparql``
        logger.
    """
    log = logger or _logger
    name = read_arango_database(caller="ensure_configured_database")
    if not name or name in _RESERVED_DATABASES:
        # No explicit database, or it's ``_system`` — nothing to
        # provision. This is the common case for translation-only
        # deployments, so it is silent (debug, not warning).
        log.debug("ensure_configured_database: ARANGO_DB=%r needs no provisioning", name)
        return None

    url = (
        read_arango_url(default="http://localhost:8529", caller="ensure_configured_database") or ""
    ).rstrip("/")
    username = read_arango_username(default="root", caller="ensure_configured_database") or "root"
    password = read_arango_password(caller="ensure_configured_database") or ""

    try:
        if client_factory is None:
            from arango import ArangoClient

            client_factory = ArangoClient
        client = client_factory(hosts=url)
        try:
            created = ensure_database(client, name, username=username, password=password)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
    except Exception as exc:  # noqa: BLE001 - best-effort by contract
        log.warning(
            "ensure_configured_database: could not provision database %r at %s "
            "(%s: %s). The service will still serve translation-only requests; "
            "create the database manually or check ArangoDB connectivity before "
            "using /connect or /execute.",
            tenant_name(name),
            url,
            type(exc).__name__,
            exc,
        )
        return None

    if created:
        log.info("ensure_configured_database: created database %r at %s", tenant_name(name), url)
        return "created"
    log.info("ensure_configured_database: database %r already exists at %s", tenant_name(name), url)
    return "exists"


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def maybe_bootstrap_configured_database(
    *,
    client_factory: Any = None,
    logger: logging.Logger | None = None,
) -> str | None:
    """Gated wrapper around :func:`ensure_configured_database` for the
    service boot path (``main.py``).

    Provisioning the configured ``ARANGO_DB`` on boot is a local-dev /
    demo convenience, so it is **suppressed** when either:

    * ``ARANGO_SPARQL_PUBLIC_MODE`` is truthy — a public deployment must
      not have its boot path creating databases; operators provision
      those explicitly (matching the public-mode posture in
      ``service/app.py``), or
    * ``ARANGO_SPARQL_SKIP_DB_BOOTSTRAP`` is truthy — an explicit opt-out
      for operators who manage provisioning out-of-band, or
    * ``CONTROL_PLANE_URL`` is set — under a governance control plane the
      databases are the projects', named by their contexts (PRD §8.7).

    Returns the :func:`ensure_configured_database` status
    (``"created"`` / ``"exists"`` / ``None``), or ``None`` when gated off.
    Never raises — boot must proceed even if provisioning is impossible.
    """
    log = logger or _logger
    if _is_truthy(os.getenv("ARANGO_SPARQL_PUBLIC_MODE")):
        log.debug("DB bootstrap skipped: public mode is enabled.")
        return None
    if _is_truthy(os.getenv("ARANGO_SPARQL_SKIP_DB_BOOTSTRAP")):
        log.info("DB bootstrap skipped: ARANGO_SPARQL_SKIP_DB_BOOTSTRAP is set.")
        return None
    if os.getenv("CONTROL_PLANE_URL", "").strip():
        log.info("DB bootstrap skipped: running under a control plane.")
        return None
    return ensure_configured_database(client_factory=client_factory, logger=log)
