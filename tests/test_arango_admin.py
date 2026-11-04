"""Unit tests for :mod:`arango_sparql.arango_admin` — database
provisioning (``ensure_database`` + ``ensure_configured_database``).

No live ArangoDB: a fake client records ``has_database`` /
``create_database`` calls so the create-if-missing logic and the
best-effort env-driven wrapper are covered deterministically.
"""

from __future__ import annotations

from typing import Any

import pytest

from arango_sparql.arango_admin import (
    ensure_configured_database,
    ensure_database,
    maybe_bootstrap_configured_database,
)


class _FakeSystemDb:
    def __init__(self, existing: set[str]) -> None:
        self._existing = existing
        self.created: list[str] = []

    def has_database(self, name: str) -> bool:
        return name in self._existing

    def create_database(self, name: str) -> None:
        self.created.append(name)
        self._existing.add(name)


class _FakeClient:
    def __init__(self, existing: set[str] | None = None) -> None:
        self.sys_db = _FakeSystemDb(existing or set())
        self.db_calls: list[tuple[str, str, str]] = []
        self.closed = False

    def db(self, name: str, username: str = "", password: str = "") -> _FakeSystemDb:
        self.db_calls.append((name, username, password))
        return self.sys_db

    def close(self) -> None:
        self.closed = True


def test_ensure_database_creates_when_missing() -> None:
    client = _FakeClient(existing=set())
    created = ensure_database(client, "demo", username="root", password="pw")
    assert created is True
    assert client.sys_db.created == ["demo"]
    # The catalogue connection must target ``_system`` with the creds.
    assert client.db_calls == [("_system", "root", "pw")]


def test_ensure_database_is_noop_when_present() -> None:
    client = _FakeClient(existing={"demo"})
    created = ensure_database(client, "demo", username="root", password="pw")
    assert created is False
    assert client.sys_db.created == []


@pytest.mark.parametrize("reserved", ["_system", ""])
def test_ensure_database_rejects_reserved_names(reserved: str) -> None:
    client = _FakeClient()
    with pytest.raises(ValueError, match="reserved/empty"):
        ensure_database(client, reserved, username="root", password="pw")
    assert client.sys_db.created == []


def _set_env(monkeypatch: pytest.MonkeyPatch, **values: str | None) -> None:
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def test_ensure_configured_database_created(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(
        monkeypatch,
        ARANGO_DB="sparql-to-aql",
        ARANGO_URL="http://localhost:8529",
        ARANGO_USER="root",
        ARANGO_PASSWORD="pw",
    )
    client = _FakeClient(existing=set())
    status = ensure_configured_database(client_factory=lambda hosts: client)
    assert status == "created"
    assert client.sys_db.created == ["sparql-to-aql"]
    assert client.closed is True


def test_ensure_configured_database_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, ARANGO_DB="sparql-to-aql", ARANGO_PASSWORD="pw")
    client = _FakeClient(existing={"sparql-to-aql"})
    status = ensure_configured_database(client_factory=lambda hosts: client)
    assert status == "exists"
    assert client.sys_db.created == []


@pytest.mark.parametrize("db_name", [None, "_system"])
def test_ensure_configured_database_skips_reserved(
    monkeypatch: pytest.MonkeyPatch, db_name: str | None
) -> None:
    _set_env(monkeypatch, ARANGO_DB=db_name)

    def _factory(hosts: str) -> Any:  # pragma: no cover - must not be called
        raise AssertionError("client factory should not be called for reserved DB")

    assert ensure_configured_database(client_factory=_factory) is None


def test_ensure_configured_database_swallows_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch, ARANGO_DB="sparql-to-aql", ARANGO_PASSWORD="pw")

    def _factory(hosts: str) -> Any:
        raise ConnectionError("ArangoDB unreachable")

    # Best-effort contract: returns None, never raises.
    assert ensure_configured_database(client_factory=_factory) is None


# ---------------------------------------------------------------------------
# maybe_bootstrap_configured_database — the gated boot-path wrapper.
# ---------------------------------------------------------------------------


def _spy_factory(client: _FakeClient):
    return lambda hosts: client


def test_maybe_bootstrap_runs_in_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(
        monkeypatch,
        ARANGO_DB="sparql-to-aql",
        ARANGO_PASSWORD="pw",
        ARANGO_SPARQL_PUBLIC_MODE=None,
        ARANGO_SPARQL_SKIP_DB_BOOTSTRAP=None,
    )
    client = _FakeClient(existing=set())
    status = maybe_bootstrap_configured_database(client_factory=_spy_factory(client))
    assert status == "created"
    assert client.sys_db.created == ["sparql-to-aql"]


def test_maybe_bootstrap_skips_in_public_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(
        monkeypatch,
        ARANGO_DB="sparql-to-aql",
        ARANGO_SPARQL_PUBLIC_MODE="true",
        ARANGO_SPARQL_SKIP_DB_BOOTSTRAP=None,
    )

    def _factory(hosts: str) -> Any:  # pragma: no cover - must not be called
        raise AssertionError("public mode must not provision a database")

    assert maybe_bootstrap_configured_database(client_factory=_factory) is None


def test_maybe_bootstrap_skips_under_a_control_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(
        monkeypatch,
        ARANGO_DB="sparql-to-aql",
        ARANGO_SPARQL_PUBLIC_MODE=None,
        ARANGO_SPARQL_SKIP_DB_BOOTSTRAP=None,
        CONTROL_PLANE_URL="http://control-plane:8000/api",
    )

    def _factory(hosts: str) -> Any:  # pragma: no cover - must not be called
        raise AssertionError("under a control plane the databases are the projects'")

    assert maybe_bootstrap_configured_database(client_factory=_factory) is None


def test_maybe_bootstrap_skips_when_opted_out(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(
        monkeypatch,
        ARANGO_DB="sparql-to-aql",
        ARANGO_SPARQL_PUBLIC_MODE=None,
        ARANGO_SPARQL_SKIP_DB_BOOTSTRAP="1",
    )

    def _factory(hosts: str) -> Any:  # pragma: no cover - must not be called
        raise AssertionError("opt-out must not provision a database")

    assert maybe_bootstrap_configured_database(client_factory=_factory) is None
