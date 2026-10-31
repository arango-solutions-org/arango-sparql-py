"""Cross-validation where one IRI is stored as several documents (PRD §6.1).

A SPARQL subject's identity is its IRI, but a store can hold that IRI more
than once, and the translator must join on the IRI rather than on the
document it happens to hold:

* **PG, two classes**: ``:s a :Person, :Employee`` is a document in each
  class collection, each with its own attributes (``name`` / ``salary``).
* **The W3C loader's layout** (``tests/w3c/loader.py``): each subject is in
  ``Document`` and in each class collection, and an edge leaves every copy
  of its source and points at the ``Document`` copy of its target.
* **Named graphs** (ADR-0001): one document per (subject, graph).

PR #101 tested "the document already bound" instead and gave silent wrong
answers in all three; its own cross tests had one document per IRI. Every
case here runs the same SPARQL through pyoxigraph and through the AQL
interpreter. Strict xfails are wrong on ``main`` today and name their issue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.aql_interp import run_aql_subset
from tests.helpers.oxi import assert_bindings_equal, drop_null_bindings, normalize_oxi_row, oxi_bindings

oxi = pytest.importorskip("pyoxigraph", reason="pyoxigraph required for cross tests")

EX = "http://ex.org/"
_PREFIXES = """@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
"""


@dataclass(frozen=True)
class Store:
    ontology: str
    docs: dict[str, list[dict[str, Any]]]
    data: str
    trig: bool = False
    resolver_kwargs: dict[str, Any] = field(default_factory=dict)


PG_TWO_CLASSES = Store(
    ontology=_PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" .
:Employee a owl:Class ; phys:collectionName "Employee" .
:name a owl:DatatypeProperty ; rdfs:domain :Person .
:salary a owl:DatatypeProperty ; rdfs:domain :Employee .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; rdfs:domain :Person ; rdfs:range :Person .
""",
    docs={
        "Person": [
            {"_id": "Person/s", "_uri": EX + "s", "name": "S"},
            {"_id": "Person/a", "_uri": EX + "a", "name": "A"},
        ],
        "Employee": [{"_id": "Employee/s", "_uri": EX + "s", "salary": 10}],
        "knows": [{"_id": "knows/1", "_from": "Person/a", "_to": "Person/s"}],
    },
    data=':s a :Person, :Employee ; :name "S" ; :salary 10 . :a a :Person ; :name "A" ; :knows :s .',
)

#: As ``tests/w3c/loader.py`` stores ``:a :knows :b . :a a :Agent . :b a :Person . :c a :Person``.
W3C_COPIES = Store(
    ontology=_PREFIXES
    + """
:Agent a owl:Class ; phys:collectionName "t_Agent" .
:Person a owl:Class ; phys:collectionName "t_Person" .
:name a owl:DatatypeProperty .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "t_knows" .
""",
    docs={
        "t_Document": [
            {"_id": "t_Document/a", "_uri": EX + "a", "name": "A"},
            {"_id": "t_Document/b", "_uri": EX + "b", "name": "B"},
            {"_id": "t_Document/c", "_uri": EX + "c"},
        ],
        "t_Agent": [{"_id": "t_Agent/a", "_uri": EX + "a", "name": "A"}],
        "t_Person": [
            {"_id": "t_Person/b", "_uri": EX + "b", "name": "B"},
            {"_id": "t_Person/c", "_uri": EX + "c"},
        ],
        "t_knows": [
            {"_id": "t_knows/0", "_from": "t_Document/a", "_to": "t_Document/b"},
            {"_id": "t_knows/1", "_from": "t_Agent/a", "_to": "t_Document/b"},
        ],
    },
    data=':a :knows :b ; :name "A" ; a :Agent . :b a :Person ; :name "B" . :c a :Person .',
    resolver_kwargs={"default_collection": "t_Document"},
)

