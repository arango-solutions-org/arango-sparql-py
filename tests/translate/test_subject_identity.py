"""Issue #18 — a document's identity is ``_uri`` when present, else ``_id``.

Emission-level guard for the fast suite; ``tests/integration/
test_subject_identity_live.py`` proves the behaviour on a real ArangoDB
(distinct non-null subjects, the correct ``COUNT(DISTINCT ?s)``, joins across
edges, constants).
"""

from __future__ import annotations

import pytest

from arango_sparql.api import translate
from arango_sparql.translate.builder import subject_in, subject_matches, subject_matches_value, subject_ref
from arango_sparql.translate.resolver import SchemaResolver

_TTL = """
@prefix :     <http://example.org/> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix phys: <https://arango.solutions/phys#> .
:Asset a owl:Class ; phys:mappingStyle "COLLECTION" ; phys:collectionName "Assets" .
:Owner a owl:Class ; phys:mappingStyle "COLLECTION" ; phys:collectionName "Owners" .
:owns a owl:ObjectProperty ; rdfs:domain :Owner ; rdfs:range :Asset ; phys:edgeCollectionName "owns" .
"""


def _aql(body: str, **kw) -> str:
    resolver = SchemaResolver.from_turtle(_TTL, **kw)
    return translate("PREFIX : <http://example.org/>\n" + body, resolver=resolver).aql


def test_identity_falls_back_to_id() -> None:
    assert subject_ref("doc1") == "NOT_NULL(doc1._uri, doc1._id)"


def test_constant_matching_is_exact_and_index_friendly() -> None:
    # A document with a _uri is never matched by its _id; both sides stay
    # plain attribute comparisons the optimizer can use.
    assert subject_matches("d", "@p") == "(d._uri == @p || (d._uri == null && d._id == @p))"
    assert subject_in("d", "[@a]") == "(d._uri IN [@a] || (d._uri == null && d._id IN [@a]))"


def test_projected_subject_uses_the_identity() -> None:
    assert "RETURN { s: NOT_NULL(doc1._uri, doc1._id) }" in _aql("SELECT ?s WHERE { ?s a :Asset }")


def test_edge_target_and_bound_object_compare_the_same_identity() -> None:
    aql = _aql("SELECT ?o ?a WHERE { ?a a :Asset . ?o a :Owner ; :owns ?a . }")
    # The traversal vertex is joined to the already-bound ?a with the SAME
    # identity expression on both sides — not _uri on one and _id on the other.
    assert "FILTER NOT_NULL(v3._uri, v3._id) == NOT_NULL(doc1._uri, doc1._id)" in aql


@pytest.mark.parametrize(
    "body, joined, earlier",
    [
        # A type pattern on a traversal vertex (``?a`` reached over ``owns``).
        ("SELECT ?a WHERE { :o :owns ?a . ?a a :Asset }", "doc4", "v2"),
        # Two classes for one variable.
        ("SELECT ?s WHERE { ?s a :Owner . ?s a :Asset }", "doc2", "doc1"),
        # Two classes for one constant.
        ("SELECT * WHERE { :x a :Owner . :x a :Asset }", "doc2", "doc1"),
    ],
)
def test_a_class_opened_for_a_bound_subject_joins_on_its_iri_through_the_index(
    body: str, joined: str, earlier: str
) -> None:
    # Same IRI, not the same document: an entity of two classes is a document
    # in each (PRD §6.1). The NOT_NULL(...) == NOT_NULL(...) form hid the _uri
    # index, so each row scanned the whole class (LDBC PG: a person scan per
    # friend, 0.13 s → 0.01 s for 185 rows).
    aql = _aql(body)
    assert f"FILTER {subject_matches_value(joined, subject_ref(earlier))}" in aql
    assert f"{subject_ref(joined)} == {subject_ref(earlier)}" not in aql


def test_constant_subject_uses_the_exact_match_form() -> None:
    aql = _aql("SELECT ?n WHERE { <Assets/a2> :name ?n . }", default_collection="Assets")
    assert "._uri == @" in aql and "._uri == null && " in aql and "._id == @" in aql


def _matches(expr_for: str, value: object, doc: dict) -> bool:
    """Run ``FILTER <expr>`` over one document with the interpreter."""
    from tests.helpers.aql_interp import run_aql_subset

    aql = f"FOR doc1 IN @@c1\nFILTER {expr_for}\nRETURN {{ s: doc1._id }}"
    return bool(run_aql_subset(aql, {"@c1": "D", "_v": value}, {"D": [doc]}))


def test_a_value_join_never_matches_a_null_value() -> None:
    # The run-time value of a join can be null (an unbound OPTIONAL column).
    # NOT_NULL(...) == null never matches (a document has an _id); the
    # index-friendly form must not match every document without a _uri.
    no_uri = {"_id": "D/a"}
    assert not _matches(f"{subject_ref('doc1')} == @_v", None, no_uri)
    assert _matches(subject_matches("doc1", "@_v"), None, no_uri)  # why the guard exists
    assert not _matches(subject_matches_value("doc1", "@_v"), None, no_uri)


@pytest.mark.parametrize(
    "doc, value, expected",
    [
        ({"_id": "D/a"}, "D/a", True),
        ({"_id": "D/a", "_uri": "http://ex.org/a"}, "http://ex.org/a", True),
        ({"_id": "D/a", "_uri": "http://ex.org/a"}, "D/a", False),  # identity is the _uri
        ({"_id": "D/a"}, "D/b", False),
    ],
)
def test_a_value_join_agrees_with_subject_identity(doc: dict, value: str, expected: bool) -> None:
    assert _matches(subject_matches_value("doc1", "@_v"), value, doc) is expected
    assert _matches(f"{subject_ref('doc1')} == @_v", value, doc) is expected
