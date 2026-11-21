"""Cross-validation: a subject the query never placed, in a mapping that
keeps some classes as RDF triples and others as documents (#191).

People are rows of a triples table; companies are documents. A company is
reached only as a value (``?s :worksFor ?c``), so a triple on it can be in
either store. The translator used to guess: the cross-subject OPTIONAL read
the triples table and answered ``OPTIONAL { ?c :cname ?n }`` with nothing,
and the BGP read the default ``Document`` collection (ERR 1203, or a
refusal under strict subject resolution). It now reads the store the
predicate's ``rdfs:domain`` names, or every store when the mapping cannot
say (:mod:`arango_sparql.translate.mixed_store`).

``:acme`` has facts in both stores: its ``cname`` is a document attribute,
its ``email`` a triples-table row. Every query runs through the in-memory
AQL interpreter on the same facts pyoxigraph (the W3C ground truth) loads,
on the library's resolver and on the service's strict one, and with and
without declared domains. The same cases run on ArangoDB in
``tests/integration/test_mixed_store_live.py``.

The library's non-strict resolver also reads its default collection, where
the non-strict contract keeps an IRI no class places: ``:zeta`` is such an
untyped document (in ``D191``, the default collection these tests give the
resolver, so they never touch a shared ``Document``). Under strict subject
resolution it is not in the mapping, so the expected answers leave it out.
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
PREFIX = f"PREFIX : <{EX}>\nPREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"

# Facts the triples table holds: people, and one fact about a company.
TRIPLES = f"""
@prefix : <{EX}> .
:a a :Person ; :name "A" ; :knows :b , :c ; :worksFor :acme .
:b a :Person ; :name "B" ; :email "b@x.org" ; :worksFor :globex .
:c a :Person ; :name "C" ; :worksFor :initech .
:d a :Person ; :name "D" ; :knows :e .
:e a :Person ; :name "E" ; :email "e@x.org" .
:acme :email "info@acme.example" .
"""
# Facts the documents hold. A document's class membership is not a triple a
# variable predicate reads on any document mapping (PRD §6.6), so the
# variable-predicate cases leave rdf:type out.
DOCS = [
    {"_key": "acme", "_uri": EX + "acme", "cname": "Acme"},
    {"_key": "globex", "_uri": EX + "globex", "cname": "Globex"},
]
DOC_FACTS = f"""
@prefix : <{EX}> .
:acme a :Company ; :cname "Acme" .
:globex a :Company ; :cname "Globex" .
"""

# The non-strict resolver's default collection, and an untyped document in
# it (review of #199: without it a read of every store dropped such data).
DEFAULT_COLLECTION = "D191"
UNTYPED_DOCS = [{"_key": "zeta", "_uri": EX + "zeta", "label": "Zeta", "phone": "555-0100"}]
UNTYPED_FACTS = f"""
@prefix : <{EX}> .
:zeta :label "Zeta" ; :phone "555-0100" .
"""

_HEAD = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix phys: <https://arango.solutions/phys#> .
:Person a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "T191" .
:Company a owl:Class ; phys:mappingStyle "COLLECTION" ; phys:collectionName "C191" .
:cname a owl:DatatypeProperty ; rdfs:domain :Company ; phys:attributeName "cname" .
"""
ONTOLOGIES = {
    # The issue's mapping: only :cname is declared.
    "undeclared": _HEAD,
    # Declared without a domain: every store is read, and a document's
    # attributes are triples a variable predicate reads.
    "no_domain": _HEAD
    + """
:email a owl:DatatypeProperty .
:label a owl:DatatypeProperty .
:phone a owl:DatatypeProperty .
""",
    # Declared domains decide the store without reading both.
    "declared": _HEAD
    + """
:worksFor a owl:ObjectProperty ; rdfs:domain :Person ; rdfs:range :Company .
:knows a owl:ObjectProperty ; rdfs:domain :Person ; rdfs:range :Person .
:email a owl:DatatypeProperty ; rdfs:domain :Person .
:name a owl:DatatypeProperty ; rdfs:domain :Person .
""",
}

