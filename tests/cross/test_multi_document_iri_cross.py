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
PREFIXES = """@prefix : <http://ex.org/> .
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
    strict: bool = False  # strict_subject_resolution, as the service's mapping sets it
    tenant_id: str | None = None


PG_TWO_CLASSES = Store(
    ontology=PREFIXES
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

#: An edge into a document outside its property's declared range: a walk
#: from that document still finds it (the range is only where copies are
#: looked for).
RANGE_ELSEWHERE = Store(
    ontology=PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" .
:City a owl:Class ; phys:collectionName "City" .
:Place a owl:Class ; phys:collectionName "Place" .
:livesIn a owl:ObjectProperty ; phys:edgeCollectionName "lives" ; rdfs:domain :Person ; rdfs:range :City .
""",
    docs={
        "Person": [{"_id": "Person/a", "_uri": EX + "a"}],
        "City": [],
        "Place": [{"_id": "Place/r", "_uri": EX + "r"}],
        "lives": [{"_id": "lives/1", "_from": "Person/a", "_to": "Place/r"}],
    },
    data=":a a :Person ; :livesIn :r . :r a :Place .",
)

#: As ``tests/w3c/loader.py`` stores ``:a :knows :b . :a a :Agent . :b a :Person . :c a :Person``.
W3C_COPIES = Store(
    ontology=PREFIXES
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
    ontology=PREFIXES
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

#: Edges inside named graphs: ``:a :knows :y`` in the default graph, ``:b
#: :knows :y`` in ``:g``, each edge into that graph's copy of ``:y``.
_NG_DOCS = {
    "Person": [
        {"_id": "Person/y0", "_uri": EX + "y"},
        {"_id": "Person/yg", "_uri": EX + "y", "_graph": EX + "g"},
        {"_id": "Person/a0", "_uri": EX + "a"},
        {"_id": "Person/bg", "_uri": EX + "b", "_graph": EX + "g"},
    ],
    "knows": [
        {"_id": "knows/1", "_from": "Person/a0", "_to": "Person/y0"},
        {"_id": "knows/2", "_from": "Person/bg", "_to": "Person/yg", "_graph": EX + "g"},
    ],
}
_NG_DATA = ":y a :Person . :a a :Person . :a :knows :y .\n:g { :y a :Person . :b a :Person . :b :knows :y . }"
_NG_ONTOLOGY = (
    PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; rdfs:domain :Person ; rdfs:range :Person .
"""
)
NG_EDGES = Store(_NG_ONTOLOGY, _NG_DOCS, _NG_DATA, trig=True)
NG_EDGES_STRICT = Store(
    _NG_ONTOLOGY, _NG_DOCS, _NG_DATA, trig=True, resolver_kwargs={"default_graph_includes_named": False}
)

#: The same IRI in two tenants; the oracle holds tenant t1's facts. ``knows``
#: declares no domain, so nothing else keeps the walk inside the tenant.
TENANTS = Store(
    ontology=PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" ; phys:tenantField "org" ; phys:tenantEntity "Org" .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; rdfs:range :Person .
""",
    docs={
        "Person": [
            {"_id": "Person/y1", "_uri": EX + "y", "org": "t1"},
            {"_id": "Person/y2", "_uri": EX + "y", "org": "t2"},
            {"_id": "Person/a1", "_uri": EX + "a", "org": "t1"},
            {"_id": "Person/b2", "_uri": EX + "b", "org": "t2"},
        ],
        "knows": [
            {"_id": "knows/1", "_from": "Person/a1", "_to": "Person/y1"},
            {"_id": "knows/2", "_from": "Person/b2", "_to": "Person/y2"},
        ],
    },
    data=":y a :Person . :a a :Person . :a :knows :y .",
    strict=True,
    tenant_id="t1",
)

#: A non-strict mapping (the /sparql route's) whose database has no default
#: ``Document`` collection, and an edge with no declared range: the walk must
#: not read a collection the query never names (ERR 1203 live).
NO_DEFAULT_COLLECTION = Store(
    ontology=PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" .
:Company a owl:Class ; phys:collectionName "Company" .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; rdfs:domain :Person .
""",
    docs={
        "Person": [{"_id": "Person/y", "_uri": EX + "y"}, {"_id": "Person/a", "_uri": EX + "a"}],
        "knows": [{"_id": "knows/1", "_from": "Person/a", "_to": "Person/y"}],
    },
    data=":y a :Person . :a a :Person . :a :knows :y .",
)

#: The W3C loader's layout with a symmetric property: one ``:y :knows :x``
#: stored as an edge from each copy of ``:y``; the oracle holds the closure.
W3C_SYMMETRIC = Store(
    ontology=PREFIXES
    + """
:Agent a owl:Class ; phys:collectionName "t_Agent" .
:Person a owl:Class ; phys:collectionName "t_Person" .
:knows a owl:ObjectProperty, owl:SymmetricProperty ; phys:edgeCollectionName "t_knows" .
""",
    docs={
        "t_Document": [{"_id": "t_Document/y", "_uri": EX + "y"}, {"_id": "t_Document/x", "_uri": EX + "x"}],
        "t_Person": [{"_id": "t_Person/y", "_uri": EX + "y"}],
        "t_Agent": [{"_id": "t_Agent/x", "_uri": EX + "x"}],
        "t_knows": [
            {"_id": "t_knows/0", "_from": "t_Document/y", "_to": "t_Document/x"},
            {"_id": "t_knows/1", "_from": "t_Person/y", "_to": "t_Document/x"},
        ],
    },
    data=":y a :Person ; :knows :x . :x a :Agent ; :knows :y .",
    resolver_kwargs={"default_collection": "t_Document"},
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
    ("pg_untyped_reverse", PG_TWO_CLASSES, "SELECT ?x ?s WHERE { ?s a :Employee . ?x :knows ?s }", None),
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
    # No declared range: the walk starts from the bound copy alone (#105 known limit).
    ("w3c_untyped_reverse", W3C_COPIES, "SELECT ?x ?y WHERE { ?y a :Person . ?x :knows ?y }", "#105"),
    ("w3c_const_reverse", W3C_COPIES, "SELECT ?x WHERE { :b a :Person . ?x :knows :b }", "#105"),
    ("pg_const_reverse", PG_TWO_CLASSES, "SELECT ?x WHERE { :s a :Employee . ?x :knows :s }", None),
    ("range_elsewhere_reverse", RANGE_ELSEWHERE, "SELECT ?x WHERE { ?c a :Place . ?x :livesIn ?c }", None),
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
    ("ng_graph_reverse", NG_EDGES, "SELECT ?x WHERE { GRAPH :g { ?y a :Person . ?x :knows ?y } }", None),
    (
        "ng_graph_reverse_const",
        NG_EDGES,
        "SELECT ?x WHERE { GRAPH :g { :y a :Person . ?x :knows :y } }",
        None,
    ),
    ("ng_strict_reverse", NG_EDGES_STRICT, "SELECT ?x ?y WHERE { ?y a :Person . ?x :knows ?y }", None),
    (
        "ng_strict_count",
        NG_EDGES_STRICT,
        "SELECT (COUNT(*) AS ?n) WHERE { ?y a :Person . ?x :knows ?y }",
        None,
    ),
    (
        "ng_strict_optional",
        NG_EDGES_STRICT,
        "SELECT ?y ?x WHERE { ?y a :Person OPTIONAL { ?x :knows ?y } }",
        None,
    ),
    (
        "ng_graph_var_reverse",
        NG_EDGES,
        "SELECT ?gg ?x WHERE { GRAPH ?gg { ?y a :Person . ?x :knows ?y } }",
        "#106",
    ),
    ("tenant_reverse", TENANTS, "SELECT ?x ?y WHERE { ?y a :Person . ?x :knows ?y }", None),
    ("tenant_reverse_const", TENANTS, "SELECT ?x WHERE { :y a :Person . ?x :knows :y }", None),
    (
        "no_default_collection_reverse",
        NO_DEFAULT_COLLECTION,
        "SELECT ?x ?y WHERE { ?y a :Person . ?x :knows ?y }",
        None,
    ),
    ("w3c_symmetric_reverse", W3C_SYMMETRIC, "SELECT ?x ?y WHERE { ?y a :Person . ?x :knows ?y }", None),
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


#: Cases the AQL interpreter cannot run (a ``UNION`` inside an OPTIONAL
#: sub-query); ``tests/integration/test_multi_document_iri_live.py`` runs them.
_LIVE_ONLY = {"ng_strict_optional"}


def case_params(*, interpreter: bool = False) -> list[Any]:
    out = []
    for name, store, query, issue in _CASES:
        marks: list[Any] = [pytest.mark.xfail(strict=True, reason=f"{issue}: wrong on main")] if issue else []
        if interpreter and name in _LIVE_ONLY:
            marks = [pytest.mark.skip(reason="the AQL interpreter cannot run it; checked live")]
        out.append(pytest.param(store, query, id=name, marks=marks))
    return out


def translate_case(store: Store, query: str) -> Any:
    """The case's translation (shared with the live test)."""
    ontology = SchemaResolver.from_turtle(store.ontology).ontology
    resolver = SchemaResolver(
        ontology=ontology, strict_subject_resolution=store.strict, **store.resolver_kwargs
    )
    return translate("PREFIX : <http://ex.org/>\n" + query, resolver=resolver, tenant_id=store.tenant_id)


def oracle_rows(store: Store, query: str) -> list[dict[str, Any]]:
    """pyoxigraph's answer on the case's facts (shared with the live test)."""
    oracle = oxi.Store()
    oracle.load(
        (PREFIXES + store.data).encode(), format=oxi.RdfFormat.TRIG if store.trig else oxi.RdfFormat.TURTLE
    )
    return [normalize_oxi_row(r) for r in oxi_bindings(oracle, "PREFIX : <http://ex.org/>\n" + query)]


@pytest.mark.parametrize("store, query", case_params(interpreter=True))
def test_the_translation_agrees_with_pyoxigraph(store: Store, query: str) -> None:
    result = translate_case(store, query)
    rows = [drop_null_bindings(r) for r in run_aql_subset(result.aql, result.bind_vars, store.docs)]
    assert_bindings_equal(oracle_rows(store, query), rows)