NAMED_GRAPHS = Store(
    ontology=_PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" .
:name a owl:DatatypeProperty ; rdfs:domain :Person .
:age a owl:DatatypeProperty ; rdfs:domain :Person .
""",
    docs={
        "Person": [
            {"_id": "Person/s0", "_uri": EX + "s", "name": "S"},
            {"_id": "Person/s1", "_uri": EX + "s", "_graph": EX + "g", "age": 3},
        ],
    },
    data=':s a :Person ; :name "S" .\n:g { :s a :Person ; :age 3 . }',
    trig=True,
)
#: pyoxigraph's default graph is the default graph only.
NAMED_GRAPHS_STRICT = Store(
    NAMED_GRAPHS.ontology,
    NAMED_GRAPHS.docs,
    NAMED_GRAPHS.data,
    trig=True,
    resolver_kwargs={"default_graph_includes_named": False},
)

_CASES: list[tuple[str, Store, str, str | None]] = [
    # (id, store, query, issue it is wrong under on main — or None)
    ("pg_two_types", PG_TWO_CLASSES, "SELECT ?s WHERE { ?s a :Person . ?s a :Employee }", None),
    ("pg_edge_then_type", PG_TWO_CLASSES, "SELECT ?s WHERE { :a :knows ?s . ?s a :Employee }", None),
    (
        "pg_exists_type",
        PG_TWO_CLASSES,
        "SELECT ?s WHERE { ?s a :Person FILTER EXISTS { ?s a :Employee } }",
        None,
    ),
    (
        "pg_exists_attr",
        PG_TWO_CLASSES,
        "SELECT ?s WHERE { ?s a :Person FILTER EXISTS { ?s :salary ?x } }",
        None,
    ),
    ("pg_minus_type", PG_TWO_CLASSES, "SELECT ?s WHERE { ?s a :Person MINUS { ?s a :Employee } }", None),
    (
        "pg_union_arm_type",
        PG_TWO_CLASSES,
        'SELECT ?s WHERE { ?s a :Person { ?s a :Employee } UNION { ?s :name "A" } }',
        None,
    ),
    (
        "pg_typed_reverse",
        PG_TWO_CLASSES,
        "SELECT ?x ?s WHERE { ?s a :Employee . ?x :knows ?s . ?x a :Person }",
        None,
    ),
    ("pg_attr_then_type", PG_TWO_CLASSES, "SELECT ?s WHERE { ?s :name ?n . ?s a :Employee }", "#105"),
    ("pg_untyped_reverse", PG_TWO_CLASSES, "SELECT ?x ?s WHERE { ?s a :Employee . ?x :knows ?s }", "#105"),
    ("w3c_const_then_type", W3C_COPIES, "SELECT ?y WHERE { :a :knows ?y . ?y a :Person }", None),
    ("w3c_var_chain", W3C_COPIES, 'SELECT ?x ?y WHERE { ?x :name "A" . ?x :knows ?y . ?y a :Person }', None),
    ("w3c_typed_both", W3C_COPIES, "SELECT ?x ?y WHERE { ?x a :Agent . ?x :knows ?y . ?y a :Person }", None),
    ("w3c_join_groups", W3C_COPIES, "SELECT ?x WHERE { { ?x :name ?n } { ?x a :Person } }", None),
    ("w3c_exists", W3C_COPIES, "SELECT ?y WHERE { :a :knows ?y FILTER EXISTS { ?y a :Person } }", None),
    (
        "w3c_not_exists",
        W3C_COPIES,
        "SELECT ?y WHERE { :a :knows ?y FILTER NOT EXISTS { ?y a :Person } }",
        None,
    ),
    ("w3c_untyped_reverse", W3C_COPIES, "SELECT ?x ?y WHERE { ?y a :Person . ?x :knows ?y }", "#105"),
    ("graph_type", NAMED_GRAPHS, "SELECT ?s WHERE { ?s :name ?n . GRAPH :g { ?s a :Person } }", None),
    (
        "graph_exists_type",
        NAMED_GRAPHS,
        "SELECT ?s WHERE { ?s :name ?n FILTER EXISTS { GRAPH :g { ?s a :Person } } }",
        None,
    ),
    (
        "graph_exists_attr",
        NAMED_GRAPHS,
        "SELECT ?s WHERE { ?s :name ?n FILTER EXISTS { GRAPH :g { ?s :age ?a } } }",
        None,
    ),
    (
        "graph_not_exists_attr",
        NAMED_GRAPHS,
        "SELECT ?s WHERE { ?s :name ?n FILTER NOT EXISTS { GRAPH :g { ?s :age ?a } } }",
        None,
    ),
    (
        "graph_minus_attr",
        NAMED_GRAPHS,
        "SELECT ?s WHERE { ?s :name ?n MINUS { GRAPH :g { ?s :age ?a } } }",
        None,
    ),
    (
        "graph_exists_var",
        NAMED_GRAPHS,
        "SELECT ?s WHERE { ?s :name ?n FILTER EXISTS { GRAPH ?gg { ?s :age ?a } } }",
        None,
    ),
    (
        "graph_inner_then_type",
        NAMED_GRAPHS_STRICT,
        "SELECT ?s WHERE { GRAPH :g { ?s :age ?a } ?s a :Person }",
        None,
    ),
    (
        "graph_var_type",
        NAMED_GRAPHS,
        "SELECT ?s ?gr WHERE { ?s :name ?n . GRAPH ?gr { ?s a :Person } }",
        "#106",
    ),
    (
        "graph_exists_inside_graph",
        NAMED_GRAPHS,
        "SELECT ?s WHERE { GRAPH :g { ?s :age ?a FILTER EXISTS { ?s :name ?n } } }",
        "#106",
    ),
]


def _params() -> list[Any]:
    out = []
    for name, store, query, issue in _CASES:
        marks = [pytest.mark.xfail(strict=True, reason=f"{issue}: wrong on main")] if issue else []
        out.append(pytest.param(store, query, id=name, marks=marks))
    return out


@pytest.mark.parametrize("store, query", _params())
def test_the_translation_agrees_with_pyoxigraph(store: Store, query: str) -> None:
    sparql = "PREFIX : <http://ex.org/>\n" + query
    resolver = SchemaResolver.from_turtle(store.ontology, **store.resolver_kwargs)
    result = translate(sparql, resolver=resolver)
    rows = [drop_null_bindings(r) for r in run_aql_subset(result.aql, result.bind_vars, store.docs)]

    oracle = oxi.Store()
    oracle.load(
        (_PREFIXES + store.data).encode(), format=oxi.RdfFormat.TRIG if store.trig else oxi.RdfFormat.TURTLE
    )
    expected = [normalize_oxi_row(r) for r in oxi_bindings(oracle, sparql)]
    assert_bindings_equal(expected, rows)
