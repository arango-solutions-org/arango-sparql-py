"""Capture what the service's log handler writes, under a chosen configuration.

The service logs through one handler that :func:`configure_observability`
installs on the ``arango_sparql`` logger and on uvicorn's access and error
loggers. :func:`captured_service_logs` re-runs that setup with the env vars a
test names, points the handler at a buffer, and afterwards puts every logger
back exactly as it was, so a test cannot change how later tests log.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from arango_sparql.service import observability

_LOGGERS = ("arango_sparql", *observability._UVICORN_LOGGERS)


@contextmanager
def captured_service_logs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    json_mode: bool,
    public_mode: bool = False,
    include_tenant: str | None = None,
) -> Iterator[io.StringIO]:
    """Yield a buffer receiving every line the service's handler writes.

    ``include_tenant`` is the raw ``ARANGO_SPARQL_LOG_INCLUDE_TENANT`` value, or
    ``None`` to leave it unset (the default follows public mode)."""
    monkeypatch.setenv("ARANGO_SPARQL_LOG_JSON", "1" if json_mode else "0")
    monkeypatch.setenv("ARANGO_SPARQL_PUBLIC_MODE", "true" if public_mode else "false")
    monkeypatch.setenv("ARANGO_SPARQL_LOG_LEVEL", "DEBUG")
    if include_tenant is None:
        monkeypatch.delenv("ARANGO_SPARQL_LOG_INCLUDE_TENANT", raising=False)
    else:
        monkeypatch.setenv("ARANGO_SPARQL_LOG_INCLUDE_TENANT", include_tenant)

    saved = {
        name: (
            list(logging.getLogger(name).handlers),
            list(logging.getLogger(name).filters),
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
        )
        for name in _LOGGERS
    }
    buffer = io.StringIO()
    try:
        # uvicorn's startup config (uvicorn.config.LOGGING_CONFIG) sets both of its
        # loggers to INFO before it imports the app; no uvicorn runs under
        # TestClient, so set what it would have.
        for name in observability._UVICORN_LOGGERS:
            logging.getLogger(name).setLevel(logging.INFO)
        observability.configure_observability(force=True)
        handler = service_handler()
        handler.setStream(buffer)
        yield buffer
    finally:
        for name, (handlers, filters, level, propagate) in saved.items():
            logger = logging.getLogger(name)
            for h in list(logger.handlers):
                logger.removeHandler(h)
            for f in list(logger.filters):
                logger.removeFilter(f)
            for h in handlers:
                logger.addHandler(h)
            for f in filters:
                logger.addFilter(f)
            logger.setLevel(level)
            logger.propagate = propagate


def service_handler() -> logging.StreamHandler:
    """The handler :func:`configure_observability` installed on ``arango_sparql``."""
    handlers = [
        h for h in logging.getLogger("arango_sparql").handlers if isinstance(h, logging.StreamHandler)
    ]
    assert len(handlers) == 1, f"expected one service handler, found {handlers}"
    return handlers[0]


def emit_access_line(path: str, *, method: str = "GET", status: int = 200) -> None:
    """Log one line exactly as uvicorn's ``httptools``/``h11`` protocols do."""
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "10.0.0.1:5000", method, path, "1.1", status
    )