QUERIES = [
    # #191's silent wrong answer: an OPTIONAL into a document.
    pytest.param(
        "SELECT ?s ?c ?n WHERE { ?s a :Person ; :worksFor ?c OPTIONAL { ?c :cname ?n } }", id="opt_cname"
    ),
    pytest.param(
        "SELECT ?s ?c ?n ?m WHERE { ?s a :Person ; :worksFor ?c OPTIONAL { ?c :cname ?n . ?c :email ?m } }",
        id="opt_two_stores",
    ),
    pytest.param(
        "SELECT ?s (COUNT(?n) AS ?k) WHERE { ?s a :Person ; :worksFor ?c OPTIONAL { ?c :cname ?n } } GROUP BY ?s",
        id="count_opt_cname",
    ),
    pytest.param(
        "SELECT ?s ?c ?p ?v WHERE { ?s a :Person ; :worksFor ?c OPTIONAL { ?c ?p ?v FILTER(?p != rdf:type) } }",
        id="opt_variable_predicate",
    ),
    # #191's loud failures (ERR 1203 / a strict refusal): probes and the BGP.
    pytest.param(
        "SELECT ?s WHERE { ?s a :Person FILTER EXISTS { ?s :knows ?x OPTIONAL { ?x :email ?m } } }",
        id="exists_opt",
    ),
    pytest.param(
        "SELECT ?s WHERE { ?s a :Person ; :knows ?o FILTER EXISTS { ?t a :Person ; :knows ?o OPTIONAL { ?o :email ?m } } }",
        id="exists_opt_shared",
    ),
    pytest.param("SELECT ?s ?x ?m WHERE { ?s a :Person ; :knows ?x . ?x :email ?m }", id="bgp_email"),
    pytest.param(
        "SELECT ?s ?c ?m WHERE { ?s a :Person ; :worksFor ?c . ?c :email ?m }", id="bgp_email_on_company"
    ),
    pytest.param(
        "SELECT ?s ?c ?p ?v WHERE { ?s a :Person ; :worksFor ?c . ?c ?p ?v FILTER(?p != rdf:type) }",
        id="bgp_variable_predicate",
    ),
    pytest.param(
        "SELECT ?s ?c WHERE { ?s a :Person ; :worksFor ?c FILTER EXISTS { ?c :email ?m } }", id="exists_email"
    ),
    pytest.param("SELECT ?s ?x WHERE { ?s a :Person ; :knows ?x MINUS { ?x :email ?m } }", id="minus_email"),
    pytest.param(
        "SELECT ?s ?c WHERE { ?s a :Person ; :worksFor ?c FILTER NOT EXISTS { ?c ?p ?v } }",
        id="not_exists_variable_predicate",
    ),
    pytest.param("SELECT ?m WHERE { :acme :email ?m }", id="constant_subject"),
    pytest.param(
        "SELECT ?p ?o WHERE { :acme ?p ?o FILTER(?p != rdf:type) }", id="constant_variable_predicate"
    ),
    pytest.param(
        "SELECT ?s ?c ?v WHERE { ?s a :Person ; :worksFor ?c { ?c :cname ?v } UNION { ?c :email ?v } }",
        id="union_stores",
    ),
    pytest.param("SELECT ?c ?m WHERE { VALUES ?c { :acme :b :initech } ?c :email ?m }", id="values_subject"),
    pytest.param("SELECT ?c ?m WHERE { VALUES ?c { :acme UNDEF } ?c :email ?m }", id="values_undef"),
    pytest.param(
        "SELECT ?s ?c ?m WHERE { ?s a :Person OPTIONAL { ?s :worksFor ?c } OPTIONAL { ?c :email ?m } }",
        id="chained_optional_email",
    ),
    pytest.param(
        "SELECT ?s ?c ?n WHERE { ?s a :Person OPTIONAL { ?s :worksFor ?c } OPTIONAL { ?c :cname ?n } }",
        id="chained_optional_cname",
    ),
    pytest.param(
        'SELECT ?s ?c WHERE { ?s a :Person ; :worksFor ?c . ?c :email "info@acme.example" }',
        id="constant_object",
    ),
    pytest.param("SELECT ?x ?m WHERE { ?x :email ?m }", id="first_seen_subject"),
    pytest.param("SELECT ?x ?m ?n WHERE { ?x :email ?m . ?x :cname ?n }", id="first_seen_two_stores"),
    pytest.param(
        "SELECT ?s ?x ?y ?m WHERE { ?s a :Person ; :knows ?x . ?x :knows ?y . ?y :email ?m }",
        id="value_chain",
    ),
]

# A value an earlier OPTIONAL can leave unbound joins any value (SPARQL 1.1
# §18.3), in the object position of a read from every store too. On the
# undeclared mapping these read every store; the declared one routes
# ``:knows`` / ``:email`` to the triples-table reader alone, whose object
# position is #193's.
OBJECT_JOIN_QUERIES = [
    pytest.param(
        "SELECT ?s ?x ?y WHERE { ?s a :Person OPTIONAL { ?s :email ?x } OPTIONAL { ?y :knows ?x } }",
        id="optional_object_unbound",
    ),
    pytest.param(
        "SELECT ?s ?x ?y WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } OPTIONAL { ?s :email ?y } OPTIONAL { ?x :knows ?y } }",
        id="optional_subject_and_object_unbound",
    ),
    pytest.param(
        "SELECT ?s ?x ?y WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } OPTIONAL { ?s :email ?y } FILTER EXISTS { ?x :email ?y } }",
        id="exists_two_unbound",
    ),
]


