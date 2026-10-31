"""The ArangoSearch text handler behind the predicate seam, against a live
ArangoDB (``translate.predicates.text_match_handler``).

Live because what a match means — tokenizing and stemming by the view's
analyzer, every term required, BM25 scores — is ArangoDB's to decide. Text
search is not SPARQL, so there is no pyoxigraph ground truth; each answer is
stated by hand from the documents below. Gated like the rest of the
integration suite (``RUN_INTEGRATION=1``).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.translate.predicates import TextIndex, text_match_handler
from arango_sparql.translate.resolver import SchemaResolver
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

EX = "http://example.org/"
_CHUNKS = "PredicateChunks"
_NOTES = "PredicateNotes"
_VIEW = "PredicateTextView"

_TTL = f"""
@prefix :     <{EX}> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
:Chunk a owl:Class ; phys:mappingStyle "COLLECTION" ; phys:collectionName "{_CHUNKS}" .
:Note  a owl:Class ; phys:mappingStyle "COLLECTION" ; phys:collectionName "{_NOTES}" .
:text    a owl:DatatypeProperty ; phys:attributeName "text" .
:account a owl:DatatypeProperty ; phys:attributeName "account" .
"""

_CHUNK_DOCS = [
    {"_key": "a", "account": "acme", "text": "The customer was frustrated with the renewal price"},
    {"_key": "b", "account": "acme", "text": "Happy customer, renewal signed early"},
    {"_key": "c", "account": "globex", "text": "Frustrated again: the price went up and support was slow"},
    {"_key": "d", "account": "globex"},
]
# Indexed by the same view: a search over chunks must not return it.
_NOTE_DOCS = [{"_key": "n", "text": "frustrated price"}]

_PREFIX = f"PREFIX : <{EX}>\nPREFIX f: <urn:fn#>\n"


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
    _drop(handle)
    handle.create_collection(_CHUNKS).insert_many(_CHUNK_DOCS)
    handle.create_collection(_NOTES).insert_many(_NOTE_DOCS)
    link = {"fields": {"text": {"analyzers": ["text_en"]}}}
    handle.create_arangosearch_view(_VIEW, properties={"links": {_CHUNKS: link, _NOTES: link}})
    _wait_until_indexed(handle, expected=4)
    yield handle
    _drop(handle)


def _drop(handle: Any) -> None:
    if any(view["name"] == _VIEW for view in handle.views()):
        handle.delete_view(_VIEW)
    for name in (_CHUNKS, _NOTES):
        if handle.has_collection(name):
            handle.delete_collection(name)


def _wait_until_indexed(handle: Any, *, expected: int) -> None:
    """A view commits asynchronously; wait until every text is searchable
    (a, b, c and the note each hold one of these terms; d has no text)."""
    query = (
        f"FOR v IN {_VIEW} SEARCH ANALYZER(v.text IN TOKENS('frustrated renewal', 'text_en'), 'text_en') "
        "OPTIONS { waitForSync: true } COLLECT WITH COUNT INTO n RETURN n"
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if list(handle.aql.execute(query)) == [expected]:
            return
        time.sleep(0.2)
    pytest.fail(f"view {_VIEW} did not index {expected} documents in 30 s")


def _ask(db: Any, query: str) -> list[dict[str, Any]]:
    handler = text_match_handler({_CHUNKS: TextIndex(_VIEW, "text_en", ("text",))})
    resolver = SchemaResolver.from_turtle(_TTL, predicates={"urn:fn#matchesText": handler})
    result = translate(_PREFIX + query, resolver=resolver)
    return list(db.aql.execute(result.aql, bind_vars=result.bind_vars))


def _keys(rows: list[dict[str, Any]], var: str = "c") -> list[str]:
    return sorted(row[var].rsplit("/", 1)[-1] for row in rows)


def test_every_term_must_occur(db: Any) -> None:
    assert _keys(_ask(db, 'SELECT ?c WHERE { ?c a :Chunk ; f:matchesText (:text "frustrated price") }')) == [
        "a",
        "c",
    ]
    assert _keys(_ask(db, 'SELECT ?c WHERE { ?c a :Chunk ; f:matchesText (:text "renewal price") }')) == ["a"]


def test_the_analyzer_stems_and_folds_case(db: Any) -> None:
    assert _keys(_ask(db, 'SELECT ?c WHERE { ?c a :Chunk ; f:matchesText (:text "FRUSTRATING") }')) == [
        "a",
        "c",
    ]


def test_a_search_stays_in_its_entitys_collection(db: Any) -> None:
    rows = _ask(db, 'SELECT ?c WHERE { ?c a :Chunk ; f:matchesText (:text "frustrated price") }')
    assert "n" not in _keys(rows)


def test_no_hit_is_an_empty_answer(db: Any) -> None:
    assert _ask(db, 'SELECT ?c WHERE { ?c a :Chunk ; f:matchesText (:text "invoice") }') == []


def test_scores_are_bm25_and_order_the_answer(db: Any) -> None:
    rows = _ask(
        db, 'SELECT ?c ?s WHERE { ?c a :Chunk . (?c ?s) f:matchesText (:text "renewal") } ORDER BY DESC(?s)'
    )
    assert [row["c"].rsplit("/", 1)[-1] for row in rows] == ["b", "a"]
    assert rows[0]["s"] > rows[1]["s"] > 0


def test_a_limit_keeps_the_best_scored_matches(db: Any) -> None:
    rows = _ask(db, 'SELECT ?c WHERE { ?c a :Chunk ; f:matchesText (:text "renewal" 1) }')
    assert _keys(rows) == ["b"]


def test_a_match_joins_with_the_rest_of_the_pattern(db: Any) -> None:
    rows = _ask(
        db,
        'SELECT ?c ?acct WHERE { ?c a :Chunk ; :account ?acct ; f:matchesText (:text "price") . FILTER(?acct = "globex") }',
    )
    assert _keys(rows) == ["c"]
