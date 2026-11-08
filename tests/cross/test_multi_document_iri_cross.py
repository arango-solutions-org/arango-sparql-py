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
    # The oracle reads the default graph as the union of every graph (the
    # lax default graph); otherwise as the default graph only.
    union_default_graph: bool = False


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

#: As PG_TWO_CLASSES, with the attribute on both copies (a replica layout):
#: reading the domain copy too must not repeat the value (review of #141).
PG_TWO_CLASSES_REPLICA = Store(
    ontology=PG_TWO_CLASSES.ontology,
    docs={
        **PG_TWO_CLASSES.docs,
        "Person": [
            {"_id": "Person/s", "_uri": EX + "s", "name": "S", "salary": 10},
            {"_id": "Person/a", "_uri": EX + "a", "name": "A"},
        ],
    },
    data=PG_TWO_CLASSES.data,
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

#: The object bound outside ``GRAPH :g`` (its default-graph copy) and walked
#: to inside it: the edge in ``:g`` points at ``:p1``'s copy in ``:g``.
_OUTSIDE_ONTOLOGY = (
    PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" .
:Post a owl:Class ; phys:collectionName "Post" .
:name a owl:DatatypeProperty ; rdfs:domain :Person .
:hasCreator a owl:ObjectProperty ; phys:edgeCollectionName "hasCreator" ; rdfs:domain :Post ;
    rdfs:range :Person .
"""
)
_OUTSIDE_DOCS = {
    "Person": [
        {"_id": "Person/p0", "_uri": EX + "p1", "name": "P1"},
        {"_id": "Person/pg", "_uri": EX + "p1", "_graph": EX + "g"},
    ],
    "Post": [
        {"_id": "Post/m1", "_uri": EX + "m1"},
        {"_id": "Post/m2", "_uri": EX + "m2", "_graph": EX + "g"},
    ],
    "hasCreator": [
        {"_id": "hasCreator/1", "_from": "Post/m1", "_to": "Person/p0"},
        {"_id": "hasCreator/2", "_from": "Post/m2", "_to": "Person/pg", "_graph": EX + "g"},
    ],
}
_OUTSIDE_DATA = (
    ':p1 a :Person ; :name "P1" . :m1 a :Post ; :hasCreator :p1 .\n'
    ":g { :p1 a :Person . :m2 a :Post ; :hasCreator :p1 . }"
)
GRAPH_OUTSIDE = Store(_OUTSIDE_ONTOLOGY, _OUTSIDE_DOCS, _OUTSIDE_DATA, trig=True)
GRAPH_OUTSIDE_STRICT = Store(
    _OUTSIDE_ONTOLOGY,
    _OUTSIDE_DOCS,
    _OUTSIDE_DATA,
    trig=True,
    resolver_kwargs={"default_graph_includes_named": False},
)

#: One IRI as a document per graph, read inside and outside ``GRAPH :g``
#: (#119): a document read on one side of the boundary is not reused on the
#: other. Strict default graph; with and without domains on the properties.
_SCOPE_ONTOLOGY = (
    PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" .
:name a owl:DatatypeProperty ; rdfs:domain :Person .
:age a owl:DatatypeProperty ; rdfs:domain :Person .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; rdfs:domain :Person ; rdfs:range :Person .
"""
)
_SCOPE_DOCS = {
    "Person": [
        {"_id": "Person/s0", "_uri": EX + "s", "name": "S-default"},
        {"_id": "Person/sg", "_uri": EX + "s", "_graph": EX + "g", "name": "S-g", "age": 5},
        {"_id": "Person/t0", "_uri": EX + "t", "name": "T"},
    ],
    "knows": [{"_id": "knows/1", "_from": "Person/s0", "_to": "Person/t0"}],
}
_SCOPE_DATA = (
    ':s a :Person ; :name "S-default" ; :knows :t . :t a :Person ; :name "T" .\n'
    ':g { :s a :Person ; :name "S-g" ; :age 5 . }'
)
_STRICT = {"default_graph_includes_named": False, "default_collection": "Person"}
GRAPH_SCOPES = Store(_SCOPE_ONTOLOGY, _SCOPE_DOCS, _SCOPE_DATA, trig=True, resolver_kwargs=_STRICT)
GRAPH_SCOPES_NO_DOMAIN = Store(
    _SCOPE_ONTOLOGY.replace(" ; rdfs:domain :Person .\n:age", " .\n:age").replace(
        ":age a owl:DatatypeProperty ; rdfs:domain :Person .", ":age a owl:DatatypeProperty ."
    ),
    _SCOPE_DOCS,
    _SCOPE_DATA,
    trig=True,
    resolver_kwargs=_STRICT,
)

#: A subject whose only document is in ``:g``, read through the union
#: default graph, then inside ``GRAPH :g`` by a property with no domain
#: (review R128-1): it is re-read in its own collection, not the (empty)
#: default one.
GRAPH_ONLY_NO_DOMAIN = Store(
    ontology=PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" .
:name a owl:DatatypeProperty .
""",
    docs={
        "Person": [{"_id": "Person/s", "_uri": EX + "s", "_graph": EX + "g", "name": "S"}],
        "Document": [],
    },
    data=':s a :Person .\n:g { :s :name "S" . }',
    trig=True,
    resolver_kwargs={"default_collection": "Document"},
)

#: A subject reached by an edge outside ``GRAPH :g``, read inside it by a
#: property with no domain: re-read from the default collection (where its
#: ``:g`` copy is), not from the edge collection its vertex came through.
GRAPH_EDGE_REACHED = Store(
    ontology=PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" .
:Org a owl:Class ; phys:collectionName "Person" .
:name a owl:DatatypeProperty .
:age a owl:DatatypeProperty .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; rdfs:domain :Person ; rdfs:range :Person .
:worksFor a owl:ObjectProperty ; phys:edgeCollectionName "worksFor" ; rdfs:domain :Person .
""",
    docs={
        "Person": [
            {"_id": "Person/s0", "_uri": EX + "s"},
            {"_id": "Person/t0", "_uri": EX + "t", "name": "T"},
            {"_id": "Person/tg", "_uri": EX + "t", "_graph": EX + "g", "name": "T-g", "age": 7},
        ],
        "knows": [{"_id": "knows/1", "_from": "Person/s0", "_to": "Person/t0"}],
        "worksFor": [{"_id": "worksFor/1", "_from": "Person/s0", "_to": "Person/t0"}],
    },
    data=':s a :Person ; :knows :t ; :worksFor :t . :t a :Person ; :name "T" .\n'
    ':g { :t a :Person ; :name "T-g" ; :age 7 . }',
    trig=True,
    resolver_kwargs=_STRICT,
)
#: As above, with the default collection empty: a vertex reached by an edge
#: is re-read in its property's range, not there.
GRAPH_EDGE_REACHED_EMPTY_DEFAULT = Store(
    GRAPH_EDGE_REACHED.ontology,
    {**GRAPH_EDGE_REACHED.docs, "Document": []},
    GRAPH_EDGE_REACHED.data,
    trig=True,
    resolver_kwargs={"default_graph_includes_named": False},
)

#: A repeated rdfs:domain / rdfs:range (#122): the shape arango-query-core's
#: ``mapping_to_turtle`` and the schema analyzer's ``owl_export`` write for a
#: property shared by several classes. Any of them holds subjects (objects).
REPEATED_RANGE_DOMAIN = Store(
    ontology=PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" .
:Company a owl:Class ; phys:collectionName "Company" .
:Post a owl:Class ; phys:collectionName "Post" .
:Comment a owl:Class ; phys:collectionName "Comment" .
:name a owl:DatatypeProperty ; rdfs:domain :Person ; rdfs:domain :Company .
:likes a owl:ObjectProperty ; phys:edgeCollectionName "likes" ; rdfs:domain :Person ;
    rdfs:range :Post ; rdfs:range :Comment .
:hasCreator a owl:ObjectProperty ; phys:edgeCollectionName "hasCreator" ;
    rdfs:domain :Post ; rdfs:domain :Comment ; rdfs:range :Person .
""",
    docs={
        "Person": [
            {"_id": "Person/a", "_uri": EX + "a", "name": "A"},
            {"_id": "Person/b", "_uri": EX + "b", "name": "B"},
        ],
        "Company": [{"_id": "Company/c", "_uri": EX + "c", "name": "C"}],
        "Post": [{"_id": "Post/m1", "_uri": EX + "m1"}],
        "Comment": [{"_id": "Comment/k1", "_uri": EX + "k1"}],
        "likes": [
            {"_id": "likes/1", "_from": "Person/a", "_to": "Post/m1"},
            {"_id": "likes/2", "_from": "Person/b", "_to": "Comment/k1"},
        ],
        "hasCreator": [
            {"_id": "hasCreator/1", "_from": "Post/m1", "_to": "Person/a"},
            {"_id": "hasCreator/2", "_from": "Comment/k1", "_to": "Person/a"},
        ],
    },
    data=':a a :Person ; :name "A" ; :likes :m1 . :b a :Person ; :name "B" ; :likes :k1 . '
    ':c a :Company ; :name "C" . :m1 a :Post ; :hasCreator :a . :k1 a :Comment ; :hasCreator :a .',
    resolver_kwargs={"default_collection": "Person"},
)

#: The repeated range in named graphs: an object found by a lookup across
#: the range's collections binds an unbound ``GRAPH ?g`` (it raised
#: AqlEmitError: "?g was never bound").
REPEATED_RANGE_GRAPHS = Store(
    ontology=REPEATED_RANGE_DOMAIN.ontology,
    docs={
        "Person": [
            {"_id": "Person/a", "_uri": EX + "a", "_graph": EX + "g"},
            {"_id": "Person/b", "_uri": EX + "b", "_graph": EX + "g"},
            {"_id": "Person/c", "_uri": EX + "c", "_graph": EX + "h"},
        ],
        "Post": [{"_id": "Post/m1", "_uri": EX + "m1", "_graph": EX + "g"}],
        "Comment": [
            {"_id": "Comment/k1", "_uri": EX + "k1", "_graph": EX + "g"},
            {"_id": "Comment/k1h", "_uri": EX + "k1", "_graph": EX + "h"},
        ],
        "likes": [
            {"_id": "likes/1", "_from": "Person/a", "_to": "Post/m1", "_graph": EX + "g"},
            {"_id": "likes/2", "_from": "Person/b", "_to": "Comment/k1", "_graph": EX + "g"},
            {"_id": "likes/3", "_from": "Person/c", "_to": "Comment/k1h", "_graph": EX + "h"},
        ],
    },
    data=":g { :a a :Person ; :likes :m1 . :b a :Person ; :likes :k1 . :m1 a :Post . :k1 a :Comment . }\n"
    ":h { :c a :Person ; :likes :k1 . :k1 a :Comment . }",
    trig=True,
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

#: An OPTIONAL body shares with the outer pattern only the variables it binds
#: (#125): a BIND, a nested group's FILTER or a sub-select in it must not read
#: ``?n``, which only the outer pattern binds. Its top-level FILTER is the
#: left join's condition and does.
OPTIONAL_SCOPE = Store(
    ontology=PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" .
:name a owl:DatatypeProperty ; rdfs:domain :Person .
:age a owl:DatatypeProperty ; rdfs:domain :Person .
:nick a owl:DatatypeProperty ; rdfs:domain :Person .
""",
    docs={
        "Person": [
            {"_id": "Person/a", "_uri": EX + "a", "name": "A", "age": 3, "nick": "A"},
            {"_id": "Person/b", "_uri": EX + "b", "name": "B", "age": 4, "nick": "X"},
        ],
    },
    data=':a a :Person ; :name "A" ; :age 3 ; :nick "A" . :b a :Person ; :name "B" ; :age 4 ; :nick "X" .',
)

#: A domain or range naming an undeclared class (#126): arango-query-core
#: writes ``rdfs:domain :Any`` for a relationship with no ``fromEntity``. It
#: routes nothing; the subject falls back to the default collection, as in
#: 0.5.0 (0.6.0 raised SchemaResolutionError).
UNDECLARED_DOMAIN = Store(
    ontology=PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" .
:name a owl:DatatypeProperty ; rdfs:domain :Any .
:age a owl:DatatypeProperty ; rdfs:domain :Person, :Any .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; rdfs:domain :Any ; rdfs:range :Any .
""",
    docs={
        "Person": [
            {"_id": "Person/a", "_uri": EX + "a", "name": "A", "age": 3},
            {"_id": "Person/b", "_uri": EX + "b", "name": "B"},
        ],
        "knows": [{"_id": "knows/1", "_from": "Person/a", "_to": "Person/b"}],
    },
    data=':a a :Person ; :name "A" ; :age 3 ; :knows :b . :b a :Person ; :name "B" .',
    resolver_kwargs={"default_collection": "Person"},
)

#: A domain naming declared classes and an undeclared one (review of #138):
#: the undeclared class stands for the default collection, beside the
#: declared ones; none of them is dropped.
UNDECLARED_MIXED_DOMAIN = Store(
    ontology=PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" .
:Org a owl:Class ; phys:collectionName "Org" .
:age a owl:DatatypeProperty ; rdfs:domain :Person, :Org, :Any .
""",
    docs={
        "Person": [{"_id": "Person/a", "_uri": EX + "a", "age": 3}],
        "Org": [{"_id": "Org/o", "_uri": EX + "o", "age": 9}],
        "Document": [{"_id": "Document/z", "_uri": EX + "z", "age": 1}],
    },
    data=":a a :Person ; :age 3 . :o a :Org ; :age 9 . :z :age 1 .",
)

#: A PG entity in two member collections of a supertype / union domain
#: (#124): ``:s`` holds the same name in both, ``:d`` a different one in each,
#: ``:m`` a name in Person only. One IRI is one subject: ``:s`` comes back
#: once, ``:d`` once per name.
UNION_COPIES = Store(
    ontology=PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" ; rdfs:subClassOf :Agent .
:Org a owl:Class ; phys:collectionName "Org" ; rdfs:subClassOf :Agent .
:Agent a owl:Class ; phys:mappingStyle "UNION" .
:Employee a owl:Class ; phys:collectionName "Employee" .
:name a owl:DatatypeProperty ; rdfs:domain [ owl:unionOf ( :Person :Org ) ] .
:age a owl:DatatypeProperty ; rdfs:domain :Agent .
:salary a owl:DatatypeProperty ; rdfs:domain [ owl:unionOf ( :Org :Employee ) ] .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; rdfs:domain :Person .
""",
    docs={
        "Person": [
            {"_id": "Person/s", "_uri": EX + "s", "name": "S", "age": 3},
            {"_id": "Person/d", "_uri": EX + "d", "name": "D1"},
            {"_id": "Person/m", "_uri": EX + "m", "name": "M"},
            {"_id": "Person/g", "_uri": EX + "g1", "_graph": EX + "g", "name": "G"},
            {"_id": "Person/x", "_uri": EX + "x", "name": "X"},
        ],
        "Org": [
            {"_id": "Org/s", "_uri": EX + "s", "name": "S", "age": 3},
            {"_id": "Org/d", "_uri": EX + "d", "name": "D2"},
            {"_id": "Org/m", "_uri": EX + "m"},
            {"_id": "Org/g", "_uri": EX + "g1", "_graph": EX + "g", "name": "G"},
        ],
        "Employee": [{"_id": "Employee/s", "_uri": EX + "s", "salary": 10}],
        "knows": [{"_id": "knows/1", "_from": "Person/x", "_to": "Person/s"}],
    },
    data=':s a :Person, :Org, :Agent, :Employee ; :name "S" ; :age 3 ; :salary 10 . '
    ':d a :Person, :Org, :Agent ; :name "D1", "D2" . :m a :Person, :Org, :Agent ; :name "M" . '
    ':x a :Person, :Agent ; :name "X" ; :knows :s .\n:g { :g1 a :Person, :Org, :Agent ; :name "G" . }',
    trig=True,
    resolver_kwargs={"default_graph_includes_named": False},
)

#: As UNION_COPIES' :s, with edges into the Person copy only: ``:knows``
#: declares the range Person, ``:likes`` the supertype Agent (#149).
UNION_EDGE_COPIES = Store(
    ontology=PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" ; rdfs:subClassOf :Agent .
:Org a owl:Class ; phys:collectionName "Org" ; rdfs:subClassOf :Agent .
:Agent a owl:Class ; phys:mappingStyle "UNION" .
:name a owl:DatatypeProperty ; rdfs:domain [ owl:unionOf ( :Person :Org ) ] .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; rdfs:domain :Person ; rdfs:range :Person .
:likes a owl:ObjectProperty ; phys:edgeCollectionName "likes" ; rdfs:domain :Person ; rdfs:range :Agent .
""",
    docs={
        "Person": [
            {"_id": "Person/s", "_uri": EX + "s", "name": "S"},
            {"_id": "Person/a", "_uri": EX + "a", "name": "A"},
        ],
        "Org": [{"_id": "Org/s", "_uri": EX + "s", "name": "S"}],
        "knows": [{"_id": "knows/1", "_from": "Person/a", "_to": "Person/s"}],
        "likes": [{"_id": "likes/1", "_from": "Person/a", "_to": "Person/s"}],
    },
    data=':s a :Person, :Org, :Agent ; :name "S" . :a a :Person, :Agent ; :name "A" ; :knows :s ; :likes :s .',
)

#: As UNION_EDGE_COPIES, with ``:a`` in Org too: only its Person copy has
#: the edges out, so a union row of ``:a`` depends on its copy for them.
UNION_EDGE_OUT_COPIES = Store(
    ontology=UNION_EDGE_COPIES.ontology,
    docs={
        **UNION_EDGE_COPIES.docs,
        "Org": [*UNION_EDGE_COPIES.docs["Org"], {"_id": "Org/a", "_uri": EX + "a", "name": "A"}],
    },
    data=UNION_EDGE_COPIES.data + " :a a :Org .",
)

#: List values under ``fan_out_list_values`` (one triple per element):
#: ``:s``'s copies share the tag "b", which is one triple, not two.
UNION_LIST_COPIES = Store(
    ontology=PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" ; rdfs:subClassOf :Agent .
:Org a owl:Class ; phys:collectionName "Org" ; rdfs:subClassOf :Agent .
:Agent a owl:Class ; phys:mappingStyle "UNION" .
:tag a owl:DatatypeProperty ; rdfs:domain [ owl:unionOf ( :Person :Org ) ] .
""",
    docs={
        "Person": [{"_id": "Person/s", "_uri": EX + "s", "tag": ["a", "b"]}],
        "Org": [{"_id": "Org/s", "_uri": EX + "s", "tag": ["b", "c"]}, {"_id": "Org/t", "_uri": EX + "t"}],
    },
    data=':s a :Person, :Org, :Agent ; :tag "a", "b", "c" . :t a :Org, :Agent .',
    resolver_kwargs={"fan_out_list_values": True},
)

#: Copies of ``:s`` in two named graphs, read through the lax default graph
#: (the union of every graph): two quads, so ``:s`` comes back once per
#: graph, as pyoxigraph and main read it (review of #146).
UNION_GRAPH_COPIES = Store(
    ontology=UNION_COPIES.ontology,
    docs={
        "Person": [{"_id": "Person/s", "_uri": EX + "s", "_graph": EX + "g2", "name": "S"}],
        "Org": [{"_id": "Org/s", "_uri": EX + "s", "_graph": EX + "g1", "name": "S"}],
    },
    data=':g1 { :s a :Org, :Agent ; :name "S" . }\n:g2 { :s a :Person, :Agent ; :name "S" . }',
    trig=True,
    union_default_graph=True,
)

#: A symmetric property with a self-loop: ``:a :knows :a`` is one triple,
#: however many ends the edge has (#121). The oracle holds the closure.
SYMMETRIC_SELF_LOOP = Store(
    ontology=PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" .
:knows a owl:ObjectProperty, owl:SymmetricProperty ; phys:edgeCollectionName "knows" ;
    rdfs:domain :Person ; rdfs:range :Person .
""",
    docs={
        "Person": [{"_id": "Person/a", "_uri": EX + "a"}, {"_id": "Person/b", "_uri": EX + "b"}],
        "knows": [
            {"_id": "knows/0", "_from": "Person/a", "_to": "Person/a"},
            {"_id": "knows/1", "_from": "Person/a", "_to": "Person/b"},
        ],
    },
    data=":a a :Person ; :knows :a, :b . :b a :Person ; :knows :a .",
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

#: ``likes`` (range Person, no domain) from a Person and from a Robot: a walk
#: into ``?x a :Person`` keeps only the Person's edge, as the class scan does.
CLASS_FILTER = Store(
    ontology=PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" .
:Robot a owl:Class ; phys:collectionName "Robot" .
:likes a owl:ObjectProperty ; phys:edgeCollectionName "likes" ; rdfs:range :Person .
""",
    docs={
        "Person": [{"_id": "Person/y", "_uri": EX + "y"}, {"_id": "Person/a", "_uri": EX + "a"}],
        "Robot": [{"_id": "Robot/r", "_uri": EX + "r"}],
        "likes": [
            {"_id": "likes/1", "_from": "Person/a", "_to": "Person/y"},
            {"_id": "likes/2", "_from": "Robot/r", "_to": "Person/y"},
        ],
    },
    data=":y a :Person . :a a :Person ; :likes :y . :r a :Robot ; :likes :y .",
)

#: TENANTS plus an edge from tenant t2's ``:b`` into tenant t1's ``:y``: a
#: walk into ``?x a :Person`` applies the tenant FILTER to ``?x`` as the
#: class scan does.
TENANTS_CROSS_EDGE = Store(
    TENANTS.ontology,
    {
        **TENANTS.docs,
        "knows": [*TENANTS.docs["knows"], {"_id": "knows/3", "_from": "Person/b2", "_to": "Person/y1"}],
    },
    TENANTS.data,
    strict=True,
    tenant_id="t1",
)

#: Every class tenant-scoped (issue #118): documents of tenants T1 and T2,
#: edges between them, and a reified relationship (D8). The oracle holds what
#: tenant T1 may see: its own documents and the edges between them.
_TENANT_ONTOLOGY = (
    PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" ; phys:tenantField "tenant" .
:Forum a owl:Class ; phys:collectionName "Forum" ; phys:tenantField "tenant" .
:Post a owl:Class ; phys:collectionName "Post" ; phys:tenantField "tenant" .
:Comment a owl:Class ; phys:collectionName "Comment" ; phys:tenantField "tenant" .
:Message a owl:Class ; phys:mappingStyle "UNION" .
:Post rdfs:subClassOf :Message . :Comment rdfs:subClassOf :Message .
:name a owl:DatatypeProperty ; rdfs:domain :Person .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; rdfs:domain :Person ; rdfs:range :Person .
:hasCreator a owl:ObjectProperty ; phys:edgeCollectionName "hasCreator" ;
    rdfs:domain [ a owl:Class ; owl:unionOf ( :Post :Comment ) ] ; rdfs:range :Person .
:hasMember a owl:ObjectProperty ; phys:edgeCollectionName "hasMember" ; phys:reifiedEdge "true" ; rdfs:domain :Forum .
:hasPerson a owl:ObjectProperty ; phys:edgeTargetOf :hasMember ; rdfs:range :Person .
:joinDate a owl:DatatypeProperty ; rdfs:domain :hasMember .
"""
)
TENANT_WALKS = Store(
    ontology=_TENANT_ONTOLOGY,
    docs={
        "Person": [
            {"_id": "Person/a", "_uri": EX + "a", "name": "A", "tenant": "T1"},
            {"_id": "Person/b", "_uri": EX + "b", "name": "B", "tenant": "T2"},
            {"_id": "Person/c", "_uri": EX + "c", "name": "C", "tenant": "T1"},
        ],
        "Forum": [
            {"_id": "Forum/f1", "_uri": EX + "f1", "tenant": "T1"},
            {"_id": "Forum/f2", "_uri": EX + "f2", "tenant": "T2"},
        ],
        "Post": [
            {"_id": "Post/m1", "_uri": EX + "m1", "tenant": "T1"},
            {"_id": "Post/m2", "_uri": EX + "m2", "tenant": "T2"},
        ],
        "Comment": [{"_id": "Comment/k1", "_uri": EX + "k1", "tenant": "T2"}],
        "knows": [
            {"_id": "knows/1", "_from": "Person/a", "_to": "Person/b"},
            {"_id": "knows/2", "_from": "Person/b", "_to": "Person/c"},
        ],
        "hasCreator": [
            {"_id": "hasCreator/1", "_from": "Post/m1", "_to": "Person/a"},
            {"_id": "hasCreator/2", "_from": "Post/m2", "_to": "Person/a"},
            {"_id": "hasCreator/3", "_from": "Comment/k1", "_to": "Person/a"},
        ],
        "hasMember": [
            {
                "_id": "hasMember/1",
                "_uri": EX + "hm1",
                "_from": "Forum/f1",
                "_to": "Person/b",
                "joinDate": "2020",
            },
            {
                "_id": "hasMember/2",
                "_uri": EX + "hm2",
                "_from": "Forum/f2",
                "_to": "Person/a",
                "joinDate": "2021",
            },
        ],
    },
    data=':a a :Person ; :name "A" . :c a :Person ; :name "C" . :f1 a :Forum . '
    ":m1 a :Post, :Message ; :hasCreator :a .",
    tenant_id="T1",
)

#: A label reused across collections (review P6): ``:Person`` is
#: tenant-scoped in ``Nodes``, while ``:Contact`` in ``Things`` carries the
#: same label value and no tenant. ``Things/d2`` is another tenant's device.
TENANT_SHARED_LABEL = Store(
    ontology=PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Nodes" ; phys:typeField "label" ; phys:typeValue "Person" ;
    phys:tenantField "tenant" .
:Device a owl:Class ; phys:collectionName "Things" ; phys:typeField "label" ; phys:typeValue "Device" ;
    phys:tenantField "tenant" .
:Contact a owl:Class ; phys:collectionName "Things" ; phys:typeField "label" ; phys:typeValue "Person" .
:owns a owl:ObjectProperty ; phys:edgeCollectionName "owns" .
""",
    docs={
        "Nodes": [{"_id": "Nodes/a", "_uri": EX + "a", "label": "Person", "tenant": "T1"}],
        "Things": [
            {"_id": "Things/c1", "_uri": EX + "c1", "label": "Person"},
            {"_id": "Things/d1", "_uri": EX + "d1", "label": "Device", "tenant": "T1"},
            {"_id": "Things/d2", "_uri": EX + "d2", "label": "Device", "tenant": "T2"},
        ],
        "owns": [
            {"_id": "owns/1", "_from": "Nodes/a", "_to": "Things/c1"},
            {"_id": "owns/2", "_from": "Nodes/a", "_to": "Things/d1"},
            {"_id": "owns/3", "_from": "Nodes/a", "_to": "Things/d2"},
        ],
    },
    data=":a a :Person ; :owns :c1, :d1 . :c1 a :Contact . :d1 a :Device .",
    tenant_id="T1",
)

#: LPG: one collection, classes as labels. ``:c`` is a Person and an Employee.
LPG_LABELS = Store(
    ontology=PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "nodes" ; phys:mappingStyle "LABEL" ;
    phys:typeField "labels" ; phys:typeFieldIsArray "true" ; phys:typeValue "Person" .
:Employee a owl:Class ; phys:collectionName "nodes" ; phys:mappingStyle "LABEL" ;
    phys:typeField "labels" ; phys:typeFieldIsArray "true" ; phys:typeValue "Employee" .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "edges" ; phys:mappingStyle "GENERIC_WITH_TYPE" ;
    phys:typeField "type" ; phys:typeValue "KNOWS" ; rdfs:domain :Person ; rdfs:range :Person .
""",
    docs={
        "nodes": [
            {"_id": "nodes/y", "_uri": EX + "y", "labels": ["Person"]},
            {"_id": "nodes/a", "_uri": EX + "a", "labels": ["Person"]},
            {"_id": "nodes/c", "_uri": EX + "c", "labels": ["Person", "Employee"]},
        ],
        "edges": [
            {"_id": "edges/1", "_from": "nodes/a", "_to": "nodes/y", "type": "KNOWS"},
            {"_id": "edges/2", "_from": "nodes/c", "_to": "nodes/y", "type": "KNOWS"},
            {"_id": "edges/3", "_from": "nodes/y", "_to": "nodes/a", "type": "LIKES"},
        ],
    },
    data=":y a :Person . :a a :Person ; :knows :y . :c a :Person, :Employee ; :knows :y .",
)

_CASES: list[tuple[str, Store, str, str | None]] = [
    # (id, store, query, issue it is wrong under on main — or None)
    *[
        (f"union_copies_{name}", UNION_COPIES, query, None)
        for name, query in [
            ("untyped_attribute", "SELECT ?s ?n WHERE { ?s :name ?n }"),
            ("constant_attribute", "SELECT ?n WHERE { :s :name ?n }"),
            ("count", "SELECT (COUNT(*) AS ?c) WHERE { ?s :name ?n }"),
            ("supertype", "SELECT ?s WHERE { ?s a :Agent }"),
            ("supertype_attribute", "SELECT ?s ?n WHERE { ?s a :Agent ; :name ?n }"),
            ("two_attributes", "SELECT ?s ?n ?a WHERE { ?s :name ?n ; :age ?a }"),
            ("graph", "SELECT ?s ?n WHERE { GRAPH :g { ?s :name ?n } }"),
            # Review of #146: only the Person copy has the edge, so both
            # copies stay; so do the copies of an attribute read from another
            # copy (#141).
            ("incoming_edge", "SELECT ?x ?s WHERE { ?s a :Agent . ?x :knows ?s }"),
            ("incoming_path", "SELECT ?x ?s WHERE { ?s a :Agent . ?x :knows+ ?s }"),
            ("inverse_path", "SELECT ?s ?x WHERE { ?s a :Agent . ?s ^:knows ?x }"),
            ("attribute_from_another_copy", "SELECT ?s ?y WHERE { ?s a :Agent ; :salary ?y }"),
        ]
    ],
    *[
        (f"union_copies_{name}", UNION_COPIES, query, None)
        for name, query in [
            # #147: the value is on one copy, or on a third (Employee).
            ("optional_attribute", "SELECT ?s ?n WHERE { ?s a :Agent OPTIONAL { ?s :name ?n } }"),
            ("optional_attribute_elsewhere", "SELECT ?s ?y WHERE { ?s a :Agent OPTIONAL { ?s :salary ?y } }"),
            ("optional_constant", "SELECT ?n WHERE { :m a :Agent OPTIONAL { :m :name ?n } }"),
            (
                "optional_filter",
                'SELECT ?s ?n WHERE { ?s a :Agent OPTIONAL { ?s :name ?n FILTER(?n != "D1") } }',
            ),
            ("optional_count", "SELECT (COUNT(*) AS ?c) WHERE { ?s a :Agent OPTIONAL { ?s :name ?n } }"),
            # A UNION repeats ?s legitimately; the OPTIONAL then reads every copy.
            (
                "optional_after_union",
                "SELECT ?s ?n WHERE { { ?s a :Agent } UNION { ?s a :Person } OPTIONAL { ?s :name ?n } }",
            ),
            # #148: a sub-select joined to the union's subject.
            ("subselect", "SELECT ?s ?n WHERE { ?s a :Agent { SELECT ?s ?n WHERE { ?s :name ?n } } }"),
            # Read from every copy in the BGP too: a constant, a bound value,
            # an EXISTS body; a second type pattern joins on the IRI.
            ("second_type", "SELECT ?s WHERE { ?s a :Agent , :Person }"),
            ("constant_elsewhere", "SELECT ?s WHERE { ?s a :Agent ; :salary 10 }"),
            ("bound_elsewhere", "SELECT ?s WHERE { VALUES ?y { 10 } ?s a :Agent ; :salary ?y }"),
            ("exists_elsewhere", "SELECT ?s WHERE { ?s a :Agent FILTER EXISTS { ?s :salary ?y } }"),
            ("not_exists", "SELECT ?s WHERE { ?s a :Agent FILTER NOT EXISTS { ?s :name ?n } }"),
        ]
    ],
    *[
        (f"union_edge_copies_{name}", UNION_EDGE_COPIES, query, None)
        for name, query in [
            # #149: the edge points at the Person copy only.
            ("constant_source", "SELECT ?s WHERE { ?s a :Agent . :a :knows ?s }"),
            ("range_person", "SELECT ?x ?s WHERE { ?s a :Agent . ?x :knows ?s }"),
            ("range_supertype", "SELECT ?x ?s WHERE { ?s a :Agent . ?x :likes ?s }"),
            ("forward", "SELECT ?s ?x WHERE { ?s a :Agent . ?s ^:likes ?x }"),
        ]
    ],
    *[
        (f"union_list_copies_{name}", UNION_LIST_COPIES, query, issue)
        for name, query, issue in [
            # Compared as whole lists, so both copies stay and "b" comes back
            # twice (#170, also on main).
            ("attribute", "SELECT ?s ?t WHERE { ?s a :Agent ; :tag ?t }", "#170"),
            ("optional", "SELECT ?s ?t WHERE { ?s a :Agent OPTIONAL { ?s :tag ?t } }", None),
            ("constant", 'SELECT ?s WHERE { ?s a :Agent OPTIONAL { ?s :tag ?t } FILTER(?t = "c") }', None),
        ]
    ],
    *[
        (f"union_edge_out_copies_{name}", UNION_EDGE_OUT_COPIES, query, None)
        for name, query in [
            # An edge out of one copy (``Person/a``): every copy stays when a
            # group walks it, and an EXISTS body (not a group's triple) reads
            # the edge by the row's IRI, so the copy kept does not hide it.
            ("out_edge", "SELECT ?s ?x WHERE { ?s a :Agent ; :knows ?x }"),
            ("exists_out", "SELECT ?s WHERE { ?s a :Agent FILTER EXISTS { ?s :knows ?x } }"),
            (
                "exists_out_attribute",
                "SELECT ?s ?n WHERE { ?s a :Agent ; :name ?n FILTER EXISTS { ?s :knows ?x } }",
            ),
        ]
    ],
    *[
        (f"union_graph_copies_{name}", UNION_GRAPH_COPIES, query, None)
        for name, query in [
            ("untyped_attribute", "SELECT ?s ?n WHERE { ?s :name ?n }"),
            ("supertype", "SELECT ?s WHERE { ?s a :Agent }"),
            ("one_graph", "SELECT ?s WHERE { GRAPH :g1 { ?s :name ?n } }"),
        ]
    ],
    *[
        (f"optional_scope_{name}", OPTIONAL_SCOPE, query, None)
        for name, query in [
            (
                "bind_outer",
                'SELECT ?p ?a ?v WHERE { ?p :name ?n OPTIONAL { ?p :age ?a BIND(CONCAT(?n, "!") AS ?v) } }',
            ),
            (
                "nested_filter_outer",
                'SELECT ?p ?a WHERE { ?p :name ?n OPTIONAL { ?p :age ?a { FILTER(?n = "A") } } }',
            ),
            (
                "subselect_outer",
                "SELECT ?p ?k WHERE { ?p :name ?n OPTIONAL { SELECT ?p ?k WHERE { ?p :nick ?k FILTER(?k != ?n) } } }",
            ),
            # Must hold: the left join's own condition, and a shared variable.
            (
                "top_filter_outer",
                'SELECT ?p ?a WHERE { ?p :name ?n OPTIONAL { ?p :age ?a FILTER(?n = "A") } }',
            ),
            (
                "top_filter_mixed",
                'SELECT ?p ?a WHERE { ?p :name ?n OPTIONAL { ?p :age ?a FILTER(?a > 2 && ?n = "B") } }',
            ),
            ("bind_shared", "SELECT ?p ?v WHERE { ?p :name ?n OPTIONAL { ?p :age ?a BIND(?a + 1 AS ?v) } }"),
            (
                "bind_shared_subject",
                "SELECT ?p ?v WHERE { ?p :name ?n OPTIONAL { ?p :age ?a BIND(STR(?p) AS ?v) } }",
            ),
            (
                "shared_object",
                "SELECT ?p ?q WHERE { ?p :nick ?n OPTIONAL { ?q :name ?n } }",
            ),
            # Review of #140: a VALUES column is bound by the body (rdflib's
            # ``_vars`` leaves it out); an EXISTS sees the outer row, in and
            # around an OPTIONAL body (substitution, §18.6).
            (
                "values_in_body",
                'SELECT ?p ?a WHERE { ?p :name ?n OPTIONAL { VALUES ?n { "A" } ?p :age ?a } }',
            ),
            (
                "values_object_in_body",
                "SELECT ?p ?k WHERE { ?p :age ?a0 OPTIONAL { VALUES ?a0 { 3 } ?p :nick ?k } }",
            ),
            (
                "optional_in_exists",
                "SELECT ?p WHERE { ?p :name ?n FILTER EXISTS { ?p :age ?a OPTIONAL { ?p :nick ?k } } }",
            ),
            (
                "optional_in_exists_bind",
                "SELECT ?p WHERE { ?p :name ?n FILTER EXISTS "
                "{ ?p :age ?a OPTIONAL { ?p :nick ?k BIND(?n AS ?z) } FILTER(BOUND(?z)) } }",
            ),
            (
                "exists_in_body",
                "SELECT ?p ?a WHERE { ?p :name ?n OPTIONAL { { ?p :age ?a FILTER EXISTS { ?q :nick ?n } } } }",
            ),
            (
                "and_error_false",
                'SELECT ?p WHERE { ?p :name ?n OPTIONAL { ?p :age ?a BIND((?n = "B" && ?a > 3) AS ?f) } '
                "FILTER(?f = false) }",
            ),
        ]
    ],
    ("undeclared_mixed_untyped", UNDECLARED_MIXED_DOMAIN, "SELECT ?s ?g WHERE { ?s :age ?g }", None),
    ("undeclared_mixed_constant", UNDECLARED_MIXED_DOMAIN, "SELECT ?g WHERE { :o :age ?g }", None),
    ("undeclared_mixed_default", UNDECLARED_MIXED_DOMAIN, "SELECT ?g WHERE { :z :age ?g }", None),
    *[
        (f"undeclared_domain_{name}", UNDECLARED_DOMAIN, query, None)
        for name, query in [
            ("untyped_subject", "SELECT ?s ?n WHERE { ?s :name ?n }"),
            ("constant_subject", "SELECT ?n WHERE { :a :name ?n }"),
            ("mixed_domain", "SELECT ?s ?g WHERE { ?s :age ?g }"),
            ("reverse_walk_constant", "SELECT ?x WHERE { ?x :knows :b }"),
            ("forward_walk", "SELECT ?y WHERE { :a :knows ?y }"),
            ("both_variable", "SELECT ?x ?y WHERE { ?x :knows ?y }"),
            ("reverse_walk_typed", "SELECT ?x WHERE { ?p a :Person . ?x :knows ?p }"),
        ]
    ],
    (
        "tenant_label_in_another_collection",
        TENANT_SHARED_LABEL,
        "SELECT ?t WHERE { ?p a :Person . ?p :owns ?t }",
        None,
    ),
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
    ("pg_attr_then_type", PG_TWO_CLASSES, "SELECT ?s WHERE { ?s :name ?n . ?s a :Employee }", None),
    # #120: the edge binds ?s to its Person copy; :salary is on the Employee
    # copy (its domain), and is read there.
    (
        "pg_edge_then_attr",
        PG_TWO_CLASSES,
        "SELECT ?s ?y WHERE { :a :knows ?s . ?s a :Employee ; :salary ?y }",
        None,
    ),
    ("pg_edge_attr_untyped", PG_TWO_CLASSES, "SELECT ?s ?y WHERE { :a :knows ?s . ?s :salary ?y }", None),
    (
        "pg_values_edge_attr",
        PG_TWO_CLASSES,
        "SELECT ?s ?y WHERE { VALUES ?x { :a } ?x :knows ?s . ?s a :Employee ; :salary ?y }",
        None,
    ),
    (
        "pg_subselect_edge_attr",
        PG_TWO_CLASSES,
        'SELECT ?s ?y WHERE { { SELECT ?x WHERE { ?x :name "A" } } ?x :knows ?s . ?s :salary ?y }',
        None,
    ),
    ("pg_type_then_other_attr", PG_TWO_CLASSES, "SELECT ?n WHERE { ?s a :Employee ; :name ?n }", None),
    ("pg_both_attrs", PG_TWO_CLASSES, "SELECT ?n ?y WHERE { ?s :name ?n ; :salary ?y }", None),
    (
        "pg_replica_edge_attr",
        PG_TWO_CLASSES_REPLICA,
        "SELECT ?s ?y WHERE { :a :knows ?s . ?s :salary ?y }",
        None,
    ),
    (
        "pg_replica_edge_count",
        PG_TWO_CLASSES_REPLICA,
        "SELECT (COUNT(*) AS ?c) WHERE { :a :knows ?s . ?s :salary ?y }",
        None,
    ),
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
    (
        "graph_outside_untyped_walk",
        GRAPH_OUTSIDE_STRICT,
        "SELECT ?m WHERE { ?p :name ?n . GRAPH :g { ?m :hasCreator ?p } }",
        None,
    ),
    (
        "graph_outside_typed",
        GRAPH_OUTSIDE,
        "SELECT ?m WHERE { ?p :name ?n . GRAPH :g { ?m a :Post . ?m :hasCreator ?p } }",
        None,
    ),
    (
        "graph_outside_typed_strict",
        GRAPH_OUTSIDE_STRICT,
        "SELECT ?m WHERE { ?p :name ?n . GRAPH :g { ?m a :Post . ?m :hasCreator ?p } }",
        None,
    ),
    (
        "graph_outside_constant_strict",
        GRAPH_OUTSIDE_STRICT,
        "SELECT ?m WHERE { :p1 :name ?n . GRAPH :g { ?m a :Post . ?m :hasCreator :p1 } }",
        None,
    ),
    (
        "graph_outside_optional",
        GRAPH_OUTSIDE_STRICT,
        "SELECT ?p ?m WHERE { ?p :name ?n OPTIONAL { GRAPH :g { ?m a :Post . ?m :hasCreator ?p } } }",
        None,
    ),
    (
        "graph_outside_exists",
        GRAPH_OUTSIDE_STRICT,
        "SELECT ?p WHERE { ?p :name ?n FILTER EXISTS { GRAPH :g { ?m a :Post . ?m :hasCreator ?p } } }",
        None,
    ),
    (
        "graph_inside_typed",
        GRAPH_OUTSIDE,
        "SELECT ?m WHERE { GRAPH :g { ?p a :Person . ?m a :Post . ?m :hasCreator ?p } }",
        None,
    ),
    (
        "scope_graph_only_no_domain_var",
        GRAPH_ONLY_NO_DOMAIN,
        "SELECT ?n WHERE { ?s a :Person . GRAPH :g { ?s :name ?n } }",
        None,
    ),
    (
        "scope_graph_only_no_domain_const",
        GRAPH_ONLY_NO_DOMAIN,
        "SELECT ?n WHERE { :s a :Person . GRAPH :g { :s :name ?n } }",
        None,
    ),
    *[
        (f"scope_edge_reached{suffix}_{name}", store, query, None)
        for suffix, store in (("", GRAPH_EDGE_REACHED), ("_empty_default", GRAPH_EDGE_REACHED_EMPTY_DEFAULT))
        for name, query in [
            ("plain", "SELECT ?x WHERE { ?s :knows ?t . GRAPH :g { ?t :name ?x } }"),
            ("optional", "SELECT ?t ?x WHERE { ?s :knows ?t . OPTIONAL { GRAPH :g { ?t :name ?x } } }"),
            ("union", "SELECT ?x WHERE { ?s :knows ?t . GRAPH :g { { ?t :name ?x } UNION { ?t :age ?x } } }"),
        ]
    ],
    # No range: nothing says where the vertex is, so it is re-read in the
    # default collection (here holding its copy; an empty one finds nothing).
    (
        "scope_edge_reached_no_range",
        GRAPH_EDGE_REACHED,
        "SELECT ?x WHERE { ?s :worksFor ?o . GRAPH :g { ?o :name ?x } }",
        None,
    ),
    *[
        (f"scope_{name}{suffix}", store, query, None)
        for suffix, store in (("", GRAPH_SCOPES), ("_no_domain", GRAPH_SCOPES_NO_DOMAIN))
        for name, query in [
            ("const_in_then_out", "SELECT ?n WHERE { GRAPH :g { :s a :Person } :s :name ?n }"),
            ("const_out_then_in", "SELECT ?n WHERE { :s a :Person . GRAPH :g { :s :name ?n } }"),
            ("const_both", "SELECT ?n ?m WHERE { :s :name ?n . GRAPH :g { :s :name ?m } }"),
            ("const_both_reversed", "SELECT ?n ?m WHERE { GRAPH :g { :s :name ?m } :s :name ?n }"),
            ("var_outer_then_in", "SELECT ?x WHERE { ?s :knows ?t . GRAPH :g { ?s :name ?x } }"),
            (
                "union_arms_in_graph",
                "SELECT ?x WHERE { ?s :knows ?t . GRAPH :g { { ?s :name ?x } UNION { ?s :age ?x } } }",
            ),
            (
                "typed_union_arms_in_graph",
                "SELECT ?x WHERE { ?s a :Person . ?s :knows ?t . GRAPH :g { { ?s :name ?x } UNION { ?s :age ?x } } }",
            ),
            ("optional_graph", "SELECT ?s ?x WHERE { ?s :knows ?t . OPTIONAL { GRAPH :g { ?s :name ?x } } }"),
        ]
    ],
    (
        "repeated_range_graph_variable",
        REPEATED_RANGE_GRAPHS,
        "SELECT ?g ?p WHERE { GRAPH ?g { ?p :likes :k1 } }",
        None,
    ),
    (
        "repeated_range_graph_variable_values",
        REPEATED_RANGE_GRAPHS,
        "SELECT ?g ?p ?m WHERE { VALUES ?m { :m1 :k1 } GRAPH ?g { ?p :likes ?m } }",
        None,
    ),
    (
        "repeated_range_graph_constant",
        REPEATED_RANGE_GRAPHS,
        "SELECT ?p WHERE { GRAPH :h { ?p :likes :k1 } }",
        None,
    ),
    *[
        (f"repeated_{name}", REPEATED_RANGE_DOMAIN, query, None)
        for name, query in [
            ("domain_untyped_subject", "SELECT ?s ?n WHERE { ?s :name ?n }"),
            ("domain_constant_subject", "SELECT ?n WHERE { :c :name ?n }"),
            ("domain_edge_subject", "SELECT ?p WHERE { :k1 :hasCreator ?p }"),
            ("domain_reverse_walk", "SELECT ?m WHERE { ?m :hasCreator :a }"),
            ("range_constant_object", "SELECT ?p WHERE { ?p :likes :k1 }"),
            ("range_values_object", "SELECT ?p ?m WHERE { VALUES ?m { :m1 :k1 } ?p :likes ?m }"),
            (
                "range_subselect_object",
                "SELECT ?p ?m WHERE { { SELECT ?m WHERE { ?m a :Comment } } ?p :likes ?m }",
            ),
        ]
    ],
    ("tenant_reverse", TENANTS, "SELECT ?x ?y WHERE { ?y a :Person . ?x :knows ?y }", None),
    ("tenant_reverse_const", TENANTS, "SELECT ?x WHERE { :y a :Person . ?x :knows :y }", None),
    (
        "no_default_collection_reverse",
        NO_DEFAULT_COLLECTION,
        "SELECT ?x ?y WHERE { ?y a :Person . ?x :knows ?y }",
        None,
    ),
    # A walk into the subject's class (the subject has one type pattern): the
    # walk applies that pattern's tests to the vertex it reaches.
    (
        "pg_class_walk_other_copy",
        PG_TWO_CLASSES,
        "SELECT ?x ?s WHERE { { ?s a :Employee } { ?x :knows ?s . ?x a :Person } }",
        None,
    ),
    (
        "range_elsewhere_class_walk",
        RANGE_ELSEWHERE,
        "SELECT ?x WHERE { { ?c a :Place } { ?x :livesIn ?c . ?x a :Person } }",
        None,
    ),
    (
        "ng_graph_class_walk",
        NG_EDGES,
        "SELECT ?x WHERE { GRAPH :g { { ?y a :Person } { ?x :knows ?y . ?x a :Person } } }",
        None,
    ),
    (
        "ng_strict_class_walk",
        NG_EDGES_STRICT,
        "SELECT ?x ?y WHERE { { ?y a :Person } { ?x :knows ?y . ?x a :Person } }",
        None,
    ),
    (
        "ng_strict_optional_class_walk",
        NG_EDGES_STRICT,
        "SELECT ?y ?x WHERE { ?y a :Person OPTIONAL { ?x :knows ?y . ?x a :Person } }",
        None,
    ),
    (
        "class_walk_keeps_the_class",
        CLASS_FILTER,
        "SELECT ?x WHERE { { ?y a :Person } { ?x :likes ?y . ?x a :Person } }",
        None,
    ),
    (
        "tenant_cross_edge_reverse",
        TENANTS_CROSS_EDGE,
        "SELECT ?x ?y WHERE { ?y a :Person . ?x :knows ?y }",
        None,  # #113, fixed with #118
    ),
    (
        "tenant_cross_edge_class_walk",
        TENANTS_CROSS_EDGE,
        "SELECT ?x ?y WHERE { { ?y a :Person } { ?x :knows ?y . ?x a :Person } }",
        None,
    ),
    (
        "tenant_class_walk",
        TENANTS,
        "SELECT ?x ?y WHERE { { ?y a :Person } { ?x :knows ?y . ?x a :Person } }",
        None,
    ),
    (
        "lpg_class_walk",
        LPG_LABELS,
        "SELECT ?x WHERE { { ?y a :Person } { ?x :knows ?y . ?x a :Employee } }",
        None,
    ),
    (
        "lpg_class_walk_const",
        LPG_LABELS,
        "SELECT ?x WHERE { :y a :Person . ?x :knows :y . ?x a :Person }",
        None,
    ),
    ("symmetric_self_loop_from", SYMMETRIC_SELF_LOOP, "SELECT ?f WHERE { :a :knows ?f }", None),
    # The OPTIONAL fast path and variable predicates read both directions (#133).
    (
        "symmetric_optional",
        SYMMETRIC_SELF_LOOP,
        "SELECT ?x ?y WHERE { ?x a :Person . OPTIONAL { ?x :knows ?y } }",
        None,
    ),
    (
        "symmetric_optional_two",
        SYMMETRIC_SELF_LOOP,
        "SELECT ?x ?y ?z WHERE { ?x a :Person . OPTIONAL { ?x :knows ?y } OPTIONAL { ?x :knows ?z } }",
        None,
    ),
    (
        "symmetric_variable_predicate",
        SYMMETRIC_SELF_LOOP,
        "SELECT ?x ?p ?y WHERE { ?x a :Person . ?x ?p ?y FILTER(?p = :knows) }",
        None,
    ),
    ("symmetric_self_loop_to", SYMMETRIC_SELF_LOOP, "SELECT ?f WHERE { ?f :knows :a }", None),
    ("symmetric_self_loop_both", SYMMETRIC_SELF_LOOP, "SELECT ?x ?y WHERE { ?x :knows ?y }", None),
    (
        "symmetric_self_loop_count",
        SYMMETRIC_SELF_LOOP,
        "SELECT ?x (COUNT(?y) AS ?n) WHERE { ?x a :Person . ?x :knows ?y } GROUP BY ?x",
        None,
    ),
    (
        "symmetric_self_loop_path",
        SYMMETRIC_SELF_LOOP,
        "SELECT ?f WHERE { :b :knows/:knows ?f }",
        None,
    ),
    # Tenant scoping of walked documents (issue #118); every one leaked T2.
    *[
        (f"tenant_{name}", TENANT_WALKS, query, None)
        for name, query in [
            ("forward_vertex", "SELECT ?b ?n WHERE { ?a a :Person . ?a :knows ?b . ?b :name ?n }"),
            ("forward_vertex_only", "SELECT ?a ?b WHERE { ?a a :Person . ?a :knows ?b }"),
            ("reverse_untyped", "SELECT ?x WHERE { ?y a :Person . ?x :knows ?y }"),
            ("reverse_constant", "SELECT ?x WHERE { ?x :knows :c }"),
            ("supertype", "SELECT ?m WHERE { ?m a :Message }"),
            ("reverse_domain_union", "SELECT ?m WHERE { ?p a :Person . ?m :hasCreator ?p }"),
            ("reified_exit", "SELECT ?f ?p WHERE { ?f a :Forum . ?f :hasMember ?k . ?k :hasPerson ?p }"),
            ("reified_exit_untyped", "SELECT ?f ?k WHERE { ?f :hasMember ?k . ?k :hasPerson ?p }"),
            (
                "reified_backwards",
                "SELECT ?f ?k WHERE { ?p a :Person . ?f :hasMember ?k . ?k :hasPerson ?p }",
            ),
            (
                "reified_backwards_values",
                "SELECT ?f ?k WHERE { VALUES ?p { :a :b } ?f :hasMember ?k . ?k :hasPerson ?p }",
            ),
            ("relationship_node_alone", "SELECT ?d WHERE { VALUES ?k { :hm1 :hm2 } ?k :joinDate ?d }"),
            (
                "optional_walk",
                "SELECT ?a ?n WHERE { ?a a :Person . OPTIONAL { ?a :knows ?b . ?b :name ?n } }",
            ),
            (
                "union_arm_walk",
                "SELECT ?a ?x WHERE { ?a a :Person . { ?a :knows ?x } UNION { ?x :knows ?a } }",
            ),
            ("path", "SELECT ?x WHERE { :a :knows+ ?x }"),
            ("typed_path", "SELECT ?x WHERE { ?a a :Person . ?a :knows+ ?x }"),
        ]
    ],
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
#: sub-query, a sub-SELECT, ``DOCUMENT()``, ``ATTRIBUTES()``); ``tests/integration/test_multi_document_iri_live.py``
#: runs them.
_LIVE_ONLY = {
    "union_copies_subselect",
    "union_copies_optional_attribute",
    "union_copies_optional_attribute_elsewhere",
    "union_copies_optional_constant",
    "union_copies_optional_filter",
    "union_copies_optional_count",
    "union_copies_optional_after_union",
    "union_list_copies_optional",
    "union_list_copies_constant",
    "ng_strict_optional",
    "ng_strict_optional_class_walk",
    "graph_outside_optional",
    "repeated_range_subselect_object",
    "tenant_relationship_node_alone",
    "pg_subselect_edge_attr",
    "optional_scope_bind_outer",
    "optional_scope_subselect_outer",
    "optional_scope_optional_in_exists_bind",
    "optional_scope_and_error_false",
    "symmetric_variable_predicate",
}


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
    rows = oxi_bindings(
        oracle, "PREFIX : <http://ex.org/>\n" + query, union_default_graph=store.union_default_graph
    )
    return [normalize_oxi_row(r) for r in rows]


@pytest.mark.parametrize("store, query", case_params(interpreter=True))
def test_the_translation_agrees_with_pyoxigraph(store: Store, query: str) -> None:
    result = translate_case(store, query)
    rows = [drop_null_bindings(r) for r in run_aql_subset(result.aql, result.bind_vars, store.docs)]
    assert_bindings_equal(oracle_rows(store, query), rows)


@pytest.mark.parametrize(
    "store, query", [pytest.param(st, q, id=n) for n, st, q, _i in _CASES if "class_walk" in n]
)
def test_the_class_walk_cases_walk_into_the_class(store: Store, query: str) -> None:
    # Guard: these cases check the walk that applies a type pattern's tests
    # to the vertex it reaches; if the join order stopped choosing it they
    # would pass on the forward plan and check nothing.
    aql = translate_case(store, query).aql
    assert " IN INBOUND " in aql or " IN ANY " in aql
