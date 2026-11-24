"""``:p*`` / ``:p+`` answer in full or refuse, never short (issue #183).

The translator lowers ``*`` / ``+`` to a union of fixed-length arms up to
``SchemaResolver.property_path_max_depth`` (default 10). Before #183 a chain
longer than that was cut off without a word: ``?s :next+ ?o`` over an 11-hop
chain returned 65 of its 66 pairs. Now the path is also walked once more,
and the query stops with ``FAIL`` (``E_SPARQL_PATH_DEPTH``) when that walk
reaches a pair the arms do not.

Each case runs the same SPARQL through pyoxigraph and through the AQL
interpreter, on both storage shapes the path goldens use: an undeclared
predicate stored as a document attribute holding the next node's IRI (the
default-collection layout of ``property_paths.yml`` and the W3C loader), and
an object property backed by an edge collection. ``tests/integration/
test_path_depth_live.py`` runs the same cases on ArangoDB.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.errors import PATH_DEPTH_EXCEEDED_CODE
from arango_sparql.translate.paths import PROPERTY_PATH_MAX_DEPTH
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.aql_interp import AqlFailCalled, run_aql_subset
from tests.helpers.oxi import assert_bindings_equal, drop_null_bindings, normalize_oxi_row, oxi_bindings

oxi = pytest.importorskip("pyoxigraph", reason="pyoxigraph required for cross tests")

EX = "http://ex.org/"
PREFIX = f"PREFIX : <{EX}>\n"
DOC, NODE, NEXT = "DepthDoc", "DepthNode", "DepthNext"


@dataclass(frozen=True)
class Graph:
    """A set of ``:next`` links over ``n0 … n<k>``, each node labelled."""

    name: str
    links: tuple[tuple[int, int], ...]

    @property
    def nodes(self) -> list[int]:
        return sorted({n for link in self.links for n in link})

    def turtle(self) -> str:
        lines = [f'<{EX}n{n}> <{EX}label> "L{n}" .' for n in self.nodes]
        lines += [f"<{EX}n{a}> <{EX}next> <{EX}n{b}> ." for a, b in self.links]
        return "\n".join(lines)


def chain(hops: int) -> Graph:
    return Graph(f"chain{hops}", tuple((i, i + 1) for i in range(hops)))


#: 11 hops: one past the default limit of 10.
LONG = chain(PROPERTY_PATH_MAX_DEPTH + 1)
#: Exactly the limit: answered in full, nothing refused.
AT_LIMIT = chain(PROPERTY_PATH_MAX_DEPTH)
#: A 3-cycle: walks of any length exist, but every node is within 2 steps.
CYCLE = Graph("cycle3", ((0, 1), (1, 2), (2, 0)))


@dataclass(frozen=True)
class Shape:
    """How ``:next`` is stored."""

    name: str
    ontology: str
    edges: bool

    def resolver(self, max_depth: int | None = None) -> SchemaResolver:
        resolver = SchemaResolver.from_turtle(self.ontology, default_collection=DOC)
        if max_depth is not None:
            resolver.property_path_max_depth = max_depth
        return resolver

    def docs(self, graph: Graph) -> dict[str, list[dict[str, Any]]]:
        """The collections holding *graph* in this shape."""
        if not self.edges:
            # One document per node; ``next`` is the next node's IRI.
            nexts = dict(graph.links)
            assert len(nexts) == len(graph.links), "the attribute shape holds one link per node"
            return {
                DOC: [
                    {
                        "_id": f"{DOC}/n{n}",
                        "_key": f"n{n}",
                        "_uri": f"{EX}n{n}",
                        "label": f"L{n}",
                        **({"next": f"{EX}n{nexts[n]}"} if n in nexts else {}),
                    }
                    for n in graph.nodes
                ]
            }
        # Class documents joined by edges; every node is also in the default
        # collection, which a zero-length step from an unplaced subject reads
        # (the W3C loader's layout).
        return {
            NODE: [
                {"_id": f"{NODE}/n{n}", "_key": f"n{n}", "_uri": f"{EX}n{n}", "label": f"L{n}"}
                for n in graph.nodes
            ],
            DOC: [{"_id": f"{DOC}/n{n}", "_key": f"n{n}", "_uri": f"{EX}n{n}"} for n in graph.nodes],
            NEXT: [
                {"_id": f"{NEXT}/{a}_{b}", "_key": f"{a}_{b}", "_from": f"{NODE}/n{a}", "_to": f"{NODE}/n{b}"}
                for a, b in graph.links
            ],
        }


ATTRIBUTE = Shape("attribute", "", edges=False)
EDGE = Shape(
    "edge",
    f"""
