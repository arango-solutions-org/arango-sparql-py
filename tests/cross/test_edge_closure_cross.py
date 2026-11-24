"""``:p*`` / ``:p+`` over an edge collection, walked to the end (#183 follow-up).

An object property stored in an edge collection of its own is answered by a
breadth-first traversal that reaches each vertex once
(``paths._emit_edge_closure_arm``), not by the bounded arms of
``tests/cross/test_path_depth_cross.py``: no depth limit, no refusal. Each
case runs the same SPARQL through pyoxigraph and through the AQL interpreter
(``tests/helpers/aql_interp.py`` runs the walk as ArangoDB does: breadth
first, each vertex once, the start visited from the outset, ``PRUNE`` seen
at depth 0 too). ``tests/integration/test_edge_closure_live.py`` runs the
same cases on ArangoDB.

The graphs cover what the walk must get right beyond reachability: a chain
of 50 hops (five times the arms' limit), a cycle back to the start (which a
walk that visits each vertex once does not report by itself), shared
descendants, a self-loop, an isolated node (``*``'s zero-length pairs), a
symmetric property, a tenant whose walk must not pass another tenant's
vertex (and must still reach, the long way round, a vertex that vertex
leads to first), and named graphs.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from arango_sparql.errors import PATH_DEPTH_EXCEEDED_CODE
from arango_sparql.translate import paths
from tests.cross.test_multi_document_iri_cross import EX, PREFIXES, Store, oracle_rows, translate_case
from tests.helpers.aql_interp import AqlFailCalled, run_aql_subset
from tests.helpers.oxi import assert_bindings_equal, drop_null_bindings

pytest.importorskip("pyoxigraph", reason="pyoxigraph required for cross tests")

Link = tuple[str, str]


def _ontology(*, tenant: bool = False, symmetric: bool = False) -> str:
    return (
        PREFIXES
        + f"""
:Node a owl:Class ; phys:collectionName "Node" {'; phys:tenantField "tenant" ' if tenant else ""}.
:label a owl:DatatypeProperty ; rdfs:domain :Node .
:next a owl:ObjectProperty {", owl:SymmetricProperty " if symmetric else ""}; phys:edgeCollectionName "next" ;
    rdfs:domain :Node ; rdfs:range :Node .
