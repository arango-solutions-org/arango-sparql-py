"""Caller-registered predicates (``translate.predicates``, issue #102).

Covers the seam with two handlers, the ready ArangoSearch text handler and a
nearest-neighbour handler written here as a caller would, and every refusal.
The live behaviour of the text handler is in
``tests/integration/test_predicates_live.py``.
"""

from __future__ import annotations

import re

import pytest
from rdflib import Literal

from arango_sparql.api import translate
from arango_sparql.errors import UnsupportedSparqlError
from arango_sparql.translate.predicates import (
    LoopEmitter,
    LoopPlan,
    PredicateCall,
    TextIndex,
    text_match_handler,
)
from arango_sparql.translate.resolver import SchemaResolver

MATCHES = "urn:fn#matchesText"
NEAR = "urn:fn#nearVector"

_TTL = """
@prefix :     <http://ex.org/> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
:Chunk    a owl:Class ; phys:collectionName "chunks" .
:Document a owl:Class ; phys:collectionName "documents" .
:text     a owl:DatatypeProperty ; phys:attributeName "text" .
:title    a owl:DatatypeProperty ; phys:attributeName "title" .
:embedding a owl:DatatypeProperty ; phys:attributeName "embedding" .
:about    a owl:ObjectProperty ; phys:edgeCollectionName "about" .
"""

_PREFIX = "PREFIX : <http://ex.org/>\nPREFIX f: <urn:fn#>\n"


def _near_vector(call: PredicateCall, emit: LoopEmitter) -> LoopPlan:
    """A nearest-neighbour handler as a caller would write it:
    ``(?e ?sim) f:nearVector (:embedding k)`` against a fixed vector."""
    assert call.searched is not None
    k = call.arguments[1].toPython()
    similarity = (
        f"APPROX_NEAR_COSINE({emit.field(call.searched.field_path)}, {emit.bind([0.1, 0.2], hint='vector')})"
    )
    return LoopPlan(
        source=emit.bind_source(call.collection),
        sort=similarity,
        limit=emit.bind(k, hint="k"),
        bindings={str(call.subjects[1]): similarity},
    )


def _resolver() -> SchemaResolver:
    return SchemaResolver.from_turtle(
        _TTL,
        predicates={
            MATCHES: text_match_handler({"chunks": TextIndex("chunks_text", "text_en", ("text",))}),
            NEAR: _near_vector,
        },
    )


def _translate(query: str):  # noqa: ANN202
    return translate(_PREFIX + query, resolver=_resolver())


def test_a_text_match_iterates_the_view_and_binds_bm25() -> None:
    result = _translate(
        'SELECT ?c ?score WHERE { (?c ?score) f:matchesText (:text "late renewal") . ?c a :Chunk }'
    )
    assert result.aql == (
        "FOR doc1 IN @@c1_chunks_text "
        "SEARCH ANALYZER(TOKENS(@_p1_text, @_p2_analyzer) ALL == doc1.text, @_p2_analyzer) "
        "OPTIONS { collections: [@_p3_collection] }\n"
        "RETURN { c: NOT_NULL(doc1._uri, doc1._id), score: BM25(doc1) }"
    )
    assert result.bind_vars == {
        "@c1_chunks_text": "chunks_text",
        "_p1_text": "late renewal",
        "_p2_analyzer": "text_en",
        "_p3_collection": "chunks",
    }


def test_a_text_match_with_a_limit_keeps_the_best_scored_before_the_rest_of_the_pattern() -> None:
    result = _translate('SELECT ?c WHERE { ?c :seq ?n . ?c a :Chunk . ?c f:matchesText (:text "renewal" 3) }')
    lines = result.aql.splitlines()
    assert lines[1:3] == ["SORT BM25(doc1) DESC", "LIMIT @_p4_limit"]
    assert lines[3] == 'FILTER HAS(doc1, "seq")'
    assert result.bind_vars["_p4_limit"] == 3


def test_a_nearest_neighbour_handler_sorts_and_limits_the_entitys_loop() -> None:
    result = _translate("SELECT ?c ?sim WHERE { ?c a :Chunk . (?c ?sim) f:nearVector (:embedding 5) }")
    assert result.aql == (
        "FOR doc1 IN @@c1_chunks\n"
        "SORT APPROX_NEAR_COSINE(doc1.embedding, @_p1_vector) DESC\n"
        "LIMIT @_p2_k\n"
        "RETURN { c: NOT_NULL(doc1._uri, doc1._id), sim: APPROX_NEAR_COSINE(doc1.embedding, @_p1_vector) }"
    )
    assert result.bind_vars == {"@c1_chunks": "chunks", "_p1_vector": [0.1, 0.2], "_p2_k": 5}


def test_the_entity_opens_first_whatever_order_the_pattern_is_written_in() -> None:
    result = _translate(
        'SELECT ?d ?c WHERE { ?d a :Document ; :title ?t . ?c a :Chunk ; f:matchesText (:text "x" 2) }'
    )
    assert result.aql.startswith("FOR doc1 IN @@c1_chunks_text SEARCH")