@prefix : <{EX}> . @prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> . @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
:Node a owl:Class ; phys:collectionName "{NODE}" .
:label a owl:DatatypeProperty ; rdfs:domain :Node .
:next a owl:ObjectProperty ; phys:edgeCollectionName "{NEXT}" ; rdfs:domain :Node ; rdfs:range :Node .
""",
    edges=True,
)
SHAPES = [pytest.param(ATTRIBUTE, id="attribute"), pytest.param(EDGE, id="edge")]

#: Queries over the whole chain: every one needs all 11 hops of LONG.
WHOLE_CHAIN = [
    pytest.param("SELECT ?s ?o WHERE { ?s :next+ ?o }", id="plus_free"),
    pytest.param("SELECT ?o WHERE { :n0 :next+ ?o }", id="plus_from_constant"),
    pytest.param("SELECT ?o WHERE { :n0 :next* ?o }", id="star_from_constant"),
    pytest.param("SELECT ?s WHERE { ?s :next+ :n11 }", id="plus_to_constant"),
    pytest.param("SELECT ?s WHERE { ?s :next* :n11 }", id="star_to_constant"),
    pytest.param("SELECT ?s WHERE { :n11 ^:next+ ?s }", id="inverse_of_plus"),
    pytest.param("SELECT ?s ?o WHERE { ?s (^:next)+ ?o }", id="plus_of_inverse"),
    pytest.param("SELECT ?s ?l WHERE { ?s :next+/:label ?l }", id="plus_then_step"),
    pytest.param("SELECT ?o WHERE { :n0 :next*/:label ?o }", id="star_then_step"),
    pytest.param("SELECT ?s ?o WHERE { ?s :label ?l . ?s :next+ ?o }", id="plus_from_bound_subject"),
    pytest.param("SELECT ?l WHERE { :n0 :next+ :n11 . :n11 :label ?l }", id="plus_both_constant"),
]


def run(shape: Shape, graph: Graph, query: str, max_depth: int | None = None) -> list[dict[str, Any]]:
    result = translate(PREFIX + query, resolver=shape.resolver(max_depth))
    return [drop_null_bindings(r) for r in run_aql_subset(result.aql, result.bind_vars, shape.docs(graph))]


def oracle(graph: Graph, query: str) -> list[dict[str, Any]]:
    store = oxi.Store()
    store.load(graph.turtle().encode(), format=oxi.RdfFormat.TURTLE)
    return [normalize_oxi_row(r) for r in oxi_bindings(store, PREFIX + query)]


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("query", WHOLE_CHAIN)
def test_a_chain_longer_than_the_limit_is_refused(shape: Shape, query: str) -> None:
    with pytest.raises(AqlFailCalled) as failure:
        run(shape, LONG, query)
    message = str(failure.value)
    assert message.startswith(PATH_DEPTH_EXCEEDED_CODE)
    assert f"property_path_max_depth ({PROPERTY_PATH_MAX_DEPTH})" in message


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("query", WHOLE_CHAIN)
def test_a_chain_longer_than_the_default_is_answered_in_full_under_a_raised_limit(
    shape: Shape, query: str
) -> None:
    expected = oracle(LONG, query)
    assert expected, "the oracle should answer every case"
    assert_bindings_equal(expected, run(shape, LONG, query, max_depth=PROPERTY_PATH_MAX_DEPTH + 1))


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("query", WHOLE_CHAIN)
def test_a_chain_at_the_limit_is_answered_in_full(shape: Shape, query: str) -> None:
    query = query.replace(":n11", f":n{PROPERTY_PATH_MAX_DEPTH}")
    expected = oracle(AT_LIMIT, query)
    assert expected
    assert_bindings_equal(expected, run(shape, AT_LIMIT, query))


@pytest.mark.parametrize("shape", SHAPES)
def test_every_pair_of_the_eleven_hop_chain(shape: Shape) -> None:
    # The issue's acceptance case, spelled out: 66 pairs for +, 78 for *.
    plus = run(shape, LONG, "SELECT ?s ?o WHERE { ?s :next+ ?o }", max_depth=11)
    assert len(plus) == 66
    assert {"s": f"{EX}n0", "o": f"{EX}n11"} in plus
    star = run(shape, LONG, "SELECT ?o WHERE { :n0 :next* ?o }", max_depth=11)
    assert sorted(r["o"] for r in star) == sorted(f"{EX}n{i}" for i in range(12))


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize(
    "query",
    [
        pytest.param("SELECT ?o WHERE { :n5 :next+ ?o }", id="plus_from_midway"),
        pytest.param("SELECT ?o WHERE { :n1 :next* ?o }", id="star_from_one_in"),
        pytest.param("SELECT ?s WHERE { ?s ^:next+ :n5 }", id="inverse_from_midway"),
    ],
)
def test_a_path_anchored_within_the_limit_is_answered(shape: Shape, query: str) -> None:
    # The check walks from the path's own subject, so a start whose chain
    # ends within 10 steps answers, though the graph is longer.
    assert_bindings_equal(oracle(LONG, query), run(shape, LONG, query))


@pytest.mark.parametrize("shape", SHAPES)
def test_a_free_subject_is_checked_over_every_subject(shape: Shape) -> None:
    # ``?s :next+ :n6`` needs 6 steps at most, but its subject is free, so the
    # check walks from every subject the arms scan and meets n0 → n11. Walking
    # back from :n6 instead would trust that every node on such a walk is one
    # the arms scan as a subject, which an edge whose source lies outside the
    # property's domain collection breaks. Refused, documented in
    # ``paths._emit_mul_path``; a limit that covers the graph answers it.
    query = "SELECT ?s WHERE { ?s :next+ :n6 }"
    with pytest.raises(AqlFailCalled):
        run(shape, LONG, query)
    assert_bindings_equal(oracle(LONG, query), run(shape, LONG, query, max_depth=11))


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize(
    "query",
    [
        pytest.param("SELECT ?s ?o WHERE { ?s :next+ ?o }", id="plus_free"),
        pytest.param("SELECT ?o WHERE { :n0 :next+ ?o }", id="plus_from_constant"),
        pytest.param("SELECT ?o WHERE { :n0 :next* ?o }", id="star_from_constant"),
        pytest.param("SELECT ?x WHERE { ?x :next+ ?x }", id="plus_back_to_itself"),
        pytest.param("SELECT ?s WHERE { ?s :next+ :n0 }", id="plus_to_constant"),
    ],
)
def test_a_cycle_is_answered_not_refused(shape: Shape, query: str) -> None:
    # Walks of 11 steps exist around a 3-cycle; they reach no new node.
    assert_bindings_equal(oracle(CYCLE, query), run(shape, CYCLE, query))


@pytest.mark.parametrize("shape", SHAPES)
def test_a_limit_of_one_still_checks(shape: Shape) -> None:
    # One arm for ``+``: the union pairs it with ``[]``.
    with pytest.raises(AqlFailCalled):
        run(shape, chain(2), "SELECT ?o WHERE { :n0 :next+ ?o }", max_depth=1)
    query = "SELECT ?o WHERE { :n0 :next+ ?o }"
    assert_bindings_equal(oracle(chain(1), query), run(shape, chain(1), query, max_depth=1))
    # A fixed object: the check runs on its own, then the single arm.
    fixed = "SELECT ?s WHERE { ?s :next+ :n1 }"
    with pytest.raises(AqlFailCalled):
        run(shape, chain(2), fixed, max_depth=1)
    assert_bindings_equal(oracle(chain(1), fixed), run(shape, chain(1), fixed, max_depth=1))


def test_a_limit_below_one_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="property_path_max_depth must be an integer >= 1"):
        run(ATTRIBUTE, chain(1), "SELECT ?o WHERE { :n0 :next+ ?o }", max_depth=0)
