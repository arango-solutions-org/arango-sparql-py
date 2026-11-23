"""A triples-table (RPT) row's object has one spelling in emitted AQL.

``triple_object_ref`` and ``triple_object_is_iri`` (``translate/builder.py``)
replaced the same two expressions written out in six readers (code-quality
review of #196–#199). These pin the helpers and check that each reader of a
table — a property triple, a variable predicate, a cross-subject OPTIONAL,
DESCRIBE and the mixed-store routed read — emits them.
"""

from __future__ import annotations

import re

import pytest

from arango_sparql.api import translate
from arango_sparql.errors import UnsupportedSparqlError
from arango_sparql.translate.builder import triple_object_is_iri, triple_object_ref
from arango_sparql.translate.resolver import ResolvedClass, SchemaResolver

_DEFAULT = ResolvedClass(iri="http://ex.org/T", collection="triples", style="RPT")
# Column names that need backtick quoting, so the helpers are seen to go
# through ``attribute_ref``.
_QUOTED = ResolvedClass(
    iri="http://ex.org/T",
    collection="triples",
    style="RPT",
    object_uri_column="obj uri",
    object_value_column="filter",
)


def test_object_ref_prefers_the_iri_column() -> None:
    assert triple_object_ref("d", _DEFAULT) == "NOT_NULL(d.object_uri, d.object_value)"
    assert triple_object_ref("d", _QUOTED) == "NOT_NULL(d.`obj uri`, d.`filter`)"


def test_object_is_iri_matches_either_column() -> None:
    assert triple_object_is_iri("d", _DEFAULT, "@o") == "(d.object_uri == @o || d.object_value == @o)"
    assert triple_object_is_iri("d", _QUOTED, "@o") == "(d.`obj uri` == @o || d.`filter` == @o)"


def test_a_column_name_that_cannot_be_quoted_is_refused() -> None:
    bad = ResolvedClass(iri="http://ex.org/T", collection="triples", style="RPT", object_uri_column="a`b")
    with pytest.raises(UnsupportedSparqlError, match="backtick"):
        triple_object_ref("d", bad)


_RPT = """
@prefix :     <http://ex.org/> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
:Person a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "triples" .
"""
_MIXED = (
    _RPT
    + """
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
:Company a owl:Class ; phys:collectionName "Company" .
:name a owl:DatatypeProperty ; rdfs:domain :Person, :Company .
"""
)
_OBJECT = re.compile(r"NOT_NULL\((\w+)\.object_uri, \1\.object_value\)")
_IS_IRI = re.compile(r"\((\w+)\.object_uri == @\w+ \|\| \1\.object_value == @\w+\)")


@pytest.mark.parametrize(
    ("ttl", "body", "pattern"),
    [
        (_RPT, "SELECT ?o WHERE { ?s a :Person ; :knows ?o }", _OBJECT),
        (_RPT, "SELECT ?s WHERE { ?s a :Person ; :knows :b }", _IS_IRI),
        (_RPT, "SELECT ?p ?o WHERE { ?s a :Person ; ?p ?o }", _OBJECT),
        (_RPT, "SELECT ?p WHERE { ?s a :Person ; ?p :b }", _IS_IRI),
        (_RPT, "SELECT ?s ?o WHERE { ?s a :Person OPTIONAL { ?t :knows ?o } }", _OBJECT),
        (_RPT, "DESCRIBE :a", _OBJECT),
        (_MIXED, "SELECT ?s ?n WHERE { ?s :name ?n }", _OBJECT),
        (_MIXED, "SELECT ?s WHERE { ?s :name :b }", _IS_IRI),
    ],
    ids=[
        "property",
        "property_iri",
        "variable_predicate",
        "variable_predicate_iri",
        "cross_subject_optional",
        "describe",
        "routed",
        "routed_iri",
    ],
)
def test_every_triples_table_reader_spells_the_object_one_way(
    ttl: str, body: str, pattern: re.Pattern[str]
) -> None:
    aql = translate("PREFIX : <http://ex.org/>\n" + body, resolver=SchemaResolver.from_turtle(ttl)).aql
    assert pattern.search(aql), aql
