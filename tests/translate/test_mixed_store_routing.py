"""Which store a triple on an unplaced subject is read from, in a mapping
with triples tables and documents (#191; :mod:`arango_sparql.translate.mixed_store`).

The answers are cross-validated against pyoxigraph in
``tests/cross/test_mixed_store_cross.py`` and live in
``tests/integration/test_mixed_store_live.py``. These tests pin the routing
itself: the predicate's ``rdfs:domain`` decides when it can, every store is
read when it cannot (with the default collection on the library's non-strict
resolver, never under strict subject resolution), and a variable predicate
refuses rather than mix attribute names with IRIs.
"""

from __future__ import annotations

import pytest

from arango_sparql.api import TranslateResult, translate
from arango_sparql.errors import SchemaResolutionError, UnsupportedSparqlError
from arango_sparql.translate.resolver import SchemaResolver
from tests.cross.test_mixed_store_cross import DEFAULT_COLLECTION, ONTOLOGIES, PREFIX, cases, make_resolver

_NO_DATATYPE_PROPERTIES = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
:Person a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "T191" .
:Company a owl:Class ; phys:mappingStyle "COLLECTION" ; phys:collectionName "C191" .
"""
_EDGES = (
    ONTOLOGIES["undeclared"]
    + """
:partner a owl:ObjectProperty ; phys:edgeCollectionName "partners" .
"""
)


def _translate(where: str, ttl: str, *, strict: bool = True) -> TranslateResult:
    resolver = SchemaResolver.from_turtle(ttl)
    resolver.strict_subject_resolution = strict
    return translate(PREFIX + where, resolver=resolver)


def _collections(result: TranslateResult) -> set[str]:
    return {v for k, v in result.bind_vars.items() if k.startswith("@")}


@pytest.mark.parametrize("strict", [False, True], ids=["library", "strict"])
@pytest.mark.parametrize(("where", "ontology"), cases())
def test_an_unplaced_subject_reads_the_default_collection_only_without_strict_resolution(
    where: str, ontology: str, strict: bool
) -> None:
    result = translate(PREFIX + where, resolver=make_resolver(ontology, strict))

    assert _collections(result) <= ({"T191", "C191"} if strict else {"T191", "C191", DEFAULT_COLLECTION})


@pytest.mark.parametrize("strict", [False, True], ids=["library", "strict"])
def test_every_store_includes_the_default_collection_only_without_strict_resolution(strict: bool) -> None:
    # The non-strict contract keeps an IRI no class places in the default
    # collection, so a read of every store reads it too (review of #199).
    result = _translate("SELECT ?m WHERE { :zeta :email ?m }", ONTOLOGIES["undeclared"], strict=strict)

    assert _collections(result) == ({"T191", "C191"} if strict else {"T191", "C191", "Document"})


def test_a_domain_naming_an_undeclared_class_reads_the_default_collection_for_it() -> None:
    # ``rdfs:domain :Company, :Any``: the undeclared class stands for the
    # default collection on the non-strict resolver (#126) and is refused
    # under strict resolution, as the visitor's domain routing does.
    ttl = ONTOLOGIES["undeclared"] + ":rating a owl:DatatypeProperty ; rdfs:domain :Company , :Any .\n"
    where = "SELECT ?c ?r WHERE { ?s a :Person ; :worksFor ?c . ?c :rating ?r }"

    assert _collections(_translate(where, ttl, strict=False)) == {"T191", "C191", "Document"}
    with pytest.raises(SchemaResolutionError):
        _translate(where, ttl, strict=True)


@pytest.mark.parametrize(
    "where",
    [
        "SELECT ?g ?m WHERE { GRAPH ?g { ?c :email ?m } }",
        "SELECT ?c ?n WHERE { ?s a :Person ; :worksFor ?c GRAPH ?g { ?c :cname ?n } }",
    ],
)
def test_a_routed_read_inside_graph_with_an_unbound_variable_is_refused(where: str) -> None:
    with pytest.raises(UnsupportedSparqlError, match="#203"):
        _translate(where, ONTOLOGIES["undeclared"])


def test_a_domain_of_documents_reads_the_documents_by_iri_without_placing_the_subject() -> None:
    result = _translate(
        "SELECT ?c ?n ?m WHERE { ?s a :Person ; :worksFor ?c . ?c :cname ?n . ?c :email ?m }",
        ONTOLOGIES["undeclared"],
    )

    # :cname reads the Company documents in place, on the subject's IRI ...
    company = "NOT_NULL(doc2.object_uri, doc2.object_value)"
    assert f"FOR doc3 IN @@c2_C191\nFILTER ({company} != null && (doc3._uri == {company} ||" in result.aql
    assert 'FILTER HAS(doc3, "cname")' in result.aql
    # ... and :email, which has no domain, still reads both stores: the
    # company was not taken to be only a document.
    assert result.aql.count("UNION_DISTINCT(") == 1
    assert _collections(result) == {"T191", "C191"}


def test_no_domain_reads_every_store_as_one_set() -> None:
    result = _translate(
        "SELECT ?c ?m WHERE { ?s a :Person ; :worksFor ?c . ?c :email ?m }", ONTOLOGIES["undeclared"]
    )

    assert "UNION_DISTINCT(" in result.aql
    assert _collections(result) == {"T191", "C191"}


def test_a_domain_in_a_triples_table_reads_that_table_alone() -> None:
    result = _translate(
        "SELECT ?x ?m WHERE { ?s a :Person ; :knows ?x . ?x :email ?m }", ONTOLOGIES["declared"]
    )

    assert "UNION" not in result.aql
    assert _collections(result) == {"T191"}


def test_a_variable_predicate_over_both_stores_refuses_without_declared_properties() -> None:
    with pytest.raises(UnsupportedSparqlError, match="attribute names, not IRIs"):
        _translate(
            "SELECT ?c ?p ?v WHERE { ?s a :Person ; :worksFor ?c . ?c ?p ?v }", _NO_DATATYPE_PROPERTIES
        )


def test_an_edge_predicate_is_left_to_the_edge_reader() -> None:
    # Its triples are edges, not rows or attributes: no store union.
    result = _translate(
        "SELECT ?c ?d WHERE { ?s a :Person ; :worksFor ?c . ?c :partner ?d }", _EDGES, strict=False
    )

    assert "UNION_DISTINCT(" not in result.aql
    assert "partners" in _collections(result)


def test_a_single_triple_store_is_not_routed_here() -> None:
    ttl = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
:Person a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "T191" .
"""
    result = _translate("SELECT ?c ?m WHERE { ?s a :Person ; :worksFor ?c . ?c :email ?m }", ttl)

    assert "UNION" not in result.aql
    assert _collections(result) == {"T191"}
