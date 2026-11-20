"""A group with a nested OPTIONAL is evaluated on its own, on ArangoDB (#194).

In ``?s :knows ?x OPTIONAL { ?s :name ?n OPTIONAL { ?x :email ?m } }`` the
second OPTIONAL's group binds ``?x`` only through its nested OPTIONAL. SPARQL
1.1 §18.5 evaluates that group without the outer ``?x`` and then keeps only
its solutions compatible with it (§18.3). The translator read the nested
OPTIONAL against the outer ``?x``, which bound ``?n`` for a person whose
contact has no email: a silent wrong answer on every mapping.

Live because the fix is a FILTER placed after a padded subquery, whose
result depends on how ArangoDB evaluates it. Each query runs on a triples
table and on PG documents with an edge collection, and must match pyoxigraph
on the same facts. Gated like the rest of the integration suite
(``RUN_INTEGRATION=1``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
import rdflib

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.oxi import drop_null_bindings, normalize_oxi_row, oxi_bindings
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

oxi = pytest.importorskip("pyoxigraph", reason="pyoxigraph is the ground truth for these tests")

EX = "http://example.org/"
_TRIPLES = "NestedOptTriples"
_PEOPLE = "NestedOptPerson"
_KNOWS = "NestedOptKnows"

# a knows b and c, d knows e; only b and e have an email.
_PERSONS: dict[str, dict[str, Any]] = {
    "a": {"name": "A", "knows": ["b", "c"]},
    "b": {"name": "B", "email": "b@x.org"},
    "c": {"name": "C"},
    "d": {"name": "D", "knows": ["e"]},
    "e": {"name": "E", "email": "e@x.org"},
}
_DATA = f"@prefix : <{EX}> .\n" + "\n".join(
    " ; ".join(
        [f":{k} a :Person"]
        + [f':{p} "{v}"' for p, v in d.items() if p != "knows"]
        + [f":knows :{t}" for t in d.get("knows", [])]
    )
    + " ."
    for k, d in _PERSONS.items()
)
_HEAD = f"""
@prefix :     <{EX}> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix phys: <https://arango.solutions/phys#> .
"""
_ONTOLOGIES = {
    "rpt": _HEAD + f':Person a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "{_TRIPLES}" .\n',
    "pg_edge": _HEAD
    + f"""
:Person a owl:Class ; phys:collectionName "{_PEOPLE}" .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "{_KNOWS}" ; rdfs:domain :Person ; rdfs:range :Person .
:name a owl:DatatypeProperty ; rdfs:domain :Person ; phys:attributeName "name" .
:email a owl:DatatypeProperty ; rdfs:domain :Person ; phys:attributeName "email" .
""",
}
_PREFIX = f"PREFIX : <{EX}>\n"


def _triple_rows() -> list[dict[str, Any]]:
    rows = []
    for s, p, o in rdflib.Graph().parse(data=_DATA, format="turtle"):
        row: dict[str, Any] = {"subject_uri": str(s), "predicate": str(p)}
        if isinstance(o, rdflib.URIRef):
            row["object_uri"] = str(o)
        else:
            row["object_value"] = o.toPython()
        rows.append(row)
    return rows


def _collections() -> dict[str, tuple[list[dict[str, Any]], bool]]:
    people = [
        {"_key": k, "_uri": EX + k, **{p: v for p, v in d.items() if p != "knows"}}
        for k, d in _PERSONS.items()
    ]
    knows = [
        {"_from": f"{_PEOPLE}/{k}", "_to": f"{_PEOPLE}/{t}"}
        for k, d in _PERSONS.items()
        for t in d.get("knows", [])
    ]
    return {_TRIPLES: (_triple_rows(), False), _PEOPLE: (people, False), _KNOWS: (knows, True)}


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
    for name, (docs, is_edge) in _collections().items():
        if handle.has_collection(name):
            handle.delete_collection(name)
        handle.create_collection(name, edge=is_edge).insert_many(docs)
    yield handle
    for name in _collections():
        handle.delete_collection(name)


@pytest.fixture(scope="module")
def oxi_store() -> Any:
    store = oxi.Store()
    store.load(_DATA.encode("utf-8"), oxi.RdfFormat.TURTLE)
    return store


def _multiset(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(json.dumps(r, sort_keys=True) for r in rows)


@pytest.mark.parametrize("mapping", sorted(_ONTOLOGIES))
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            "SELECT ?s ?x ?m ?n WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } "
            "OPTIONAL { ?s :name ?n OPTIONAL { ?x :email ?m } } }",
            id="issue_194",
        ),
        pytest.param(
            "SELECT ?s ?x ?n ?m WHERE { ?s a :Person ; :knows ?x OPTIONAL { ?s :name ?n OPTIONAL { ?x :email ?m } } }",
            id="required_x",
        ),
        pytest.param(
            "SELECT ?s ?x ?n ?m WHERE { ?s a :Person ; :knows ?x { ?s :name ?n OPTIONAL { ?x :email ?m } } }",
            id="required_group",
        ),
        pytest.param(
            "SELECT ?s ?x WHERE { ?s a :Person ; :knows ?x MINUS { ?s :name ?n OPTIONAL { ?x :email ?m } } }",
            id="minus",
        ),
        # The right side's OPTIONAL re-binds the outer ?o: it binds its own and
        # compares it (on a triples table it used to be refused).
        pytest.param(
            "SELECT ?s ?o WHERE { ?s a :Person ; :knows ?o MINUS { ?t a :Person OPTIONAL { ?t :knows ?o } } }",
            id="minus_rebind",
        ),
    ],
)
def test_nested_optional_group_scope_matches_oxigraph(
    db: Any, oxi_store: Any, mapping: str, body: str
) -> None:
    res = translate(_PREFIX + body, resolver=SchemaResolver.from_turtle(_ONTOLOGIES[mapping]))
    db.aql.validate(res.aql)
    actual = [drop_null_bindings(r) for r in db.aql.execute(res.aql, bind_vars=res.bind_vars)]
    expected = [normalize_oxi_row(r) for r in oxi_bindings(oxi_store, _PREFIX + body)]
    assert _multiset(actual) == _multiset(expected)


def test_issue_194_keeps_the_name_unbound_when_no_contact_is_compatible(db: Any) -> None:
    """The issue's row by hand: a's contact c has no email, so a's (x = c) row gets no ?n."""
    body = (
        "SELECT ?s ?x ?m ?n WHERE { ?s a :Person ; :knows ?x "
        "OPTIONAL { ?s :name ?n OPTIONAL { ?x :email ?m } } FILTER(?x = :c) }"
    )
    for mapping in sorted(_ONTOLOGIES):
        res = translate(_PREFIX + body, resolver=SchemaResolver.from_turtle(_ONTOLOGIES[mapping]))
        rows = [drop_null_bindings(r) for r in db.aql.execute(res.aql, bind_vars=res.bind_vars)]
        assert rows == [{"s": EX + "a", "x": EX + "c"}], mapping
