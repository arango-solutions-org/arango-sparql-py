"""A triple store detected without an ontology has no classes or properties of its own.

Schema detection describes a triples table (RPT) per table, not per
``rdf:type`` class (PRD §6.3.2). Without an ontology the resolver names its
entities with generated ``urn:arango-sparql:concept#`` IRIs, which a triples
table never stores, so a type or predicate FILTER on one could only match no
rows: a silent wrong answer. Translation refuses it, and the refusal of an
undeclared class says which triples tables need an ontology.

Found by reproducing the open 2026-09-29 prd-sync observation on a live
ArangoDB (the schema_live seed): ``?s a <urn:…#_triples>`` and
``?s <urn:…#KNOWS> ?o`` returned 0 rows with no error on both detection
strategies; the data's own predicate IRIs returned the right rows.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.errors import SchemaResolutionError, UnsupportedSparqlError
from arango_sparql.service.mapping import _resolver_from_request
from arango_sparql.translate.mapping import MappingBundle, MappingSource
from arango_sparql.translate.resolver import SchemaResolver, triples_tables
from tests.helpers.aql_interp import run_aql_subset

_C = "urn:arango-sparql:concept#"
_EX = "http://ex.org/"
_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

#: What both detection strategies produce for a pure ``_triples`` store:
#: one RPT entity named after the table, RPT_EDGE relationships, no OWL.
_DETECTED_RPT = MappingBundle(
    physical_mapping={
        "entities": {"_triples": {"style": "RPT", "triplesCollection": "_triples"}},
        "relationships": {
            "KNOWS": {
                "style": "RPT_EDGE",
                "triplesCollection": "_triples",
                "fromEntity": "_triples",
                "toEntity": "_triples",
            }
        },
    },
    source=MappingSource(kind="heuristic"),
)

_PG = MappingBundle(
    physical_mapping={"entities": {"Person": {"style": "COLLECTION", "collectionName": "persons"}}},
    source=MappingSource(kind="heuristic"),
)

#: An ontology that declares the data's own class for the triples table.
_RPT_OWL = f"""
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
<{_EX}Person> a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "_triples" .
"""

_TRIPLES: dict[str, list[dict[str, Any]]] = {
    "_triples": [
        {
            "subject_uri": f"{_EX}a",
            "predicate": _RDF_TYPE,
            "object_uri": f"{_EX}Person",
            "object_value": None,
        },
        {
            "subject_uri": f"{_EX}b",
            "predicate": _RDF_TYPE,
            "object_uri": f"{_EX}Person",
            "object_value": None,
        },
        {"subject_uri": f"{_EX}a", "predicate": f"{_EX}knows", "object_uri": f"{_EX}b", "object_value": None},
    ]
}


def _detected() -> SchemaResolver:
    return SchemaResolver.from_mapping_bundle(_DETECTED_RPT)


def _service(ontology_ttl: str | None, bundle: MappingBundle) -> SchemaResolver:
    return _resolver_from_request(
        SimpleNamespace(ontology_ttl=ontology_ttl, mapping=None), analyzer_bundle=bundle
    )


def test_a_generated_class_on_a_triples_table_is_refused() -> None:
    with pytest.raises(SchemaResolutionError, match="generated from the mapping") as err:
        translate(f"SELECT ?s WHERE {{ ?s a <{_C}_triples> }}", resolver=_detected())

    assert err.value.code == "E_SCHEMA_RESOLVE"
    assert "'_triples'" in str(err.value)
    assert "phys:mappingStyle" in str(err.value)


@pytest.mark.parametrize("predicate", ["KNOWS", "name"])
def test_a_generated_predicate_on_a_triples_table_is_refused(predicate: str) -> None:
    with pytest.raises(SchemaResolutionError, match="generated from the mapping") as err:
        translate(f"SELECT ?s ?o WHERE {{ ?s <{_C}{predicate}> ?o }}", resolver=_detected())

    assert "Use the predicate IRI the data stores" in str(err.value)


def test_a_generated_predicate_on_a_declared_rpt_class_is_refused() -> None:
    resolver = SchemaResolver.from_turtle(_RPT_OWL)

    with pytest.raises(SchemaResolutionError, match="generated from the mapping"):
        translate(f"SELECT ?s ?o WHERE {{ ?s a <{_EX}Person> ; <{_C}knows> ?o }}", resolver=resolver)


def test_a_generated_predicate_in_a_cross_subject_optional_is_refused() -> None:
    resolver = SchemaResolver.from_turtle(_RPT_OWL)
    query = (
        f"SELECT ?s ?o ?e WHERE {{ ?s a <{_EX}Person> ; <{_EX}knows> ?o . OPTIONAL {{ ?o <{_C}email> ?e }} }}"
    )

    with pytest.raises(SchemaResolutionError, match="generated from the mapping"):
        translate(query, resolver=resolver)


@pytest.mark.parametrize(
    "required",
    [pytest.param(f"?s <{_EX}knows> ?o", id="detected"), pytest.param(f"?s a <{_EX}Person>", id="declared")],
)
def test_a_generated_predicate_in_a_same_subject_optional_is_refused(required: str) -> None:
    # The OPTIONAL read ``doc.name`` off the triples row and bound nothing,
    # with no error (review of #189); it now runs through the triples reader.
    resolver = _detected() if "knows" in required else SchemaResolver.from_turtle(_RPT_OWL)

    with pytest.raises(SchemaResolutionError, match="generated from the mapping"):
        translate(f"SELECT ?s ?x WHERE {{ {required} OPTIONAL {{ ?s <{_C}name> ?x }} }}", resolver=resolver)


def test_a_same_subject_optional_on_a_triples_table_reads_the_table() -> None:
    query = f"SELECT ?s ?x WHERE {{ ?s a <{_EX}Person> OPTIONAL {{ ?s <{_EX}knows> ?x }} }}"
    result = translate(query, resolver=SchemaResolver.from_turtle(_RPT_OWL))

    rows = run_aql_subset(result.aql, result.bind_vars, _TRIPLES)

    assert sorted((r["s"], r["x"]) for r in rows) == [(f"{_EX}a", f"{_EX}b"), (f"{_EX}b", None)]


def test_a_shared_optional_binding_on_a_triples_subject_inside_minus_is_refused() -> None:
    # Inside MINUS the OPTIONAL stays inline (conditional-add, SPARQL
    # §18.2.5.2), whose document attribute read cannot see a triples row.
    query = (
        f"SELECT ?s WHERE {{ ?s a <{_EX}Person> ; <{_EX}knows> ?o "
        f"MINUS {{ ?t a <{_EX}Person> OPTIONAL {{ ?t <{_EX}knows> ?o }} }} }}"
    )

    with pytest.raises(UnsupportedSparqlError, match="inside MINUS when the binding is shared"):
        translate(query, resolver=SchemaResolver.from_turtle(_RPT_OWL))


def test_the_data_s_own_predicate_still_reads_the_triples_table() -> None:
    result = translate(f"SELECT ?s ?o WHERE {{ ?s <{_EX}knows> ?o }}", resolver=_detected())

    rows = run_aql_subset(result.aql, result.bind_vars, _TRIPLES)

    assert rows == [{"s": f"{_EX}a", "o": f"{_EX}b"}]


def test_a_declared_rpt_class_still_reads_its_rows() -> None:
    result = translate(
        f"SELECT ?s WHERE {{ ?s a <{_EX}Person> }}", resolver=SchemaResolver.from_turtle(_RPT_OWL)
    )

    rows = run_aql_subset(result.aql, result.bind_vars, _TRIPLES)

    assert sorted(r["s"] for r in rows) == [f"{_EX}a", f"{_EX}b"]


def test_a_generated_class_outside_a_triples_table_still_translates() -> None:
    result = translate(
        f"SELECT ?s WHERE {{ ?s a <{_C}Person> }}", resolver=SchemaResolver.from_mapping_bundle(_PG)
    )

    assert result.bind_vars["@c1_persons"] == "persons"


def test_an_undeclared_class_on_a_detected_triple_store_names_the_table() -> None:
    with pytest.raises(SchemaResolutionError) as err:
        translate(f"SELECT ?s WHERE {{ ?s a <{_EX}Person> }}", resolver=_detected())

    message = str(err.value)
    assert message.startswith(f"class IRI '{_EX}Person' is not declared owl:Class in the ontology. ")
    assert "keeps triples in '_triples'" in message
    assert 'phys:mappingStyle "RPT"' in message


def test_the_service_resolver_names_the_detected_triples_table() -> None:
    with pytest.raises(SchemaResolutionError, match="keeps triples in '_triples'"):
        translate(f"SELECT ?s WHERE {{ ?s a <{_EX}Person> }}", resolver=_service(None, _DETECTED_RPT))


def test_no_table_hint_once_an_ontology_declares_a_class_for_the_table() -> None:
    resolver = _service(_RPT_OWL, _DETECTED_RPT)

    with pytest.raises(SchemaResolutionError) as err:
        translate(f"SELECT ?s WHERE {{ ?s a <{_EX}Missing> }}", resolver=resolver)

    assert str(err.value) == f"class IRI '{_EX}Missing' is not declared owl:Class in the ontology"
    assert resolver.triples_tables_without_classes() == ()


def test_no_table_hint_without_a_triples_table() -> None:
    with pytest.raises(SchemaResolutionError) as err:
        translate(
            f"SELECT ?s WHERE {{ ?s a <{_EX}Missing> }}", resolver=SchemaResolver.from_mapping_bundle(_PG)
        )

    assert str(err.value) == f"class IRI '{_EX}Missing' is not declared owl:Class in the ontology"


def test_triples_tables_lists_each_rpt_table_once() -> None:
    bundle = MappingBundle(
        physical_mapping={
            "entities": {
                "A": {"style": "RPT", "triplesCollection": "_triples"},
                "B": {"style": "RPT"},
                "C": {"style": "RPT", "collectionName": "quads", "triplesCollection": "_triples"},
                "D": {"style": "COLLECTION", "collectionName": "docs"},
            }
        }
    )

    assert triples_tables(bundle) == ("_triples", "quads")
    assert triples_tables(None) == ()
