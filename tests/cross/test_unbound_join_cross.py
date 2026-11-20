"""Cross-validation: an unbound variable joins any value (SPARQL 1.1 §18.3).

A variable an earlier OPTIONAL can leave unbound is *compatible* with every
value a later pattern gives it: the row joins, and the variable takes that
value. #190 (PR #192) fixed this for triples-table (RPT) subjects; #193 found
it everywhere else, where the join compared the stored identity with null,
matched nothing, and dropped the row silently:

* a **document subject** (PG collection, LPG label, a supertype read from
  several collections);
* an **object** — an edge's target vertex, a document attribute (single- and
  multi-valued), a triples-table object column;
* **EXISTS** with two such variables.

Each mapping stores the same facts (:data:`FACTS`); the translated AQL runs
through the in-memory interpreter and must give pyoxigraph's bindings. Live
ArangoDB runs of the same shapes are in
``tests/integration/test_unbound_join_live.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
import rdflib

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.aql_interp import run_aql_subset
from tests.helpers.oxi import (
    assert_bindings_equal,
    drop_null_bindings,
    load_store_from_string,
    normalize_oxi_row,
    oxi_bindings,
)

oxi = pytest.importorskip("pyoxigraph", reason="pyoxigraph required for cross tests")

EX = "http://ex.org/"

# :a knows :b and :c, :d knows :e; :b, :c, :e and :f know nobody, so their
# ``OPTIONAL { ?s :knows ?x }`` leaves ?x unbound. :b and :e have an email.
PEOPLE: dict[str, dict[str, Any]] = {
    "a": {"name": "A", "knows": ["b", "c"], "tags": ["x", "y"]},
    "b": {"name": "B", "email": "b@x.org", "tags": ["y"]},
    "c": {"name": "C"},
    "d": {"name": "D", "knows": ["e"]},
    "e": {"name": "E", "email": "e@x.org", "tags": ["z"]},
    "f": {},
}
#: The supertype mapping splits the people into two classes.
HUMANS = ("a", "b", "c")


def _facts(*, types: bool = True, knows: bool = True, tags: bool = False, agent: bool = False) -> str:
    lines = [f"@prefix : <{EX}> ."]
    for key, props in PEOPLE.items():
        if types:
            lines.append(f":{key} a :Person .")
        if agent:
            lines.append(f":{key} a :{'Human' if key in HUMANS else 'Bot'} , :Agent .")
        if "name" in props:
            lines.append(f':{key} :name "{props["name"]}" .')
        if "email" in props:
            lines.append(f':{key} :email "{props["email"]}" .')
        if knows:
            lines.extend(f":{key} :knows :{other} ." for other in props.get("knows", []))
        if tags:
            lines.extend(f':{key} :tag "{tag}" .' for tag in props.get("tags", []))
    return "\n".join(lines)


_HEAD = f"""
@prefix : <{EX}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix phys: <https://arango.solutions/phys#> .
:name a owl:DatatypeProperty ; rdfs:domain :Person ; phys:attributeName "name" .
:email a owl:DatatypeProperty ; rdfs:domain :Person ; phys:attributeName "email" .
"""


def _person(key: str, **extra: Any) -> dict[str, Any]:
    props = PEOPLE[key]
    doc = {"_uri": EX + key, **{p: props[p] for p in ("name", "email") if p in props}}
    return {**doc, **extra}


def _pg_edge() -> tuple[str, dict[str, list[dict[str, Any]]]]:
    ttl = (
        _HEAD
        + """
:Person a owl:Class ; phys:collectionName "Person" .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; rdfs:domain :Person ; rdfs:range :Person .
"""
    )
    persons = [_person(k, _id=f"Person/{k}") for k in PEOPLE]
    edges = [
        {"_from": f"Person/{k}", "_to": f"Person/{t}"} for k, d in PEOPLE.items() for t in d.get("knows", [])
    ]
    return ttl, {"Person": persons, "knows": edges}


def _lpg() -> tuple[str, dict[str, list[dict[str, Any]]]]:
    ttl = (
        _HEAD
        + """
:Person a owl:Class ; phys:collectionName "vertices" ; phys:mappingStyle "LABEL" ;
    phys:typeField "type" ; phys:typeValue "Person" .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "edges" ; phys:mappingStyle "GENERIC_WITH_TYPE" ;
    phys:typeField "type" ; phys:typeValue "knows" ; rdfs:domain :Person ; rdfs:range :Person .
