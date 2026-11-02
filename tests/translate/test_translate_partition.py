"""Unit + golden tests for the federation entry point (CDF M5 WP-C2).

Covers the contract decisions recorded in
``docs/architecture/proposals/federation-entry-point.md``:
sub-SELECT-string wire shape, subject-IRI canonical keys (projection
augmentation), seed-binding pushdown via a trailing ``VALUES`` clause,
and executor-stamped ``as_of`` (``None`` at translate time).

Cross-validation against pyoxigraph (including the two-leg join
scenario) lives in ``tests/cross/test_partition_cross.py``.
"""

from __future__ import annotations

import pytest
from rdflib import Literal, URIRef
from rdflib.namespace import XSD

from arango_sparql.api import translate_partition
from arango_sparql.errors import AqlEmitError, UnsupportedSparqlError
from arango_sparql.partition import _values_clause
from arango_sparql.translate.resolver import SchemaResolver

ONTOLOGY_TTL = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
<http://ex.org/Person> a owl:Class ; phys:collectionName "Person" .
"""

PARTITION = "PREFIX ex: <http://ex.org/> SELECT ?name WHERE { ?p a ex:Person ; ex:name ?name }"


def _resolver() -> SchemaResolver:
    return SchemaResolver.from_turtle(ONTOLOGY_TTL)


# ---------------------------------------------------------------------------
# Canonical keys
# ---------------------------------------------------------------------------


def test_canonical_key_added_to_projection() -> None:
    result = translate_partition(PARTITION, resolver=_resolver(), canonical_keys=["?p"])
    assert result.aql == (
        'FOR doc1 IN @@c1_Person\nFILTER HAS(doc1, "name")\nRETURN { name: doc1.name, p: NOT_NULL(doc1._uri, doc1._id) }'
    )
    assert result.projected_vars == ["name", "p"]
    assert result.canonical_key_columns == {"p": "p"}


def test_canonical_key_already_projected_not_duplicated() -> None:
    sparql = "PREFIX ex: <http://ex.org/> SELECT ?p ?name WHERE { ?p a ex:Person ; ex:name ?name }"
    result = translate_partition(sparql, resolver=_resolver(), canonical_keys=["p"])
    assert result.projected_vars == ["p", "name"]
    # Exactly one ``p:`` column in the RETURN object.
    assert result.aql.count(" p: ") == 1


def test_canonical_key_unbound_var_raises() -> None:
    with pytest.raises(AqlEmitError, match=r"\?ghost"):
        translate_partition(PARTITION, resolver=_resolver(), canonical_keys=["ghost"])


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_provenance_source_objects_and_as_of() -> None:
    result = translate_partition(PARTITION, resolver=_resolver(), source="cmdb-arango")
    assert result.provenance is not None
    assert result.provenance.source == "cmdb-arango"
    assert result.provenance.source_objects == ["Person"]
    assert result.provenance.query_text == PARTITION
    assert result.provenance.aql == result.aql
    # Decision #4: translation is pure; the executor stamps as_of.
    assert result.provenance.as_of is None


# ---------------------------------------------------------------------------
# Seed-binding pushdown
# ---------------------------------------------------------------------------


def test_seed_bindings_become_values_pushdown() -> None:
    result = translate_partition(
        PARTITION,
        resolver=_resolver(),
        canonical_keys=["p"],
        seed_bindings=[
            {"p": URIRef("http://ex.org/alice")},
            {"p": {"type": "uri", "value": "http://ex.org/bob"}},
        ],
    )
    # Distinct seeds on a variable the partition binds are a semi-join: an
    # IN test on the subject against a list computed before the loop, with
    # the keys travelling as a bind variable (never inlined).
    assert result.aql.startswith("LET values2 = NOOPT(@_p1_values)\n")
    assert "FILTER NOT_NULL(doc1._uri, doc1._id) IN values2" in result.aql
    assert result.bind_vars["_p1_values"] == ["http://ex.org/alice", "http://ex.org/bob"]
    # Provenance records the effective query (what actually ran).
    assert "VALUES (?p)" in result.provenance.query_text


def test_seed_rows_union_vars_and_undef_padding() -> None:
    clause = _values_clause(
        [
            {"a": URIRef("http://ex.org/x"), "b": 1},
            {"b": 2},
        ]
    )
    assert clause == "VALUES (?a ?b) { (<http://ex.org/x> 1) (UNDEF 2) }"


def test_seed_term_serialization_matrix() -> None:
    clause = _values_clause(
        [
            {
                "iri": {"type": "uri", "value": "http://ex.org/i"},
                "plain": "hello",
                "typed": Literal("5", datatype=XSD.integer),
                "lang": {"type": "literal", "value": "salut", "xml:lang": "fr"},
                "num": 2.5,
                "flag": True,
                "missing": None,
            }
        ]
    )
    assert "<http://ex.org/i>" in clause
    assert '"hello"' in clause
    assert '"5"^^<http://www.w3.org/2001/XMLSchema#integer>' in clause
    assert '"salut"@fr' in clause
    cells = clause.split("{", 1)[1]
    assert "2.5" in cells.split()
    assert "true" in cells.replace("(", " ").split()
    assert "UNDEF" in cells.split()


def test_seed_literal_escaping_survives_parse() -> None:
    # A hostile "value" from an upstream leg must not be able to break
    # out of its literal and smuggle syntax into the partition text.
    hostile = 'x" } . ?p ex:evil "y'
    result = translate_partition(
        PARTITION,
        resolver=_resolver(),
        seed_bindings=[{"name": hostile}],
    )
    binding_rows = result.bind_vars["_p1_values"]
    assert binding_rows == [hostile]


def test_seed_unsafe_iri_rejected() -> None:
    with pytest.raises(UnsupportedSparqlError, match="unsafe"):
        translate_partition(
            PARTITION,
            resolver=_resolver(),
            seed_bindings=[{"p": {"type": "uri", "value": "http://ex.org/a> <b"}}],
        )


def test_seed_empty_row_rejected() -> None:
    with pytest.raises(UnsupportedSparqlError, match="at least one variable"):
        translate_partition(PARTITION, resolver=_resolver(), seed_bindings=[{}])


# ---------------------------------------------------------------------------
# Contract guards
# ---------------------------------------------------------------------------


def test_non_select_partition_rejected() -> None:
    with pytest.raises(UnsupportedSparqlError, match="SELECT"):
        translate_partition(
            "PREFIX ex: <http://ex.org/> ASK { ?p a ex:Person }",
            resolver=_resolver(),
        )


def test_select_star_projects_all_bound_vars() -> None:
    sparql = "PREFIX ex: <http://ex.org/> SELECT * WHERE { ?p a ex:Person ; ex:name ?name }"
    result = translate_partition(sparql, resolver=_resolver(), canonical_keys=["p"])
    assert set(result.projected_vars) >= {"p", "name"}
    assert result.canonical_key_columns == {"p": "p"}
