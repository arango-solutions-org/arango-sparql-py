"""A union row that stands for every copy of its IRI (#147, ``translate/copies.py``).

Results are checked against pyoxigraph in
``tests/cross/test_multi_document_iri_cross.py`` (and live); these pin the
choices that keep the common read cheap: what is read from the row's own
document, what from every copy, and where the copies are looked up.
"""

from __future__ import annotations

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver, UriIndexes

_TTL = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
:Person a owl:Class ; phys:collectionName "Person" ; rdfs:subClassOf :Agent .
:Org a owl:Class ; phys:collectionName "Org" ; rdfs:subClassOf :Agent .
:Agent a owl:Class ; phys:mappingStyle "UNION" .
:Employee a owl:Class ; phys:collectionName "Employee" .
:name a owl:DatatypeProperty ; rdfs:domain [ owl:unionOf ( :Person :Org ) ] .
:salary a owl:DatatypeProperty ; rdfs:domain :Employee .
"""


def _aql(query: str) -> str:
    return translate("PREFIX : <http://ex.org/>\n" + query, resolver=SchemaResolver.from_turtle(_TTL)).aql


def test_an_attribute_every_member_holds_is_read_from_the_row_and_compared() -> None:
    aql = _aql("SELECT ?s ?n WHERE { ?s a :Agent ; :name ?n }")
    assert "UNIQUE((" not in aql
    assert 'HAS(doc4, "name") == HAS(doc3, "name") && doc4.name == doc3.name' in aql


def test_an_attribute_on_another_copy_is_looked_up_in_the_members_and_its_domain() -> None:
    # The domain (Employee) is where it is declared; a member's copy can still
    # hold a value (review of #171), so both are looked up.
    aql = _aql("SELECT ?s ?y WHERE { ?s a :Agent ; :salary ?y }")
    lookup = aql[aql.index("UNIQUE((") : aql.index("FILTER HAS(")]
    assert all(f"_{c}" in lookup for c in ("Employee", "Org", "Person"))


def test_the_lookup_is_guarded_and_served_by_a_sparse_index() -> None:
    # A row with no _uri reads its own document and starts no subquery; the
    # lookup filters _uri != null, so a sparse _uri index serves it (review
    # of #171: an _uri-or-_id match scanned every collection per row).
    aql = _aql("SELECT ?s ?y WHERE { ?s a :Agent ; :salary ?y }")
    assert "LET vals" in aql and "._uri != null) ? UNIQUE((" in aql
    assert ': (HAS(doc3, "salary") ? [doc3.salary] : [])' in aql
    lookup = aql[aql.index("UNIQUE((") : aql.index("FILTER HAS(")]
    assert "_id ==" not in lookup


def test_a_compared_attribute_read_again_later_is_read_from_every_copy() -> None:
    # The rows kept differ on it; each pairs with every copy's value.
    aql = _aql("SELECT ?s ?n ?m WHERE { ?s a :Agent ; :name ?n OPTIONAL { ?s :name ?m } }")
    assert 'HAS(doc4, "name") == HAS(doc3, "name")' in aql
    assert "UNIQUE((" in aql[aql.index("LET optsub") :]


def test_an_optional_that_rebinds_a_variable_is_still_refused() -> None:
    import pytest

    from arango_sparql.errors import UnsupportedSparqlError

    with pytest.raises(UnsupportedSparqlError, match="re-binds variable"):
        _aql("SELECT ?s ?y WHERE { ?s a :Agent OPTIONAL { ?s :name ?y } OPTIONAL { ?s :salary ?y } }")


def test_an_optional_attribute_is_read_from_every_copy_and_not_compared() -> None:
    aql = _aql("SELECT ?s ?n WHERE { ?s a :Agent OPTIONAL { ?s :name ?n } }")
    probe = aql[aql.index("LET repeated") : aql.index("FILTER repeated")]
    assert '"name"' not in probe
    optional = aql[aql.index("LET optsub") :]
    assert "UNIQUE((" in optional


def test_a_read_in_another_scope_does_not_make_the_row_compare_it() -> None:
    # The sub-select reads its own ?s: the outer row reads nothing, so it
    # keeps one row per IRI (#148).
    aql = _aql("SELECT ?s ?n WHERE { ?s a :Agent { SELECT ?s ?n WHERE { ?s :name ?n } } }")
    outer_probe = aql[aql.index("LET repeated") : aql.index("FILTER repeated")]
    assert '"name"' not in outer_probe


def test_an_edge_out_of_the_subject_keeps_every_copy() -> None:
    ttl = _TTL + ':knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; rdfs:domain :Person .\n'
    aql = translate(
        "PREFIX : <http://ex.org/>\nSELECT ?s ?x WHERE { ?s a :Agent ; :knows ?x }",
        resolver=SchemaResolver.from_turtle(ttl),
    ).aql
    assert "repeated" not in aql


def test_a_lookup_in_an_unindexed_collection_is_warned_about() -> None:
    # The members are indexed; the domain collection the salary is looked up
    # in is not, so each row scans it (W_SCHEMA_URI_INDEX_MISSING, §6.1).
    every = frozenset({"Person", "Org", "Employee"})
    resolver = SchemaResolver(
        ontology=SchemaResolver.from_turtle(_TTL).ontology,
        uri_indexes=UriIndexes(collections=every, carrying=every, indexed=frozenset({"Person", "Org"})),
    )
    result = translate(
        "PREFIX : <http://ex.org/>\nSELECT ?s ?y WHERE { ?s a :Agent ; :salary ?y }", resolver=resolver
    )
    warned = {w["iri"] for w in result.schema_warnings if w["code"] == "W_SCHEMA_URI_INDEX_MISSING"}
    assert warned == {"Employee"}