"""
    )
    vertices = [_person(k, _id=f"vertices/{k}", type="Person") for k in PEOPLE]
    edges = [
        {"_from": f"vertices/{k}", "_to": f"vertices/{t}", "type": "knows"}
        for k, d in PEOPLE.items()
        for t in d.get("knows", [])
    ]
    return ttl, {"vertices": vertices, "edges": edges}


def _rpt() -> tuple[str, dict[str, list[dict[str, Any]]]]:
    ttl = (
        _HEAD
        + """
:Person a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "_triples" .
"""
    )
    rows: list[dict[str, Any]] = []
    for s, p, o in rdflib.Graph().parse(data=_facts(), format="turtle"):
        row: dict[str, Any] = {"subject_uri": str(s), "predicate": str(p)}
        if isinstance(o, rdflib.URIRef):
            row["object_uri"] = str(o)
        else:
            row["object_value"] = o.toPython()
        rows.append(row)
    return ttl, {"_triples": rows}


def _pg_attr() -> tuple[str, dict[str, list[dict[str, Any]]]]:
    """No edges: the attribute and multi-valued attribute object paths."""
    ttl = (
        _HEAD
        + """
:Person a owl:Class ; phys:collectionName "Person" .
:tag a owl:DatatypeProperty ; rdfs:domain :Person ; phys:attributeName "tags" ; phys:multiValued "true" .
"""
    )
    persons = [
        _person(k, _id=f"Person/{k}", **({"tags": PEOPLE[k]["tags"]} if "tags" in PEOPLE[k] else {}))
        for k in PEOPLE
    ]
    return ttl, {"Person": persons}


def _pg_union() -> tuple[str, dict[str, list[dict[str, Any]]]]:
    """``:Agent`` is a supertype read from two collections (one UNION FOR)."""
    ttl = (
        _HEAD.replace("rdfs:domain :Person", "rdfs:domain :Agent")
        + """
