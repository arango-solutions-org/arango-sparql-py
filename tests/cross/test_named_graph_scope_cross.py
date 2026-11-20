"""Cross-validation of the GRAPH scope (#195, #106, #144; PRD §6.6 named graphs).

``GRAPH ?g`` ranges over the named graphs only, so a document whose
``_graph`` is null (the default graph) never matches inside it, and
everything inside a GRAPH is matched in the scope's graph: a MINUS / EXISTS
probe, an OPTIONAL body (inline, a correlated subquery, or the triples-table
scan) and a sub-SELECT. Each case runs on PG (edge collection), LPG (generic
edges with a type field) and RPT (a triples table), through the AQL
interpreter here and on ArangoDB in
``tests/integration/test_named_graph_scope_live.py``.

Two fact sets:

* ``_SCOPE_FACTS``: people in the default graph and two named graphs; ``:a``
  and ``:d`` have a copy in the default graph and in ``:g1``.
* ``_SPLIT_FACTS`` (the review of #198): one IRI's triples split across
  ``:g1`` / ``:g2`` / ``:g3``, so a pattern that reads the wrong graph finds
  a value instead of none.

The oracle is pyoxigraph, except for a sub-SELECT inside ``GRAPH ?g``:
pyoxigraph evaluates it over every named graph at once and joins afterwards,
where SPARQL 1.1 §18.6 evaluates the group once per named graph; rdflib does
the latter and is the oracle for those cases.
"""

from __future__ import annotations

from typing import Any

import pytest
import rdflib

from tests.cross.test_multi_document_iri_cross import EX, PREFIXES, Store, oracle_rows, translate_case
from tests.helpers.aql_interp import run_aql_subset
from tests.helpers.oxi import assert_bindings_equal, drop_null_bindings

pytest.importorskip("pyoxigraph", reason="pyoxigraph required for cross tests")

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

#: (graph or None for the default graph, subject, attributes, knows)
Facts = list[tuple[str | None, str, dict[str, str], list[str]]]

_SCOPE_FACTS: Facts = [
    (None, "a", {"name": "A"}, ["b"]),
    (None, "b", {"email": "b@x"}, ["a"]),
    (None, "z", {"name": "Z"}, ["z"]),
    (None, "d", {"name": "D0"}, ["z"]),
    (EX + "g1", "c", {"name": "C"}, ["d"]),
    (EX + "g1", "d", {"email": "d@x"}, ["c"]),
    (EX + "g1", "a", {"name": "A1"}, ["c"]),
    (EX + "g2", "e", {}, ["f"]),
    (EX + "g2", "f", {"name": "F", "email": "f@x"}, ["e"]),
]

_SPLIT_FACTS: Facts = [
    (None, "a", {"name": "A"}, ["b"]),
    (None, "b", {"email": "b@x"}, []),
    (None, "z", {"name": "Z"}, []),
    (EX + "g1", "a", {"name": "A1", "email": "a1@x"}, ["c"]),
    (EX + "g1", "c", {"name": "C"}, []),
    (EX + "g1", "m", {"email": "m@x"}, []),
    (EX + "g2", "m", {"name": "M2"}, []),
    (EX + "g2", "c", {"email": "c2@x"}, []),
    (EX + "g2", "e", {}, ["m"]),
    (EX + "g3", "a", {"email": "a3@x"}, []),
]


def _key(graph: str | None, subject: str) -> str:
    return subject if graph is None else f"{subject}_{graph.rsplit('/', 1)[1]}"


def _in_graph(graph: str | None) -> dict[str, str]:
    return {} if graph is None else {"_graph": graph}


def _trig(facts: Facts) -> str:
    blocks = []
    for graph, subject, attrs, knows in facts:
        body = [f":{subject} a :Person", *(f':{subject} :{k} "{v}"' for k, v in attrs.items())]
        body += [f":{subject} :knows :{t}" for t in knows]
        block = " .\n".join(body) + " ."
        blocks.append(block if graph is None else f"<{graph}> {{\n{block}\n}}")
    return "\n".join(blocks)


