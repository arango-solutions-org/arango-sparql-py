"""``:p*`` / ``:p+`` over an edge collection, walked to the end, on ArangoDB.

The cases of ``tests/cross/test_edge_closure_cross.py`` on a live server, in
a database of their own that is dropped afterwards: the 50-hop chain answered
in full, cycles back to the start, shared descendants, a symmetric property,
a tenant whose walk must not pass another tenant's vertex, and named graphs.
Live also shows what the interpreter can only model: that ArangoDB's
``uniqueVertices: "global"`` walk, its ``PRUNE`` at depth 0 and the
edge-count check behave as ``paths._emit_edge_closure_arm`` relies on, and
it runs ``ASK``. Gated like the rest of the integration suite
(``RUN_INTEGRATION=1``).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest

from arango_sparql.errors import ARANGO_ERROR_QUERY_FAIL_CALLED, PATH_DEPTH_EXCEEDED_CODE
from arango_sparql.translate import paths
from tests.cross.test_edge_closure_cross import (
    _SHARED_LABELS,
    CHAIN,
    DIAMOND,
    GRAPH_CASES,
    LOOPS,
    TENANT,
    _cases,
)
from tests.cross.test_multi_document_iri_cross import EX, Store, oracle_rows, translate_case
from tests.helpers.oxi import assert_bindings_equal, drop_null_bindings
from tests.integration.conftest import (
    DEFAULT_ARANGO_PASSWORD,
    DEFAULT_ARANGO_URL,
    DEFAULT_ARANGO_USER,
    arangodb_reachable,
    integration_enabled,
    try_boot_arangodb_via_compose,
)

pytestmark = pytest.mark.integration

pytest.importorskip("pyoxigraph", reason="pyoxigraph is the ground truth for these tests")

DATABASE = "sparql_edge_closure_live"


@pytest.fixture(scope="module")
def db() -> Iterator[Any]:
    if not integration_enabled():
        pytest.skip("set RUN_INTEGRATION=1 to enable integration tests")
    if not arangodb_reachable() and not try_boot_arangodb_via_compose():
        pytest.skip(f"ArangoDB at {DEFAULT_ARANGO_URL} is unreachable and could not be booted")
    from arango import ArangoClient

    client = ArangoClient(hosts=DEFAULT_ARANGO_URL)
    sys_db = client.db("_system", username=DEFAULT_ARANGO_USER, password=DEFAULT_ARANGO_PASSWORD)
    if sys_db.has_database(DATABASE):
        sys_db.delete_database(DATABASE)
    sys_db.create_database(DATABASE)
    try:
        yield client.db(DATABASE, username=DEFAULT_ARANGO_USER, password=DEFAULT_ARANGO_PASSWORD)
    finally:
        sys_db.delete_database(DATABASE, ignore_missing=True)
        client.close()


_LOADED: dict[str, Store | None] = {"store": None}


def load(db: Any, store: Store) -> None:
    """Replace the database's collections with *store*'s documents."""
    if _LOADED["store"] is store:
        return
    for collection in db.collections():
        if not collection["system"]:
            db.delete_collection(collection["name"])
    for name, docs in store.docs.items():
        edge = any("_from" in d for d in docs)
        db.create_collection(name, edge=edge).insert_many(
            [{**{k: v for k, v in d.items() if k != "_id"}, "_key": d["_id"].split("/", 1)[1]} for d in docs]
        )
    _LOADED["store"] = store


def run(db: Any, store: Store, query: str, **overrides: Any) -> list[Any]:
    load(db, store)
    result = translate_case(store, query)
    bind_vars = dict(result.bind_vars)
    for suffix, value in overrides.items():
        (name,) = [k for k in bind_vars if re.fullmatch(rf"_p\d+_{suffix}", k)]
        bind_vars[name] = value
    return list(db.aql.execute(result.aql, bind_vars=bind_vars))


@pytest.mark.parametrize("store, query", [*_cases(), *GRAPH_CASES])
def test_the_walk_agrees_with_pyoxigraph(db: Any, store: Store, query: str) -> None:
    actual = [drop_null_bindings(r) for r in run(db, store, query)]
    assert_bindings_equal(oracle_rows(store, query), actual)


@pytest.mark.parametrize(
    "store, query, answer",
    [
        pytest.param(CHAIN, "ASK { :n0 :next+ :n50 }", True, id="fifty_hops"),
        pytest.param(CHAIN, "ASK { :n50 :next+ :n0 }", False, id="backwards"),
        pytest.param(CHAIN, "ASK { :n7 :next* :n7 }", True, id="zero_length"),
        pytest.param(LOOPS, "ASK { :n0 :next+ :n0 }", True, id="cycle_back_to_the_start"),
        pytest.param(LOOPS, "ASK { :n3 :next+ :n3 }", False, id="reached_but_no_cycle"),
        pytest.param(DIAMOND, "ASK { :n0 :next+ :n0 }", False, id="cycle_elsewhere"),
        pytest.param(TENANT, "ASK { :n0 :next+ :n7 }", False, id="only_through_another_tenant"),
        pytest.param(TENANT, "ASK { :n0 :next+ :n3 }", True, id="the_long_way_round"),
    ],
)
def test_ask(db: Any, store: Store, query: str, answer: bool) -> None:
    assert run(db, store, query) == [answer]


def test_the_walk_reaches_each_vertex_once(db: Any) -> None:
    # 50 * 51 / 2 pairs, each once: the walk does not repeat a vertex, and
    # the union keeps each pair once.
    rows = run(db, CHAIN, "SELECT ?s ?o WHERE { ?s :next+ ?o }")
    assert len(rows) == len({(r["s"], r["o"]) for r in rows}) == 1275


def test_an_edge_collection_larger_than_the_depth_bound_is_refused(db: Any) -> None:
    # The bound is a bind parameter, so a run can shrink it: past the edge
    # count (50) the query fails rather than answer short; at it, it answers.
    from arango.exceptions import AQLQueryExecuteError

    query = "SELECT ?o WHERE { :n0 :next+ ?o }"
    with pytest.raises(AQLQueryExecuteError) as failure:
        run(db, CHAIN, query, depth=49)
    assert failure.value.error_code == ARANGO_ERROR_QUERY_FAIL_CALLED
    assert PATH_DEPTH_EXCEEDED_CODE in str(failure.value)
    assert len(run(db, CHAIN, query, depth=50)) == 50
    assert paths.EDGE_CLOSURE_MAX_DEPTH == 2**53 - 1


@pytest.mark.parametrize(
    "query, expected",
    [
        ("SELECT ?o WHERE { :p :near+ ?o }", {EX + "q", EX + "r"}),
        ("SELECT ?s ?o WHERE { ?s :near+ ?o }", {(EX + a, EX + b) for a, b in ("pq", "pr", "qr")}),
    ],
)
def test_the_start_is_not_held_to_the_test_each_hop_passes(
    db: Any, monkeypatch: pytest.MonkeyPatch, query: str, expected: set[Any]
) -> None:
    # ArangoDB's PRUNE sees the start at depth 0; the walk's ``e != null``
    # keeps a start the arms would walk from.
    walked = run(db, _SHARED_LABELS, query)
    monkeypatch.setattr(paths, "_edge_closure", lambda *_args: None)
    assert_bindings_equal(run(db, _SHARED_LABELS, query), walked)
    assert {r["o"] if len(r) == 1 else (r["s"], r["o"]) for r in walked} == expected