:Human a owl:Class ; phys:collectionName "Human" .
:Bot a owl:Class ; phys:collectionName "Bot" .
:Agent a owl:Class ; phys:mappingStyle "UNION" .
:Human rdfs:subClassOf :Agent .
:Bot rdfs:subClassOf :Agent .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; rdfs:domain :Agent ; rdfs:range :Agent .
"""
    )
    home = {k: ("Human" if k in HUMANS else "Bot") for k in PEOPLE}
    docs: dict[str, list[dict[str, Any]]] = {"Human": [], "Bot": []}
    for k in PEOPLE:
        docs[home[k]].append(_person(k, _id=f"{home[k]}/{k}"))
    docs["knows"] = [
        {"_from": f"{home[k]}/{k}", "_to": f"{home[t]}/{t}"}
        for k, d in PEOPLE.items()
        for t in d.get("knows", [])
    ]
    return ttl, docs


#: Shapes from #193 and the #192 review harness, wrong on every document
#: mapping before the fix (rows dropped where ?x was unbound).
SUBJECT_CASES = [
    pytest.param(
        "SELECT ?s ?x ?m WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } OPTIONAL { ?x :email ?m } }",
        id="chained_optional",
    ),
    pytest.param(
        "SELECT ?s ?x ?m ?n WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } OPTIONAL { ?x :email ?m . ?x :name ?n } }",
        id="chained_optional_two_triples",
    ),
    pytest.param(
        "SELECT ?s ?x ?m WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } ?x :email ?m }", id="required_join"
    ),
    pytest.param(
        "SELECT ?s ?x WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } ?x a :Person }", id="required_type"
    ),
    pytest.param(
        "SELECT ?s ?x WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } FILTER EXISTS { ?x :email ?m } }",
        id="exists",
    ),
    pytest.param(
        "SELECT ?s ?x WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } FILTER NOT EXISTS { ?x :email ?m } }",
        id="not_exists",
    ),
    pytest.param(
        "SELECT ?s ?x ?m ?n WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } { ?x :email ?m } UNION { ?x :name ?n } }",
        id="union_after",
    ),
    pytest.param(
        "SELECT ?s (COUNT(?m) AS ?c) WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } OPTIONAL { ?x :email ?m } } GROUP BY ?s",
        id="count",
    ),
    pytest.param(
        "SELECT ?s ?x ?m WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } OPTIONAL { ?x :email ?m } FILTER(BOUND(?x)) }",
        id="bound_after",
    ),
    pytest.param(
        "SELECT ?s ?x ?y WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } OPTIONAL { ?x :knows ?y } }",
        id="chained_edge",
    ),
    pytest.param(
        "SELECT DISTINCT ?x WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } OPTIONAL { ?x :email ?m } } ORDER BY ?x",
        id="distinct",
    ),
    pytest.param(
        "SELECT ?s ?x ?m WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } OPTIONAL { ?x :email ?m } MINUS { ?s :knows ?x } }",
        id="minus_after_chain",
    ),
]

#: The variable is unbound in *object* position (#193 §2), on edges too.
OBJECT_CASES = [
    pytest.param(
        "SELECT ?s ?x ?y WHERE { ?s a :Person OPTIONAL { ?s :email ?x } OPTIONAL { ?y :knows ?x } }",
        id="object_optional",
    ),
    pytest.param(
        "SELECT ?s ?x ?y WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } ?y :knows ?x }", id="object_required"
    ),
    pytest.param(
        "SELECT ?s ?x ?y WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } OPTIONAL { ?s :email ?y } OPTIONAL { ?x :knows ?y } }",
        id="object_edge_target",
    ),
    # Two possibly-unbound variables in one EXISTS (#193 §3): 2 of 6 rows before.
    pytest.param(
        "SELECT ?s ?x ?y WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } OPTIONAL { ?s :email ?y } FILTER EXISTS { ?x :email ?y } }",
        id="exists_two_unbound",
    ),
]

#: Document attributes as the object: single-valued and multi-valued (the
#: unbound side reads every element; the bound side keeps one row).
ATTRIBUTE_CASES = [
    pytest.param(
        "SELECT ?s ?x ?y WHERE { ?s a :Person OPTIONAL { ?s :email ?x } ?y :email ?x }", id="attribute_object"
    ),
    pytest.param(
        "SELECT ?s ?t ?y WHERE { ?s a :Person OPTIONAL { ?s :email ?t } OPTIONAL { ?y :tag ?t } }",
        id="multi_valued_object_none_match",
    ),
    pytest.param(
        'SELECT ?s ?t ?y WHERE { ?s a :Person OPTIONAL { ?s :tag ?t FILTER(?t = "y") } ?y :tag ?t }',
        id="multi_valued_object",
    ),
    pytest.param(
        "SELECT ?s ?x ?m WHERE { VALUES (?s ?x) { (:a UNDEF) (:c :b) } ?s :name ?n OPTIONAL { ?x :email ?m } }",
        id="values_undef_subject",
    ),
]

#: A supertype subject (one FOR over a UNION of collections).
SUPERTYPE_CASES = [
    pytest.param(
        "SELECT ?s ?x WHERE { ?s a :Human OPTIONAL { ?s :knows ?x } ?x a :Agent }",
        id="supertype_required_type",
    ),
]
# ``OPTIONAL { ?x a :Agent ; :name ?n }`` on this mapping runs live only
# (``tests/integration/test_unbound_join_live.py``): its copy check inside a
# union sub-query is beyond the interpreter.


def _check(ttl: str, docs: dict[str, list[dict[str, Any]]], facts: str, body: str) -> None:
    query = f"PREFIX : <{EX}>\n" + body
    expected = [normalize_oxi_row(r) for r in oxi_bindings(load_store_from_string(facts), query)]
    assert expected, "the case must have a non-empty answer to prove anything"
    result = translate(query, resolver=SchemaResolver.from_turtle(ttl))
    actual = [drop_null_bindings(r) for r in run_aql_subset(result.aql, result.bind_vars, docs)]
    assert_bindings_equal(expected, actual)


EDGE_MAPPINGS = [pytest.param(_pg_edge, id="pg_edge"), pytest.param(_lpg, id="lpg")]


@pytest.mark.cross
@pytest.mark.parametrize("mapping", EDGE_MAPPINGS)
@pytest.mark.parametrize("body", SUBJECT_CASES + OBJECT_CASES)
def test_unbound_variable_joins_any_document(mapping: Any, body: str) -> None:
    ttl, docs = mapping()
    _check(ttl, docs, _facts(), body)


@pytest.mark.cross
@pytest.mark.parametrize("body", OBJECT_CASES)
def test_unbound_variable_joins_any_triples_object(body: str) -> None:
    ttl, docs = _rpt()
    _check(ttl, docs, _facts(), body)


@pytest.mark.cross
@pytest.mark.parametrize("body", ATTRIBUTE_CASES)
def test_unbound_variable_joins_any_attribute_value(body: str) -> None:
    ttl, docs = _pg_attr()
    _check(ttl, docs, _facts(knows=False, tags=True), body)


@pytest.mark.cross
@pytest.mark.parametrize("body", SUPERTYPE_CASES)
def test_unbound_variable_joins_any_supertype_member(body: str) -> None:
    ttl, docs = _pg_union()
    _check(ttl, docs, _facts(types=False, agent=True), body)