def _documents(
    facts: Facts, label: dict[str, str], edge_label: dict[str, str]
) -> dict[str, list[dict[str, Any]]]:
    return {
        "Person": [
            {"_id": f"Person/{_key(g, s)}", "_uri": EX + s, **_in_graph(g), **label, **attrs}
            for g, s, attrs, _k in facts
        ],
        "knows": [
            {
                "_id": f"knows/{_key(g, s)}_{t}",
                "_from": f"Person/{_key(g, s)}",
                "_to": f"Person/{_key(g, t)}",
                **_in_graph(g),
                **edge_label,
            }
            for g, s, _a, knows in facts
            for t in knows
        ],
    }


def _triples(facts: Facts) -> dict[str, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    for graph, subject, attrs, knows in facts:
        here = {"subject_uri": EX + subject, **_in_graph(graph)}
        rows.append({**here, "predicate": RDF_TYPE, "object_uri": EX + "Person"})
        rows += [{**here, "predicate": EX + k, "object_value": v} for k, v in attrs.items()]
        rows += [{**here, "predicate": EX + "knows", "object_uri": EX + t} for t in knows]
    return {"t_triples": [{"_id": f"t_triples/{i}", **row} for i, row in enumerate(rows)]}


_PROPERTIES = """
:name a owl:DatatypeProperty ; rdfs:domain :Person .
:email a owl:DatatypeProperty ; rdfs:domain :Person .
"""
_PG = """
:Person a owl:Class ; phys:collectionName "Person" .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; rdfs:domain :Person ; rdfs:range :Person .
"""
_LPG = """
:Person a owl:Class ; phys:collectionName "Person" ; phys:mappingStyle "LABEL" ;
    phys:typeField "type" ; phys:typeValue "Person" .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; phys:mappingStyle "GENERIC_WITH_TYPE" ;
    phys:typeField "type" ; phys:typeValue "knows" ; rdfs:domain :Person ; rdfs:range :Person .
"""
_RPT = """
:Person a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "t_triples" .
"""


def _stores(facts: Facts, *, lax: bool = True) -> dict[str, Store]:
    """The facts on PG, LPG and RPT. *lax*: the resolver's default graph is
    the union of every graph (its default), and the oracle reads it so."""
    options: dict[str, Any] = {
        "trig": True,
        "union_default_graph": lax,
        "resolver_kwargs": {} if lax else {"default_graph_includes_named": False},
    }
    data = _trig(facts)
    return {
        "pg": Store(PREFIXES + _PG + _PROPERTIES, _documents(facts, {}, {}), data, **options),
        "lpg": Store(
            PREFIXES + _LPG + _PROPERTIES,
            _documents(facts, {"type": "Person"}, {"type": "knows"}),
            data,
            **options,
        ),
        "rpt": Store(PREFIXES + _RPT, _triples(facts), data, **options),
    }


SCOPE = _stores(_SCOPE_FACTS)
SPLIT = _stores(_SPLIT_FACTS)
SPLIT_STRICT = _stores(_SPLIT_FACTS, lax=False)
MAPPINGS = ("pg", "lpg", "rpt")


def rdflib_rows(store: Store, query: str) -> list[dict[str, Any]]:
    """rdflib's answer on the case's facts: the oracle for a sub-SELECT inside
    ``GRAPH ?g``, which it evaluates once per named graph (§18.6)."""
    dataset = rdflib.Dataset(default_union=store.union_default_graph)
    dataset.parse(data=PREFIXES + store.data, format="trig")
    rows = []
    for row in dataset.query("PREFIX : <http://ex.org/>\n" + query):
        values = row.asdict()  # type: ignore[union-attr]
        rows.append(
            {str(k): v.toPython() if isinstance(v, rdflib.Literal) else str(v) for k, v in values.items()}
        )
    return rows


def expected_rows(store: Store, query: str, oracle: str) -> list[dict[str, Any]]:
    return rdflib_rows(store, query) if oracle == "rdflib" else oracle_rows(store, query)


#: (name, fact set, query, oracle)
_SHAPES: list[tuple[str, dict[str, Store], str, str]] = [
    # The default graph's documents are in no named graph (#195).
    (
        "issue",
        SCOPE,
        "SELECT ?g ?s ?x ?m WHERE { GRAPH ?g { ?s a :Person OPTIONAL { ?s :knows ?x } OPTIONAL { ?x :email ?m } } }",
        "pyoxigraph",
    ),
    ("type", SCOPE, "SELECT ?g ?s WHERE { GRAPH ?g { ?s a :Person } }", "pyoxigraph"),
    ("attribute", SCOPE, "SELECT ?g ?s ?n WHERE { GRAPH ?g { ?s :name ?n } }", "pyoxigraph"),
    ("join", SCOPE, "SELECT ?g ?s ?x ?m WHERE { GRAPH ?g { ?s :knows ?x . ?x :email ?m } }", "pyoxigraph"),
    (
        "union",
        SCOPE,
        "SELECT ?g ?s ?n WHERE { GRAPH ?g { { ?s :name ?n } UNION { ?s :email ?n } } }",
        "pyoxigraph",
    ),
    (
        "count",
        SCOPE,
        "SELECT ?g (COUNT(?s) AS ?c) WHERE { GRAPH ?g { ?s a :Person } } GROUP BY ?g",
        "pyoxigraph",
    ),
    (
        "values_undef",
        SCOPE,
        "SELECT ?g ?s ?n WHERE { VALUES ?g { UNDEF :g2 } GRAPH ?g { ?s :name ?n } }",
        "pyoxigraph",
    ),
    ("outside", SCOPE, "SELECT ?g ?s ?n WHERE { ?s :name ?n . GRAPH ?g { ?s a :Person } }", "pyoxigraph"),
    ("constant", SCOPE, "SELECT ?s ?n WHERE { GRAPH :g1 { ?s a :Person ; :name ?n } }", "pyoxigraph"),
    # A MINUS / EXISTS probe is matched in the scope's graph (#106).
    (
        "exists",
        SCOPE,
        "SELECT ?g ?s WHERE { GRAPH ?g { ?s :email ?e FILTER EXISTS { ?s :name ?n } } }",
        "pyoxigraph",
    ),
    (
        "not_exists",
        SCOPE,
        "SELECT ?g ?s WHERE { GRAPH ?g { ?s :email ?e FILTER NOT EXISTS { ?s :name ?n } } }",
        "pyoxigraph",
    ),
    ("minus", SCOPE, "SELECT ?g ?s WHERE { GRAPH ?g { ?s :email ?e MINUS { ?s :name ?n } } }", "pyoxigraph"),
    (
        "constant_exists",
        SCOPE,
        "SELECT ?s WHERE { GRAPH :g1 { ?s :email ?e FILTER EXISTS { ?s :name ?n } } }",
        "pyoxigraph",
    ),
    (
        "constant_minus",
        SCOPE,
        "SELECT ?s WHERE { GRAPH :g1 { ?s :email ?e MINUS { ?s :name ?n } } }",
        "pyoxigraph",
    ),
    # An OPTIONAL body, inline, as a subquery or as the triples-table scan,
    # is matched in the scope's graph (review of #198).
    (
        "optional_object",
        SCOPE,
        "SELECT ?g ?s ?x ?n WHERE { GRAPH ?g { ?s a :Person ; :knows ?x OPTIONAL { ?x :name ?n } } }",
        "pyoxigraph",
    ),
    (
        "constant_optional_object",
        SCOPE,
        "SELECT ?s ?x ?n WHERE { GRAPH :g1 { ?s a :Person ; :knows ?x OPTIONAL { ?x :name ?n } } }",
        "pyoxigraph",
    ),
    (
        "optional_same",
        SPLIT,
        "SELECT ?g ?s ?e WHERE { GRAPH ?g { ?s :name ?n OPTIONAL { ?s :email ?e } } }",
        "pyoxigraph",
    ),
    (
        "optional_not_bound",
        SPLIT,
        "SELECT ?g ?s WHERE { GRAPH ?g { ?s :email ?e OPTIONAL { ?s :name ?n } FILTER(!BOUND(?n)) } }",
        "pyoxigraph",
    ),
    (
        "optional_constant",
        SPLIT,
        "SELECT ?g ?s ?n WHERE { GRAPH ?g { ?s :email ?e OPTIONAL { :c :name ?n } } }",
        "pyoxigraph",
    ),
    (
        "optional_other_subject",
        SPLIT,
        'SELECT ?g ?s ?t WHERE { GRAPH ?g { ?s :email ?e OPTIONAL { ?t :name "C" } } }',
        "pyoxigraph",
    ),
    (
        "optional_after_values",
        SPLIT,
        "SELECT ?g ?e WHERE { VALUES ?g { :g1 :g2 } GRAPH ?g { OPTIONAL { :a :email ?e } } }",
        "pyoxigraph",
    ),
    (
        "optional_two_triples",
        SPLIT,
        "SELECT ?g ?s ?x ?n WHERE { GRAPH ?g { ?s :email ?e OPTIONAL { ?s :knows ?x . ?x :name ?n } } }",
        "pyoxigraph",
    ),
    (
        "optional_union_body",
        SPLIT,
        "SELECT ?g ?s ?v WHERE { GRAPH ?g { ?s :email ?e OPTIONAL { { ?s :name ?v } UNION { ?s :knows ?v } } } }",
        "pyoxigraph",
    ),
    (
        "optional_bind_body",
        SPLIT,
        "SELECT ?g ?s ?n ?k WHERE { GRAPH ?g { ?s :email ?e OPTIONAL { ?s :name ?n BIND(1 AS ?k) } } }",
        "pyoxigraph",
    ),
    (
        "optional_object_split",
        SPLIT,
        "SELECT ?g ?s ?x ?n WHERE { GRAPH ?g { ?s :knows ?x OPTIONAL { ?x :name ?n } } }",
        "pyoxigraph",
    ),
    # A sub-SELECT is matched in the scope's graph: correlated with ?g after a
    # pattern binds it; first in the group it projects the graph each row
    # came from (W3C subquery/sq01-sq05). rdflib is the oracle (see above).
    (
        "subselect_after",
        SPLIT,
        "SELECT ?g ?s ?n WHERE { GRAPH ?g { ?s :email ?e { SELECT ?s ?n WHERE { ?s :name ?n } } } }",
        "rdflib",
    ),
    (
        "subselect_constant",
        SPLIT,
        "SELECT ?s ?n WHERE { GRAPH :g1 { ?s :email ?e { SELECT ?s ?n WHERE { ?s :name ?n } } } }",
        "rdflib",
    ),
    (
        "subselect_first",
        SPLIT,
        "SELECT ?g ?s ?n WHERE { GRAPH ?g { { SELECT ?s ?n WHERE { ?s :name ?n } } ?s :email ?e } }",
        "rdflib",
    ),
    (
        "subselect_only",
        SPLIT,
        "SELECT ?g ?s WHERE { GRAPH ?g { { SELECT ?s WHERE { ?s :email ?e } } } }",
        "rdflib",
    ),
    (
        "subselect_distinct",
        SPLIT,
        "SELECT ?g ?x WHERE { GRAPH ?g { { SELECT DISTINCT ?x WHERE { ?s :knows ?x } } } }",
        "rdflib",
    ),
    (
        "subselect_limit_after",
        SPLIT,
        "SELECT ?g ?t ?s WHERE { GRAPH ?g { ?t :email ?e { SELECT ?s WHERE { ?s :name ?n } ORDER BY ?s LIMIT 1 } } }",
        "rdflib",
    ),
    (
        "subselect_count_after",
        SPLIT,
        "SELECT ?g ?t ?c WHERE { GRAPH ?g { ?t :email ?e { SELECT (COUNT(?s) AS ?c) WHERE { ?s a :Person } } } }",
        "rdflib",
    ),
    (
        "subselect_own_g",
        SPLIT,
        "SELECT ?s WHERE { GRAPH ?g { { SELECT ?s WHERE { ?s :knows ?g } } } }",
        "rdflib",
    ),
    (
        "subselect_union_body",
        SPLIT,
        "SELECT ?g ?s ?n WHERE { GRAPH ?g { { SELECT ?s ?n WHERE { { ?s :name ?n } UNION { ?s :email ?n } } } } }",
        "rdflib",
    ),
    (
        "subselect_optional_body",
        SPLIT,
        "SELECT ?g ?s ?n WHERE { GRAPH ?g { { SELECT ?s ?n WHERE { ?s a :Person OPTIONAL { ?s :name ?n } } } } }",
        "rdflib",
    ),
    (
        "subselect_exists_body",
        SPLIT,
        "SELECT ?g ?s WHERE { GRAPH ?g { { SELECT ?s WHERE { ?s :email ?e FILTER EXISTS { ?s :name ?n } } } } }",
        "rdflib",
    ),
    (
        "subselect_order_only",
        SPLIT,
        "SELECT ?g ?s WHERE { GRAPH ?g { { SELECT ?s WHERE { ?s :name ?n } ORDER BY ?s } } }",
        "rdflib",
    ),
    (
        "subselect_in_optional",
        SPLIT,
        "SELECT ?g ?s ?n WHERE { GRAPH ?g { ?s :email ?e OPTIONAL { SELECT ?s ?n WHERE { ?s :name ?n } } } }",
        "rdflib",
    ),
    # #144: an aggregated sub-SELECT in GRAPH <iri> under the strict default
    # graph read the default graph instead of :g1.
    (
        "subselect_constant_aggregate_strict",
        SPLIT_STRICT,
        "SELECT ?s ?n WHERE { GRAPH :g1 { { SELECT ?s (COUNT(?e) AS ?n) WHERE { ?s :email ?e } GROUP BY ?s } } }",
        "pyoxigraph",
    ),
]

_CASES: list[tuple[str, Store, str, str]] = [
    (f"{name}_{mapping}", stores[mapping], query, oracle)
    for name, stores, query, oracle in _SHAPES
    for mapping in MAPPINGS
]

#: Shapes the AQL interpreter cannot run (a sub-SELECT, a UNION inside an
#: OPTIONAL sub-query); the live test runs them.
_LIVE_ONLY_SHAPES = {name for name, _s, query, _o in _SHAPES if query.count("SELECT") > 1} | {
    "optional_union_body",
}


def case_params(*, interpreter: bool = False) -> list[Any]:
    out = []
    for name, store, query, oracle in _CASES:
        shape = name.rsplit("_", 1)[0]
        marks: list[Any] = []
        if interpreter and shape in _LIVE_ONLY_SHAPES:
            marks = [pytest.mark.skip(reason="the AQL interpreter cannot run it; checked live")]
        out.append(pytest.param(store, query, oracle, id=name, marks=marks))
    return out


@pytest.mark.parametrize("store, query, oracle", case_params(interpreter=True))
def test_the_translation_agrees_with_the_oracle(store: Store, query: str, oracle: str) -> None:
    result = translate_case(store, query)
    rows = [drop_null_bindings(r) for r in run_aql_subset(result.aql, result.bind_vars, store.docs)]
    assert_bindings_equal(expected_rows(store, query, oracle), rows)


def test_the_split_facts_tell_the_oracles_apart() -> None:
    # Guard: on these facts pyoxigraph and rdflib disagree about a sub-SELECT
    # alone in GRAPH ?g, so the rdflib cases check the per-graph evaluation
    # rather than passing on data where both read the same.
    query = "SELECT ?g ?s ?n WHERE { GRAPH ?g { { SELECT ?s ?n WHERE { ?s :name ?n } } ?s :email ?e } }"
    store = SPLIT["pg"]
    assert sorted(map(str, rdflib_rows(store, query))) != sorted(map(str, oracle_rows(store, query)))