def test_the_score_orders_like_any_variable() -> None:
    result = _translate(
        'SELECT ?c WHERE { ?c a :Chunk . (?c ?s) f:matchesText (:text "x") } ORDER BY DESC(?s)'
    )
    assert "SORT BM25(doc1) DESC\nRETURN" in result.aql


def test_an_unregistered_iri_stays_an_ordinary_predicate() -> None:
    resolver = SchemaResolver.from_turtle(_TTL)
    result = translate(_PREFIX + 'SELECT ?c WHERE { ?c a :Chunk ; f:matchesText "x" }', resolver=resolver)
    assert "SEARCH" not in result.aql
    assert "FILTER doc1.matchesText == @_p1_matchesText" in result.aql


@pytest.mark.parametrize(
    ("pattern", "message"),
    [
        ('?d a :Document ; f:matchesText (:text "x")', "collection 'documents' has no text index"),
        ('?c a :Chunk ; f:matchesText (:title "x")', "does not index chunks.title"),
        ('?c a :Chunk ; f:matchesText (:about "x")', "is not a mapped datatype property"),
        ('?c a :Chunk ; f:matchesText ("x")', 'takes (:property "terms")'),
        ('?c a :Chunk ; f:matchesText (:text "x" 1 2)', 'takes (:property "terms")'),
        ('?c a :Chunk ; f:matchesText ("x" "y")', "must be an IRI"),
        ('?c a :Chunk ; f:matchesText (:text "")', "non-empty plain string"),
        ("?c a :Chunk ; f:matchesText (:text 42)", "non-empty plain string"),
        ('?c a :Chunk ; f:matchesText (:text "x" 0)', "positive integer"),
        ('(?c ?s ?t) f:matchesText (:text "x") . ?c a :Chunk', "at most a score"),
        ('?c f:matchesText (:text "x")', "needs ?c's class in the same group"),
        ('?c a :Chunk . OPTIONAL { ?c f:matchesText (:text "x") }', "not bound before it"),
        ('?c a :Chunk . FILTER EXISTS { ?c f:matchesText (:text "x") }', "not bound before it"),
        (
            '?x a :Document ; :title ?s . { ?c a :Chunk . (?c ?s) f:matchesText (:text "x") }',
            "is already bound",
        ),
        ('?c a :Chunk . { ?c f:matchesText (:text "x") }', "not bound before it"),
        ('?c a :Chunk ; f:matchesText (:text "x"), (:text "y")', "take one registered predicate"),
        (
            '?d a :Document . { ?c a :Chunk ; f:matchesText (:text "x" 2) }',
            "must apply to the query's first loop",
        ),
        ('(<http://ex.org/c1>) f:matchesText (:text "x")', "must be a variable"),
        ('(?c "s") f:matchesText (:text "x") . ?c a :Chunk', "binds only variables"),
    ],
)
def test_a_call_the_handler_or_the_seam_cannot_answer_is_refused_by_name(pattern: str, message: str) -> None:
    with pytest.raises(UnsupportedSparqlError, match=re.escape(message)) as refused:
        _translate(f"SELECT * WHERE {{ {pattern} }}")
    assert refused.value.code == "E_PREDICATE"


def test_a_handler_may_bind_only_its_subject_list() -> None:
    def rogue(call: PredicateCall, emit: LoopEmitter) -> LoopPlan:
        return LoopPlan(source=emit.bind_source(call.collection), bindings={"elsewhere": "1"})

    resolver = SchemaResolver.from_turtle(_TTL, predicates={MATCHES: rogue})
    with pytest.raises(UnsupportedSparqlError, match="not in its subject list"):
        translate(
            _PREFIX + 'SELECT ?c WHERE { ?c a :Chunk . (?c ?s) f:matchesText (:text "x") }', resolver=resolver
        )


def test_a_handler_sees_the_call_resolved() -> None:
    seen: list[PredicateCall] = []

    def spy(call: PredicateCall, emit: LoopEmitter) -> LoopPlan:
        seen.append(call)
        return LoopPlan(source=emit.bind_source(call.collection))

    resolver = SchemaResolver.from_turtle(_TTL, predicates={MATCHES: spy})
    translate(
        _PREFIX + 'SELECT ?c WHERE { ?c a :Chunk . (?c ?s) f:matchesText (:text "x" 4) }', resolver=resolver
    )
    (call,) = seen
    assert call.predicate == MATCHES
    assert [str(v) for v in call.subjects] == ["c", "s"]
    assert call.arguments[1:] == (Literal("x"), Literal(4))
    assert (call.class_iri, call.collection) == ("http://ex.org/Chunk", "chunks")
    assert call.searched is not None and call.searched.field_path == ("text",)


def test_a_text_index_needs_a_view_an_analyzer_and_fields() -> None:
    with pytest.raises(ValueError, match="needs a view"):
        TextIndex("v", "text_en", ())
