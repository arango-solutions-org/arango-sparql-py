"""``:p*`` / ``:p+`` answer in full or refuse, on ArangoDB (issue #183).

The cases of ``tests/cross/test_path_depth_cross.py``, run on a live server:
the translated AQL stops with ``FAIL`` (ERR 1569, ``E_SPARQL_PATH_DEPTH``)
where a path is longer than ``SchemaResolver.property_path_max_depth``, and
otherwise returns pyoxigraph's answer. Live also covers what the interpreter
cannot run (``ASK``) and shows the guard survives ArangoDB's optimizer: the
``FAIL`` is not folded away or skipped. Gated like the rest of the
integration suite (``RUN_INTEGRATION=1``).

The case matrix runs at a limit of 4 over 5- and 4-hop chains: ArangoDB's
planner time for the attribute shape grows about fourfold per hop of the
longest arm (the ``use-indexes`` rule on the IRI join), so the 11-hop chain
at the default limit is run for the refusal, and answered on edges only.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.errors import ARANGO_ERROR_QUERY_FAIL_CALLED, PATH_DEPTH_EXCEEDED_CODE
from arango_sparql.translate.paths import PROPERTY_PATH_MAX_DEPTH
from tests.cross.test_path_depth_cross import (
    CYCLE,
    DOC,
    EDGE,
    LONG,
    NEXT,
    NODE,
    PREFIX,
    SHAPES,
    WHOLE_CHAIN,
    Graph,
    Shape,
    chain,
    oracle,
)
from tests.helpers.oxi import assert_bindings_equal, drop_null_bindings
from tests.integration.conftest import (
    DEFAULT_ARANGO_DB,
    DEFAULT_ARANGO_PASSWORD,
    DEFAULT_ARANGO_URL,
    DEFAULT_ARANGO_USER,
    arangodb_reachable,
    ensure_test_database,
    integration_enabled,
    try_boot_arangodb_via_compose,
)

pytestmark = pytest.mark.integration

pytest.importorskip("pyoxigraph", reason="pyoxigraph is the ground truth for these tests")


@pytest.fixture(scope="module")
def db() -> Iterator[Any]:
    if not integration_enabled():
        pytest.skip("set RUN_INTEGRATION=1 to enable integration tests")
    if not arangodb_reachable() and not try_boot_arangodb_via_compose():
        pytest.skip(f"ArangoDB at {DEFAULT_ARANGO_URL} is unreachable and could not be booted")
    from arango import ArangoClient

    ensure_test_database()
    handle = ArangoClient(hosts=DEFAULT_ARANGO_URL).db(
        DEFAULT_ARANGO_DB, username=DEFAULT_ARANGO_USER, password=DEFAULT_ARANGO_PASSWORD
    )
    yield handle
    for name in (DOC, NODE, NEXT):
        if handle.has_collection(name):
            handle.delete_collection(name)


def load(db: Any, shape: Shape, graph: Graph) -> None:
    """Replace the test collections with *graph* stored in *shape*."""
    for name in (DOC, NODE, NEXT):
        if db.has_collection(name):
            db.delete_collection(name)
    for name, docs in shape.docs(graph).items():
        db.create_collection(name, edge=name == NEXT).insert_many(
            [{k: v for k, v in d.items() if k != "_id"} for d in docs]
        )


def run(db: Any, shape: Shape, query: str, max_depth: int | None = None) -> list[Any]:
    result = translate(PREFIX + query, resolver=shape.resolver(max_depth))
    return list(db.aql.execute(result.aql, bind_vars=result.bind_vars))


def assert_refused(db: Any, shape: Shape, query: str, max_depth: int | None = None) -> None:
    from arango.exceptions import AQLQueryExecuteError

    with pytest.raises(AQLQueryExecuteError) as failure:
        run(db, shape, query, max_depth)
    assert failure.value.error_code == ARANGO_ERROR_QUERY_FAIL_CALLED
    assert PATH_DEPTH_EXCEEDED_CODE in str(failure.value)


#: The live case matrix's limit, and its chains: one hop past it, and at it.
LIMIT = 4
PAST, AT = chain(LIMIT + 1), chain(LIMIT)


def whole_chain(hops: int) -> list[str]:
    """The cross test's whole-chain queries, ending at ``:n<hops>``."""
    return [param.values[0].replace(":n11", f":n{hops}") for param in WHOLE_CHAIN]


