"""EXISTS / MINUS on a triples-table subject when the mapping also has documents.

The MINUS / EXISTS probe is translated by a child visitor seeded with the
outer bindings. It did not carry which outer variables are triples-table
(RPT) subjects, so a probe triple on one was routed as a document. On a
mapping that is one triple store that happened to reach the table anyway;
on a mapping that also has PG classes it opened the default ``Document``
collection, and ArangoDB refused the query (ERR 1203; #190). The probe now
carries that state, as ``_spawn_child`` does for UNION and OPTIONAL.

Live because the bug is about what ArangoDB runs. Every query also runs
through pyoxigraph on the same facts, the W3C ground truth, and the answers
must agree. Gated like the rest of the integration suite
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
_TABLE = "ProbeHybridTriples"
_COMPANIES = "ProbeHybridCompanies"
_TTL = f"""
@prefix :     <{EX}> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix phys: <https://arango.solutions/phys#> .
:Person a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "{_TABLE}" .
:Company a owl:Class ; phys:mappingStyle "COLLECTION" ; phys:collectionName "{_COMPANIES}" .
:cname a owl:DatatypeProperty ; rdfs:domain :Company ; phys:attributeName "cname" .
"""
# People live in the triples table; companies are documents.
_TRIPLES = f"""
@prefix : <{EX}> .
:a a :Person ; :knows :b , :c ; :worksFor :acme .
:d a :Person ; :knows :e ; :worksFor :beta .
:b a :Person ; :email "b@x.org" .
:c a :Person .
:e a :Person ; :email "e@x.org" .
"""
_DOCS = [
    {"_key": "acme", "_uri": EX + "acme", "cname": "Acme"},
    {"_key": "beta", "_uri": EX + "beta"},
]
_PREFIX = f"PREFIX : <{EX}>\n"


def _rows() -> list[dict[str, Any]]:
    """One triples-table row per RDF triple: IRIs in ``object_uri``, literals in ``object_value``."""
    rows = []
    for s, p, o in rdflib.Graph().parse(data=_TRIPLES, format="turtle"):
        row: dict[str, Any] = {"subject_uri": str(s), "predicate": str(p)}
        if isinstance(o, rdflib.URIRef):
            row["object_uri"] = str(o)
        else:
            row["object_value"] = o.toPython()
        rows.append(row)
    return rows


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
    for name, docs in ((_TABLE, _rows()), (_COMPANIES, _DOCS)):
        if handle.has_collection(name):
            handle.delete_collection(name)
        handle.create_collection(name).insert_many(docs)
    yield handle
    handle.delete_collection(_TABLE)
    handle.delete_collection(_COMPANIES)


@pytest.fixture(scope="module")
def oxi_store() -> Any:
    facts = _TRIPLES + "".join(
        f"<{d['_uri']}> a <{EX}Company>" + (f' ; <{EX}cname> "{d["cname"]}"' if "cname" in d else "") + " .\n"
        for d in _DOCS
    )
    store = oxi.Store()
    store.load(facts.encode("utf-8"), oxi.RdfFormat.TURTLE)
    return store


def _multiset(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(json.dumps(r, sort_keys=True) for r in rows)


def _arango(db: Any, body: str) -> list[dict[str, Any]]:
    res = translate(_PREFIX + body, resolver=SchemaResolver.from_turtle(_TTL))
    db.aql.validate(res.aql)
    return [drop_null_bindings(r) for r in db.aql.execute(res.aql, bind_vars=res.bind_vars)]


def _oxigraph(store: Any, body: str) -> list[dict[str, Any]]:
    return [normalize_oxi_row(r) for r in oxi_bindings(store, _PREFIX + body)]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "SELECT ?s WHERE { ?s a :Person FILTER EXISTS { ?s :knows ?x } }",
            [{"s": EX + "a"}, {"s": EX + "d"}],
        ),
        (
            "SELECT ?s WHERE { ?s a :Person FILTER NOT EXISTS { ?s :email ?m } }",
            [{"s": EX + "a"}, {"s": EX + "c"}, {"s": EX + "d"}],
        ),
        (
            "SELECT ?s WHERE { ?s a :Person MINUS { ?s :knows ?x } }",
            [{"s": EX + "b"}, {"s": EX + "c"}, {"s": EX + "e"}],
        ),
        # A probe that leaves the triples table for a document still routes by
        # the property's class (it worked before; kept as a control).
        ("SELECT ?s WHERE { ?s a :Person ; :worksFor ?c FILTER EXISTS { ?c :cname ?n } }", [{"s": EX + "a"}]),
    ],
)
def test_a_probe_on_a_triples_table_subject_in_a_mixed_mapping_matches_pyoxigraph(
    db: Any, oxi_store: Any, body: str, expected: list[dict[str, Any]]
) -> None:
    arango = _arango(db, body)

    assert _multiset(arango) == _multiset(_oxigraph(oxi_store, body))
    assert _multiset(arango) == _multiset(expected)