# The untyped document, reached as a constant, a first-seen subject and a
# VALUES row; its variable predicate needs its attributes declared.
UNTYPED_QUERIES = [
    (pytest.param("SELECT ?v WHERE { :zeta :phone ?v }", id="untyped_constant"), sorted(ONTOLOGIES)),
    (pytest.param("SELECT ?x ?l WHERE { ?x :label ?l }", id="untyped_first_seen"), sorted(ONTOLOGIES)),
    (
        pytest.param("SELECT ?c ?l WHERE { VALUES ?c { :zeta :acme } ?c :label ?l }", id="untyped_values"),
        sorted(ONTOLOGIES),
    ),
    (
        pytest.param(
            "SELECT ?p ?o WHERE { :zeta ?p ?o FILTER(?p != rdf:type) }", id="untyped_variable_predicate"
        ),
        ["no_domain"],
    ),
]


def make_resolver(ontology: str, strict: bool) -> SchemaResolver:
    """The mapping's resolver: the library's (non-strict, default collection
    :data:`DEFAULT_COLLECTION`) or the service's strict one."""
    resolver = SchemaResolver.from_turtle(ONTOLOGIES[ontology], default_collection=DEFAULT_COLLECTION)
    resolver.strict_subject_resolution = strict
    return resolver


def collections() -> dict[str, list[dict[str, Any]]]:
    """The stored rows and documents. The default collection is there for
    both resolvers: data on the non-strict one, which the strict one must
    not read."""
    return {"T191": _rows(), "C191": DOCS, DEFAULT_COLLECTION: UNTYPED_DOCS}


def facts(strict: bool) -> str:
    """The RDF the mapping exposes: the untyped document only without strict
    subject resolution."""
    return TRIPLES + DOC_FACTS + ("" if strict else UNTYPED_FACTS)


def _rows() -> list[dict[str, Any]]:
    """One triples-table row per triple: IRIs in ``object_uri``, literals in ``object_value``."""
    rows: list[dict[str, Any]] = []
    for s, p, o in rdflib.Graph().parse(data=TRIPLES, format="turtle"):
        row: dict[str, Any] = {"subject_uri": str(s), "predicate": str(p)}
        if isinstance(o, rdflib.URIRef):
            row["object_uri"] = str(o)
        else:
            row["object_value"] = o.toPython()
        rows.append(row)
    return rows


def cases() -> list[Any]:
    """Every (query, mapping) pair: :data:`QUERIES` on every mapping,
    :data:`OBJECT_JOIN_QUERIES` on the undeclared one, :data:`UNTYPED_QUERIES`
    on the mappings each names."""
    return (
        [
            pytest.param(q.values[0], ontology, id=f"{q.id}-{ontology}")
            for q in QUERIES
            for ontology in sorted(ONTOLOGIES)
        ]
        + [pytest.param(q.values[0], "undeclared", id=f"{q.id}-undeclared") for q in OBJECT_JOIN_QUERIES]
        + [
            pytest.param(q.values[0], ontology, id=f"{q.id}-{ontology}")
            for q, ontologies in UNTYPED_QUERIES
            for ontology in ontologies
        ]
    )


@pytest.mark.cross
@pytest.mark.parametrize("strict", [False, True], ids=["library", "strict"])
@pytest.mark.parametrize(("where", "ontology"), cases())
def test_an_unplaced_subject_in_a_mixed_mapping_matches_oxigraph(
    where: str, ontology: str, strict: bool
) -> None:
    query = PREFIX + where

    result = translate(query, resolver=make_resolver(ontology, strict))
    actual = [drop_null_bindings(r) for r in run_aql_subset(result.aql, result.bind_vars, collections())]

    store = load_store_from_string(facts(strict))
    expected = [normalize_oxi_row(r) for r in oxi_bindings(store, query)]
    assert_bindings_equal(expected, actual)


_DOCUMENT_ONLY = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix phys: <https://arango.solutions/phys#> .
:Person a owl:Class ; phys:collectionName "P191" .
:email a owl:DatatypeProperty ; rdfs:domain :Person .
"""


@pytest.mark.cross
@pytest.mark.parametrize("mapping", ["triples_table_domain", "document_domain"])
def test_a_declared_domain_is_read_not_the_default_collection(mapping: str) -> None:
    # The one answer #191 changes from main: ``:email`` declares a domain,
    # so an untyped ``:zeta`` with an email in the default collection is not
    # one of its subjects. A triples-table domain (``:Person`` is RPT) reads
    # the table, as a document domain reads its collection; main read
    # ``Document`` for the former only because it could not route a
    # triples-table domain (review of #199).
    if mapping == "triples_table_domain":
        resolver = make_resolver("declared", strict=False)
    else:
        resolver = SchemaResolver.from_turtle(_DOCUMENT_ONLY, default_collection=DEFAULT_COLLECTION)
    stored = {
        **collections(),
        "P191": [],
        DEFAULT_COLLECTION: [{"_key": "zeta", "_uri": EX + "zeta", "email": "z@x"}],
    }

    result = translate(PREFIX + "SELECT ?m WHERE { :zeta :email ?m }", resolver=resolver)

    assert DEFAULT_COLLECTION not in result.bind_vars.values()
    assert run_aql_subset(result.aql, result.bind_vars, stored) == []