@pytest.mark.parametrize("shape", SHAPES)
def test_an_eleven_hop_chain_is_refused_at_the_default_limit(db: Any, shape: Shape) -> None:
    load(db, shape, LONG)
    assert_refused(db, shape, "SELECT ?s ?o WHERE { ?s :next+ ?o }")
    assert_refused(db, shape, "SELECT ?o WHERE { :n0 :next* ?o }")


def test_every_pair_of_the_eleven_hop_chain(db: Any) -> None:
    # Edge shape only: at a limit of 11 the attribute shape takes ArangoDB's
    # planner about half a minute per query; the cross test covers it.
    load(db, EDGE, LONG)
    assert len(run(db, EDGE, "SELECT ?s ?o WHERE { ?s :next+ ?o }", max_depth=11)) == 66
    assert len(run(db, EDGE, "SELECT ?o WHERE { :n0 :next* ?o }", max_depth=11)) == 12


@pytest.mark.parametrize("shape", SHAPES)
def test_whole_chain_cases_refuse_then_answer_under_a_raised_limit(db: Any, shape: Shape) -> None:
    load(db, shape, PAST)
    for query in whole_chain(LIMIT + 1):
        assert_refused(db, shape, query, max_depth=LIMIT)
        actual = [drop_null_bindings(r) for r in run(db, shape, query, max_depth=LIMIT + 1)]
        assert_bindings_equal(oracle(PAST, query), actual)


@pytest.mark.parametrize("shape", SHAPES)
def test_whole_chain_cases_at_the_limit_answer(db: Any, shape: Shape) -> None:
    load(db, shape, AT)
    for query in whole_chain(LIMIT):
        actual = [drop_null_bindings(r) for r in run(db, shape, query, max_depth=LIMIT)]
        assert_bindings_equal(oracle(AT, query), actual)


@pytest.mark.parametrize("shape", SHAPES)
def test_ask(db: Any, shape: Shape) -> None:
    load(db, shape, PAST)
    assert_refused(db, shape, f"ASK {{ :n0 :next+ :n{LIMIT + 1} }}", max_depth=LIMIT)
    assert run(db, shape, f"ASK {{ :n0 :next+ :n{LIMIT + 1} }}", max_depth=LIMIT + 1) == [True]
    assert run(db, shape, f"ASK {{ :n{LIMIT + 1} :next+ :n0 }}", max_depth=LIMIT + 1) == [False]


@pytest.mark.parametrize("shape", SHAPES)
def test_a_cycle_is_answered(db: Any, shape: Shape) -> None:
    load(db, shape, CYCLE)
    for query in (
        "SELECT ?s ?o WHERE { ?s :next+ ?o }",
        "SELECT ?o WHERE { :n0 :next* ?o }",
        "SELECT ?x WHERE { ?x :next+ ?x }",
    ):
        actual = [drop_null_bindings(r) for r in run(db, shape, query, max_depth=LIMIT)]
        assert_bindings_equal(oracle(CYCLE, query), actual)


@pytest.mark.parametrize("shape", SHAPES)
def test_an_anchored_path_within_the_limit_answers(db: Any, shape: Shape) -> None:
    load(db, shape, PAST)
    for query in ("SELECT ?o WHERE { :n2 :next+ ?o }", "SELECT ?s WHERE { ?s ^:next+ :n2 }"):
        actual = [drop_null_bindings(r) for r in run(db, shape, query, max_depth=LIMIT)]
        assert_bindings_equal(oracle(PAST, query), actual)