"""
    )


def _by_number(name: str) -> tuple[str, int]:
    match = re.fullmatch(r"([a-z]+)(\d*)", name)
    assert match is not None
    return match.group(1), int(match.group(2) or 0)


def graph_store(
    links: list[Link],
    *,
    nodes: tuple[str, ...] = (),
    symmetric: bool = False,
    tenants: dict[str, str] | None = None,
    tenant_id: str | None = None,
) -> Store:
    """*links* as ``Node`` documents joined by ``next`` edges; every node is
    labelled. *tenants* puts nodes under a tenant (default ``T1``); the oracle
    reads only what tenant *tenant_id* can see."""
    names = sorted({n for link in links for n in link} | set(nodes), key=_by_number)
    tenant_of = {n: (tenants or {}).get(n, "T1") for n in names} if tenant_id else {}
    docs = {
        "Node": [
            {
                "_id": f"Node/{n}",
                "_uri": EX + n,
                "label": f"L{n}",
                **({"tenant": tenant_of[n]} if tenant_id else {}),
            }
            for n in names
        ],
        "next": [
            {"_id": f"next/{i}", "_from": f"Node/{a}", "_to": f"Node/{b}"} for i, (a, b) in enumerate(links)
        ],
    }
    visible = {n for n in names if not tenant_id or tenant_of[n] == tenant_id}
    triples = [f':{n} :label "L{n}" .' for n in names if n in visible]
    for a, b in links:
        if a in visible and b in visible:
            triples.append(f":{a} :next :{b} .")
            if symmetric:
                triples.append(f":{b} :next :{a} .")
    return Store(
        ontology=_ontology(tenant=bool(tenant_id), symmetric=symmetric),
        docs=docs,
        data="\n".join(triples),
        # A zero-length path from a variable nothing placed ranges over the
        # default collection: here, the nodes.
        resolver_kwargs={"default_collection": "Node"},
        tenant_id=tenant_id,
    )


#: 50 hops: five times the arms' default limit of 10.
CHAIN = graph_store([(f"n{i}", f"n{i + 1}") for i in range(50)])
#: A cycle through the start, a branch off it, a self-loop, an isolated node.
LOOPS = graph_store(
    [("n0", "n1"), ("n1", "n2"), ("n2", "n0"), ("n2", "n3"), ("n5", "n5")],
    nodes=("n9",),
)
#: Shared descendants, a cycle that does not pass the start, an edge into it.
DIAMOND = graph_store(
    [("n0", "n1"), ("n0", "n2"), ("n1", "n3"), ("n2", "n3"), ("n3", "n4"), ("n4", "n1"), ("n6", "n0")]
)
#: Each edge read both ways; a self-loop; an isolated node.
SYMMETRIC = graph_store([("n0", "n1"), ("n1", "n2"), ("n3", "n3")], nodes=("n5",), symmetric=True)
#: ``b`` is another tenant's. ``n0`` reaches ``b`` first (one hop) and ``n3``
#: through it in two; for T1 the walk must not pass ``b``, and must still
#: reach ``n3`` in three hops. ``n7`` is reached only through ``b``, and the
#: only cycle through ``n0`` passes ``b``.
TENANT = graph_store(
    [
        ("n0", "b"),
        ("b", "n3"),
        ("b", "n0"),
        ("b", "n7"),
        ("n0", "n1"),
        ("n1", "n2"),
        ("n2", "n3"),
        ("n3", "n4"),
        ("n4", "b"),
    ],
    tenants={"b": "T2"},
    tenant_id="T1",
)

#: Queries over the nodes ``n0`` and ``n3`` every graph above has.
#:
#: Two zero-length answers of ``*`` are left out, as the zero-length arm gives
#: them the same way with or without the walk: pyoxigraph pairs every literal
#: in the data with itself for ``?s :next* ?o`` (the arm ranges over
#: documents, hence ``isIRI``), and answers nothing for ``:x :next* ?o`` when
#: ``:x`` is in no triple, where the arm, as SPARQL 1.1 §18.4 reads, answers
#: ``:x``.
QUERIES: dict[str, str] = {
    "plus_free": "SELECT ?s ?o WHERE { ?s :next+ ?o }",
    "star_free": "SELECT ?s ?o WHERE { ?s :next* ?o FILTER(isIRI(?s)) }",
    "plus_from_constant": "SELECT ?o WHERE { :n0 :next+ ?o }",
    "star_from_constant": "SELECT ?o WHERE { :n0 :next* ?o }",
    "plus_to_constant": "SELECT ?s WHERE { ?s :next+ :n3 }",
    "star_to_constant": "SELECT ?s WHERE { ?s :next* :n3 }",
    "both_constant": "SELECT (COUNT(*) AS ?c) WHERE { :n0 :next+ :n3 }",
    "back_to_itself": "SELECT ?x WHERE { ?x :next+ ?x }",
    "constant_back_to_itself": "SELECT (COUNT(*) AS ?c) WHERE { :n0 :next+ :n0 }",
    "inverse_of_plus": "SELECT ?s ?o WHERE { ?s ^:next+ ?o }",
    "inverse_from_constant": "SELECT ?o WHERE { :n3 ^:next+ ?o }",
    "plus_of_inverse": "SELECT ?s ?o WHERE { ?s (^:next)+ ?o }",
    "plus_of_inverse_to_constant": "SELECT ?s WHERE { ?s (^:next)+ :n0 }",
    "plus_then_step": "SELECT ?s ?l WHERE { ?s :next+/:label ?l }",
    "star_then_step": "SELECT ?l WHERE { :n0 :next*/:label ?l }",
    "step_then_plus": "SELECT ?s ?o WHERE { ?s :next/:next+ ?o }",
    "subject_bound_to_a_document": 'SELECT ?s ?o WHERE { ?s :label "Ln0" . ?s :next+ ?o }',
    "subject_bound_to_values": "SELECT ?s ?o WHERE { VALUES ?s { :n0 :n3 :nowhere } ?s :next+ ?o }",
    "object_bound_to_values": "SELECT ?s ?o WHERE { VALUES ?o { :n0 :n3 } ?s :next+ ?o }",
    "filtered": "SELECT ?o WHERE { ?s :next+ ?o FILTER(?s = :n0) }",
    "count": "SELECT (COUNT(*) AS ?c) WHERE { ?s :next+ ?o }",
    "distinct_ends": "SELECT DISTINCT ?o WHERE { ?s :next* ?o FILTER(isIRI(?o)) }",
    "optional": "SELECT ?s ?o WHERE { ?s :label ?l OPTIONAL { ?s :next+ ?o } }",
    "zero_length_to_unknown": "SELECT ?s WHERE { ?s :next* :nowhere }",
}
#: An end the query already holds as a document, with the start free: the
#: one-hop arm walks back from it, so these keep the arms (``paths.
#: _walked_back_from_document``); and ``:x (^:p)*``, which the arms walk
#: backwards from ``:x`` (``paths._edge_closure``). Run on the small graphs.
ARMS_QUERIES: dict[str, str] = {
    "object_bound_to_a_document": 'SELECT ?s ?o WHERE { ?o :label "Ln3" . ?s :next+ ?o }',
    "constant_object_read_first": "SELECT ?s WHERE { :n3 :label ?l . ?s :next* :n3 }",
    "star_of_inverse_from_constant": "SELECT ?o WHERE { :n3 (^:next)* ?o }",
    "plus_of_inverse_from_constant": "SELECT ?o WHERE { :n3 (^:next)+ ?o }",
}


def _cases() -> list[Any]:
    out = []
    for graph_name, store in (
        ("chain", CHAIN),
        ("loops", LOOPS),
        ("diamond", DIAMOND),
        ("symmetric", SYMMETRIC),
    ):
        for name, query in QUERIES.items():
            out.append(pytest.param(store, query, id=f"{graph_name}-{name}"))
    for graph_name, store in (("loops", LOOPS), ("diamond", DIAMOND)):
        for name, query in ARMS_QUERIES.items():
            out.append(pytest.param(store, query, id=f"{graph_name}-{name}"))
    for name in (
        "plus_free",
        "star_free",
        "plus_from_constant",
        "star_from_constant",
        "plus_to_constant",
        "back_to_itself",
        "constant_back_to_itself",
        "inverse_of_plus",
        "plus_of_inverse_to_constant",
        "subject_bound_to_values",
        "count",
    ):
        out.append(pytest.param(TENANT, QUERIES[name], id=f"tenant-{name}"))
    out.append(pytest.param(TENANT, "SELECT ?o WHERE { :b :next+ ?o }", id="tenant-from_another_tenant"))
    out.append(
        pytest.param(
            TENANT,
            "SELECT (COUNT(*) AS ?c) WHERE { :b :next+ :n3 }",
            id="tenant-to_constant_from_another_tenant",
        )
    )
    return out


# --- Named graphs ------------------------------------------------------------

#: (graph or None for the default graph, links). As the W3C loader and
#: ``tests/cross/test_named_graph_scope_cross.py`` store them: one document
#: per node and graph, edges between the copies in one graph. ``n0 -> n1``
#: is in ``:g1`` and ``:g2``; ``:g1``'s chain is 12 hops.
_GRAPH_FACTS: list[tuple[str | None, list[Link]]] = [
    (None, [("d0", "d1"), ("d1", "d2")]),
    (EX + "g1", [(f"n{i}", f"n{i + 1}") for i in range(12)]),
    (EX + "g2", [("c0", "c1"), ("c1", "c2"), ("c2", "c0"), ("n0", "n1")]),
]


def _copy(graph: str | None, node: str) -> str:
    return node if graph is None else f"{node}_{graph.rsplit('/', 1)[1]}"


def named_graph_store(*, lax: bool) -> Store:
    docs: dict[str, list[dict[str, Any]]] = {"Node": [], "next": []}
    blocks = []
    for graph, links in _GRAPH_FACTS:
        in_graph = {} if graph is None else {"_graph": graph}
        names = sorted({n for link in links for n in link}, key=_by_number)
        docs["Node"] += [
            {"_id": f"Node/{_copy(graph, n)}", "_uri": EX + n, "label": f"L{n}", **in_graph} for n in names
        ]
        docs["next"] += [
            {
                "_id": f"next/{_copy(graph, a)}_{b}",
                "_from": f"Node/{_copy(graph, a)}",
                "_to": f"Node/{_copy(graph, b)}",
                **in_graph,
            }
            for a, b in links
        ]
        body = "\n".join(
            [*(f':{n} :label "L{n}" .' for n in names), *(f":{a} :next :{b} ." for a, b in links)]
        )
        blocks.append(body if graph is None else f"<{graph}> {{\n{body}\n}}")
    return Store(
        ontology=_ontology(),
        docs=docs,
        data="\n".join(blocks),
        trig=True,
        union_default_graph=lax,
        resolver_kwargs={
            "default_collection": "Node",
            **({} if lax else {"default_graph_includes_named": False}),
        },
    )


NAMED_LAX = named_graph_store(lax=True)
NAMED_STRICT = named_graph_store(lax=False)

GRAPH_CASES = [
    pytest.param(NAMED_LAX, "SELECT ?s ?o WHERE { GRAPH :g1 { ?s :next+ ?o } }", id="lax-constant_graph"),
    pytest.param(NAMED_LAX, "SELECT ?g ?s ?o WHERE { GRAPH ?g { ?s :next+ ?o } }", id="lax-graph_variable"),
    pytest.param(
        NAMED_LAX, "SELECT ?g ?o WHERE { GRAPH ?g { :n0 :next+ ?o } }", id="lax-graph_variable_constant_start"
    ),
    pytest.param(NAMED_LAX, "SELECT ?g ?x WHERE { GRAPH ?g { ?x :next+ ?x } }", id="lax-cycle_per_graph"),
    pytest.param(
        NAMED_LAX, "SELECT ?o WHERE { GRAPH :g2 { :n0 :next+ ?o } }", id="lax-copy_in_another_graph"
    ),
    pytest.param(NAMED_LAX, "SELECT ?s ?o WHERE { ?s :next+ ?o }", id="lax-default_graph_is_every_graph"),
    pytest.param(NAMED_STRICT, "SELECT ?s ?o WHERE { ?s :next+ ?o }", id="strict-default_graph_only"),
    pytest.param(
        NAMED_STRICT, "SELECT ?g ?s ?o WHERE { GRAPH ?g { ?s :next+ ?o } }", id="strict-graph_variable"
    ),
]


def run(store: Store, query: str) -> list[dict[str, Any]]:
    result = translate_case(store, query)
    return [drop_null_bindings(r) for r in run_aql_subset(result.aql, result.bind_vars, store.docs)]


@pytest.mark.parametrize("store, query", [*_cases(), *GRAPH_CASES])
def test_the_walk_agrees_with_pyoxigraph(store: Store, query: str) -> None:
    assert_bindings_equal(oracle_rows(store, query), run(store, query))


@pytest.mark.parametrize("store, query", [*_cases(), *GRAPH_CASES])
def test_the_walk_is_a_traversal_with_no_depth_check(store: Store, query: str) -> None:
    aql = translate_case(store, query).aql
    if query in ARMS_QUERIES.values():
        assert "uniqueVertices" not in aql and "MINUS(" in aql
        return
    assert "uniqueVertices" in aql
    assert "MINUS(" not in aql, "a traversal needs no depth check"


def test_fifty_hops_in_full() -> None:
    pairs = run(CHAIN, "SELECT ?s ?o WHERE { ?s :next+ ?o }")
    assert len(pairs) == 50 * 51 // 2
    assert {"s": EX + "n0", "o": EX + "n50"} in pairs
    assert sorted(r["o"] for r in run(CHAIN, "SELECT ?o WHERE { :n0 :next* ?o }")) == sorted(
        EX + f"n{i}" for i in range(51)
    )


def test_a_cycle_back_to_the_start_counts_for_plus() -> None:
    # A walk that visits each vertex once starts with the start visited, so
    # the cycle n0 -> n1 -> n2 -> n0 never reports n0; ``back`` does.
    assert {r["o"] for r in run(LOOPS, "SELECT ?o WHERE { :n0 :next+ ?o }")} == {
        EX + "n0",
        EX + "n1",
        EX + "n2",
        EX + "n3",
    }
    # n3 is reached, and reaches nothing: not its own successor.
    assert run(LOOPS, "SELECT ?o WHERE { :n3 :next+ ?o }") == []
    assert run(LOOPS, "SELECT ?o WHERE { :n5 :next+ ?o }") == [{"o": EX + "n5"}]


def test_another_tenants_vertex_is_neither_reached_nor_passed() -> None:
    # n0 -> b -> n3 is two hops through T2's b; n0 -> n1 -> n2 -> n3 is three.
    # b is reached first; pruned, it marks no vertex beyond it as visited.
    reached = {r["o"] for r in run(TENANT, "SELECT ?o WHERE { :n0 :next+ ?o }")}
    assert reached == {EX + f"n{i}" for i in (1, 2, 3, 4)}
    # The one cycle back to n0 passes b.
    assert run(TENANT, "SELECT (COUNT(*) AS ?c) WHERE { :n0 :next+ :n0 }") == [{"c": 0}]


@pytest.mark.parametrize("hops, refused", [(3, False), (4, True)])
def test_an_edge_collection_larger_than_the_depth_bound_is_refused(
    monkeypatch: pytest.MonkeyPatch, hops: int, refused: bool
) -> None:
    # No answer is further than the edge collection has edges; past the
    # bound, the walk could stop short, so the query is refused instead.
    monkeypatch.setattr(paths, "EDGE_CLOSURE_MAX_DEPTH", 3)
    store = graph_store([(f"n{i}", f"n{i + 1}") for i in range(hops)])
    query = "SELECT ?o WHERE { :n0 :next+ ?o }"
    if not refused:
        assert_bindings_equal(oracle_rows(store, query), run(store, query))
        return
    with pytest.raises(AqlFailCalled) as failure:
        run(store, query)
    assert str(failure.value).startswith(PATH_DEPTH_EXCEEDED_CODE)
    assert "edge collection 'next'" in str(failure.value)


#: LPG: ``:p`` is a Place (no tenant) and also another tenant's Person. Read
#: as a Place, it is a start; the arms never hold a start to the test each
#: hop's vertex passes, and the walk must not either (``PRUNE`` sees the
#: start at depth 0, hence ``e != null``).
_SHARED_LABELS = Store(
    ontology=PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "nodes" ; phys:mappingStyle "LABEL" ; phys:typeField "labels" ;
    phys:typeFieldIsArray "true" ; phys:typeValue "Person" ; phys:tenantField "org" .
:Place a owl:Class ; phys:collectionName "nodes" ; phys:mappingStyle "LABEL" ; phys:typeField "labels" ;
    phys:typeFieldIsArray "true" ; phys:typeValue "Place" .
:near a owl:ObjectProperty ; phys:edgeCollectionName "near" ; rdfs:domain :Place ; rdfs:range :Place .
""",
    docs={
        "nodes": [
            {"_id": "nodes/p", "_uri": EX + "p", "labels": ["Place", "Person"], "org": "T2"},
            {"_id": "nodes/q", "_uri": EX + "q", "labels": ["Place"]},
            {"_id": "nodes/r", "_uri": EX + "r", "labels": ["Place"]},
            {"_id": "nodes/s", "_uri": EX + "s", "labels": ["Person"], "org": "T2"},
        ],
        "near": [
            {"_id": "near/1", "_from": "nodes/p", "_to": "nodes/q"},
            {"_id": "near/2", "_from": "nodes/q", "_to": "nodes/r"},
            {"_id": "near/3", "_from": "nodes/q", "_to": "nodes/s"},
        ],
    },
    data="",
    tenant_id="T1",
)


@pytest.mark.parametrize(
    "query, expected",
    [
        ("SELECT ?o WHERE { :p :near+ ?o }", {EX + "q", EX + "r"}),
        ("SELECT ?s ?o WHERE { ?s :near+ ?o }", {(EX + a, EX + b) for a, b in ("pq", "pr", "qr")}),
    ],
)
def test_the_start_is_not_held_to_the_test_each_hop_passes(
    monkeypatch: pytest.MonkeyPatch, query: str, expected: set[Any]
) -> None:
    walked = run(_SHARED_LABELS, query)
    assert "uniqueVertices" in translate_case(_SHARED_LABELS, query).aql
    monkeypatch.setattr(paths, "_edge_closure", lambda *_args: None)
    arms = run(_SHARED_LABELS, query)
    assert_bindings_equal(arms, walked)
    assert {r["o"] if len(r) == 1 else (r["s"], r["o"]) for r in walked} == expected
