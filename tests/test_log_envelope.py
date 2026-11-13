"""The service's log lines: format, envelope, stream and privacy fields.

PRD §9.5 (structured logs, JSON envelope, stdout), §17.2 (hashed session id,
tenant identifiers behind ``ARANGO_SPARQL_LOG_INCLUDE_TENANT``) and §3.14's
log-envelope acceptance test. Lines are read from the real handler
:func:`configure_observability` installs (``tests/helpers/log_capture.py``),
not from ``caplog``, which would bypass the formatter and the filters.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arango_sparql import __version__
from arango_sparql._privacy import tenant_name
from arango_sparql.service import app, observability
from tests.helpers.log_capture import captured_service_logs, emit_access_line, service_handler

SESSION_TOKEN = "tok-3f9a1c7e-5b2d-4e8f-9a6c-1d2e3f4a5b6c"
HASHED = hashlib.sha256(SESSION_TOKEN.encode()).hexdigest()[:12]
TRANSLATE_BODY = {"sparql": "SELECT ?s WHERE { ?s ?p ?o } LIMIT 1"}


def _lines(buffer) -> list[str]:
    return [line for line in buffer.getvalue().splitlines() if line.strip()]


def _json_lines(buffer) -> list[dict]:
    return [json.loads(line) for line in _lines(buffer)]


def _timing(records: list[dict], endpoint: str) -> dict:
    found = [r for r in records if r.get("msg") == "endpoint_timing" and r.get("endpoint") == endpoint]
    assert found, f"no endpoint_timing line for {endpoint}: {records}"
    return found[-1]


# --- format and stream -------------------------------------------------------------------


def test_json_mode_writes_one_json_object_per_line_with_the_envelope(monkeypatch) -> None:
    with captured_service_logs(monkeypatch, json_mode=True) as buf:
        resp = TestClient(app).post("/translate", json=TRANSLATE_BODY, headers={"X-Request-Id": "req-178"})
    assert resp.status_code in (200, 422), resp.text
    records = _json_lines(buf)  # every line parses: a plain-text line would raise here
    line = _timing(records, "/translate")
    for key in (
        "ts",
        "level",
        "logger",
        "correlation_id",
        "msg",
        "service",
        "version",
        "endpoint",
        "elapsed_ms",
    ):
        assert key in line, f"{key} missing from {line}"
    assert line["service"] == "arango-sparql-py"
    assert line["version"] == __version__
    assert line["correlation_id"] == "req-178"
    assert line["ts"].endswith("Z")


def test_a_record_field_never_overwrites_an_envelope_field(monkeypatch) -> None:
    with captured_service_logs(monkeypatch, json_mode=True) as buf:
        logging.getLogger("arango_sparql.test").info("hello", extra={"service": "spoofed", "version": "0"})
    (line,) = _json_lines(buf)
    assert line["service"] == "arango-sparql-py"
    assert line["version"] == __version__


def test_default_mode_writes_key_value_lines(monkeypatch) -> None:
    with captured_service_logs(monkeypatch, json_mode=False) as buf:
        TestClient(app).post("/translate", json=TRANSLATE_BODY)
    lines = [line for line in _lines(buf) if "endpoint_timing" in line]
    assert lines, buf.getvalue()
    assert "endpoint=" in lines[-1] and "correlation_id=" in lines[-1]
    with pytest.raises(json.JSONDecodeError):
        json.loads(lines[-1])


def test_logs_go_to_stdout(monkeypatch) -> None:
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    with captured_service_logs(monkeypatch, json_mode=True, keep_stream=True):
        assert service_handler().stream is stdout
        logging.getLogger("arango_sparql.test").info("to stdout")
        emit_access_line("/health")
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [line["logger"] for line in lines] == ["arango_sparql.test", "uvicorn.access"]


def test_main_py_keeps_the_service_handler_on_uvicorns_loggers(monkeypatch) -> None:
    """``main.py`` imports the app before ``uvicorn.run``; uvicorn's default
    logging config would then put its own formatter back (#181 review). It
    passes ``log_config=None``, with which uvicorn only sets levels."""
    import runpy

    import uvicorn

    import arango_sparql.arango_admin as admin

    calls: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(admin, "maybe_bootstrap_configured_database", lambda: None)
    runpy.run_path(str(Path(__file__).resolve().parent.parent / "main.py"), run_name="__main__")
    (kwargs,) = calls
    assert kwargs["log_config"] is None

    with captured_service_logs(monkeypatch, json_mode=True) as buf:
        ours = service_handler()
        uvicorn.Config("arango_sparql.service:app", **kwargs)  # runs uvicorn's configure_logging
        for name in observability._UVICORN_LOGGERS:
            assert logging.getLogger(name).handlers == [ours], name
            assert logging.getLogger(name).level == logging.INFO, name
        emit_access_line("/health")
    assert json.loads(buf.getvalue())["logger"] == "uvicorn.access"


@pytest.mark.parametrize("json_mode", [True, False])
def test_uvicorn_access_and_error_lines_use_the_service_format(monkeypatch, json_mode: bool) -> None:
    with captured_service_logs(monkeypatch, json_mode=json_mode) as buf:
        emit_access_line("/health")
        logging.getLogger("uvicorn.error").error("worker boot failed")
    lines = _lines(buf)
    assert len(lines) == 2, lines
    if json_mode:
        access, error = (json.loads(line) for line in lines)
        assert access["logger"] == "uvicorn.access" and "/health" in access["msg"]
        assert error["logger"] == "uvicorn.error" and error["level"] == "ERROR"
        assert access["service"] == error["service"] == "arango-sparql-py"
    else:
        assert "uvicorn.access" in lines[0] and "uvicorn.error" in lines[1]
    for name in observability._UVICORN_LOGGERS:
        assert logging.getLogger(name).propagate is False, f"{name} would print every line twice"


def test_access_line_redacts_credentials_and_query_text(monkeypatch) -> None:
    with captured_service_logs(monkeypatch, json_mode=True) as buf:
        emit_access_line("/sparql?query=SELECT%20%3Fsecret&session=tok-raw&default-graph-uri=g")
    (line,) = _json_lines(buf)
    assert "tok-raw" not in line["msg"] and "secret" not in line["msg"]
    assert "query=[redacted]" in line["msg"] and "session=[redacted]" in line["msg"]
    assert "default-graph-uri=g" in line["msg"]


# --- session id (PRD §17.2) --------------------------------------------------------------


def test_hash_session_token_is_the_first_12_hex_of_sha256() -> None:
    assert observability.hash_session_token(SESSION_TOKEN) == HASHED
    assert len(HASHED) == 12 and int(HASHED, 16) >= 0


@pytest.mark.parametrize(
    "headers, params",
    [
        ({"X-Arango-Session": SESSION_TOKEN}, None),
        ({"Authorization": f"Bearer {SESSION_TOKEN}"}, None),
        ({}, {"session": SESSION_TOKEN}),
    ],
    ids=["session-header", "bearer", "query-param"],
)
def test_lines_in_a_request_carry_the_hashed_session_id_never_the_token(monkeypatch, headers, params) -> None:
    with captured_service_logs(monkeypatch, json_mode=True) as buf:
        TestClient(app).post("/translate", json=TRANSLATE_BODY, headers=headers, params=params)
    line = _timing(_json_lines(buf), "/translate")
    assert line["session_id"] == HASHED
    assert SESSION_TOKEN not in buf.getvalue()
    assert SESSION_TOKEN[:8] not in buf.getvalue()


def test_no_session_id_without_a_token(monkeypatch) -> None:
    with captured_service_logs(monkeypatch, json_mode=True) as buf:
        TestClient(app).post("/translate", json=TRANSLATE_BODY)
    assert "session_id" not in _timing(_json_lines(buf), "/translate")


def test_lines_outside_a_request_carry_no_request_fields(monkeypatch) -> None:
    with captured_service_logs(monkeypatch, json_mode=True) as buf:
        logging.getLogger("arango_sparql.startup").info("booted")
    (line,) = _json_lines(buf)
    assert "session_id" not in line and "tenant" not in line
    assert line["correlation_id"] == "-"


# --- tenant identifiers (PRD §17.2) ------------------------------------------------------


@pytest.mark.parametrize(
    "public_mode, include_tenant, logged",
    [
        (False, None, True),  # default outside public mode: on
        (True, None, False),  # default in public mode: off
        (True, "true", True),  # the operator opts in
        (False, "false", False),  # the operator opts out
        (False, "maybe", False),  # not a boolean: fails closed
    ],
)
def test_tenant_identifiers_follow_the_switch(monkeypatch, public_mode, include_tenant, logged) -> None:
    with captured_service_logs(
        monkeypatch, json_mode=True, public_mode=public_mode, include_tenant=include_tenant
    ) as buf:
        TestClient(app).post("/translate", json=TRANSLATE_BODY, headers={"X-Tenant-Id": "acme-tenant"})
        logging.getLogger("arango_sparql.test").info(
            "db", extra={"database": "acme_db", "graph": "acme_graph", "collection": "acme_coll"}
        )
        # A name formatted into the message is marked at the call site instead.
        logging.getLogger("arango_sparql.test").warning("warm failed: db=%r", tenant_name("acme_msg_db"))
    output = buf.getvalue()
    if not logged:
        assert "'[tenant]'" in output, output
    for name in ("acme-tenant", "acme_db", "acme_graph", "acme_coll", "acme_msg_db"):
        assert (name in output) is logged, f"{name}: expected logged={logged}\n{output}"
    if logged:
        assert _timing(_json_lines(buf), "/translate")["tenant"] == "acme-tenant"


def test_an_unreadable_tenant_switch_is_reported(monkeypatch, caplog) -> None:
    monkeypatch.setenv("ARANGO_SPARQL_LOG_INCLUDE_TENANT", "maybe")
    with caplog.at_level(logging.WARNING, logger="arango_sparql"):
        assert observability.tenant_logging_enabled() is False
    assert any("ARANGO_SPARQL_LOG_INCLUDE_TENANT" in r.getMessage() for r in caplog.records)


def test_default_tenant_env_is_used_when_no_header(monkeypatch) -> None:
    monkeypatch.setenv("ARANGO_SPARQL_DEFAULT_TENANT", "env-tenant")
    with captured_service_logs(monkeypatch, json_mode=True) as buf:
        TestClient(app).post("/translate", json=TRANSLATE_BODY)
    assert _timing(_json_lines(buf), "/translate")["tenant"] == "env-tenant"


def test_nested_tenant_names_are_redacted_and_the_shared_record_is_not(monkeypatch) -> None:
    """Redaction happens on the formatter's copy: a name inside a container
    argument is caught, and other handlers see the record as it was logged."""
    seen: list[logging.LogRecord] = []

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    keep = _Keep()
    logger = logging.getLogger("arango_sparql.test.nested")
    with captured_service_logs(monkeypatch, json_mode=True, public_mode=True) as buf:
        logger.addHandler(keep)
        try:
            logger.warning(
                "dbs=%s", [tenant_name("acme_list_db"), "plain"], extra={"database": "acme_field_db"}
            )
        finally:
            logger.removeHandler(keep)
    (line,) = _json_lines(buf)
    assert "acme_list_db" not in line["msg"] and "[tenant]" in line["msg"] and "plain" in line["msg"]
    assert "database" not in line
    (record,) = seen
    assert record.args == (["acme_list_db", "plain"],)
    assert record.__dict__["database"] == "acme_field_db"
