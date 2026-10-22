"""Issues #75 and #76 — names ArangoDB cannot take bare produce valid AQL.

A SPARQL variable or attribute name is written into AQL as an object key
(``RETURN { name: … }``) or an attribute read (``doc.name``). ArangoDB rejects
a bare name whose leading underscore is not followed by a letter (``_2``,
``_``; #75) or that is an AQL keyword (``filter``, ``desc``, ``graph``; #76),
with ERR 1501. The translator now backtick-quotes those.

Live because the rule is the server's: the keyword list in
``translate/builder.py`` (and the object-key subset in
``tests/helpers/aql_interp.py``) is re-validated here against the running
ArangoDB, so a server that reserves a new word fails this test instead of
breaking queries. Every query also runs through pyoxigraph on the same data —
the W3C ground truth — and the answers must agree. Gated like the rest of the
integration suite (``RUN_INTEGRATION=1``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from arango_sparql.api import translate
from arango_sparql.translate.builder import AQL_RESERVED_NAMES, attribute_ref, object_key
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.aql_interp import AQL_RESERVED_OBJECT_KEYS
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

_DOCS_COLL = "AqlNamesDocs"
_TAGS_COLL = "AqlNamesTags"
_FACTS_COLL = "AqlNamesFacts"

# Words ArangoDB's grammar knows that stay usable bare (context words). Checked
# below so a server that starts reserving one is caught.
_CONTEXT_WORDS = ("search", "keep", "count", "options", "prune", "to", "current", "new", "old", "at", "least")

_TTL = f"""
@prefix :     <http://example.org/> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
:Doc a owl:Class ; phys:mappingStyle "COLLECTION" ; phys:collectionName "{_DOCS_COLL}" .
:Tag a owl:Class ; phys:mappingStyle "COLLECTION" ; phys:collectionName "{_TAGS_COLL}" .
:Fact a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "{_FACTS_COLL}" ;
  phys:subjectColumn "subject" ; phys:predicateColumn "graph" ;
  phys:objectUriColumn "in" ; phys:objectValueColumn "_2" .
"""

_EX = "http://example.org/"
_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

# Field names chosen to be every kind of name ArangoDB refuses bare, plus one
# context word (``count``) that must stay bare.
_DOCS: list[dict[str, Any]] = [
    {"_uri": f"{_EX}a", "_2": "second-a", "filter": 3, "desc": "alpha", "graph": "g1", "in": 5, "count": 10},
    {"_uri": f"{_EX}b", "_2": "second-b", "filter": 1, "desc": "beta", "graph": "g1", "count": 20},
    {"_uri": f"{_EX}c", "filter": 2, "graph": "g2", "in": 5},
]
_TAGS: list[dict[str, Any]] = [{"_uri": f"{_EX}t1"}, {"_uri": f"{_EX}t2"}]
_FACTS: list[dict[str, Any]] = [
    {"subject": f"{_EX}f1", "graph": _RDF_TYPE, "in": f"{_EX}Fact"},
    {"subject": f"{_EX}f2", "graph": _RDF_TYPE, "in": f"{_EX}Fact"},
    {"subject": f"{_EX}f1", "graph": f"{_EX}label", "_2": "one"},
    {"subject": f"{_EX}f2", "graph": f"{_EX}label", "_2": "two"},
]

_PREFIX = "PREFIX : <http://example.org/>\n"


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
    for name, docs in ((_DOCS_COLL, _DOCS), (_TAGS_COLL, _TAGS), (_FACTS_COLL, _FACTS)):
        if handle.has_collection(name):
            handle.delete_collection(name)
        handle.create_collection(name).insert_many(docs)
    yield handle
    for name in (_DOCS_COLL, _TAGS_COLL, _FACTS_COLL):
        handle.delete_collection(name)


@pytest.fixture(scope="module")
def oxi_store() -> Any:
    """The same data as RDF; an absent field is no triple."""
    lines = []
    for d in _DOCS:
        s = f"<{d['_uri']}>"
        lines.append(f"{s} a <{_EX}Doc> .")
        for attr, value in d.items():
            if attr != "_uri":
                lines.append(f"{s} <{_EX}{attr}> {json.dumps(value)} .")
    lines += [f"<{t['_uri']}> a <{_EX}Tag> ." for t in _TAGS]
    lines += [
        f'<{_EX}f1> a <{_EX}Fact> ; <{_EX}label> "one" .',
        f'<{_EX}f2> a <{_EX}Fact> ; <{_EX}label> "two" .',
    ]
    store = oxi.Store()
    store.load("\n".join(lines).encode("utf-8"), oxi.RdfFormat.TURTLE)
    return store


def _parses(db: Any, aql: str) -> bool:
    from arango.exceptions import AQLQueryValidateError

    try:
        db.aql.validate(aql)
    except AQLQueryValidateError as exc:
        assert exc.error_code == 1501, f"unexpected error {exc.error_code} validating {aql!r}"
        return False
    return True


# ---------------------------------------------------------------------------
# The keyword lists are the server's
# ---------------------------------------------------------------------------


def test_every_reserved_name_is_rejected_bare_and_accepted_quoted(db: Any) -> None:
    for word in sorted(AQL_RESERVED_NAMES):
        for spelling in (word, word.upper()):
            assert not _parses(db, f"LET doc = {{}} RETURN doc.{spelling}"), (
                f"{spelling} parses bare after a dot"
            )
            assert _parses(db, f"LET doc = {{}} RETURN {attribute_ref('doc', spelling)}")
            assert _parses(db, f"RETURN {{{object_key(spelling)}: 1}}")


def test_object_key_subset_matches_the_server(db: Any) -> None:
    assert AQL_RESERVED_OBJECT_KEYS <= AQL_RESERVED_NAMES
    for word in sorted(AQL_RESERVED_NAMES):
        rejected = not _parses(db, f"RETURN {{{word}: 1}}")
        assert rejected == (word in AQL_RESERVED_OBJECT_KEYS), (
            f"object key {word!r}: server rejects={rejected}"
        )


def test_context_words_stay_usable_bare(db: Any) -> None:
    # If a new ArangoDB reserves one of these, it must join AQL_RESERVED_NAMES.
    for word in _CONTEXT_WORDS:
        assert _parses(db, f"LET doc = {{}} RETURN doc.{word}"), f"{word} is now reserved after a dot"
        assert attribute_ref("doc", word) == f"doc.{word}"


@pytest.mark.parametrize("name", ["_2", "_", "__", "_2a", "2"])
def test_lexer_shaped_names_are_quoted_where_arangodb_needs_it(db: Any, name: str) -> None:
    assert not _parses(db, f"LET doc = {{}} RETURN doc.{name}")
    assert _parses(db, f"LET doc = {{}} RETURN {attribute_ref('doc', name)}")
    assert not _parses(db, f"RETURN {{{name}: 1}}")
    assert _parses(db, f"RETURN {{{object_key(name)}: 1}}")


def test_every_golden_parses(db: Any) -> None:
    corpus = yaml.safe_load(
        (Path(__file__).parents[1] / "translate" / "aql_names.yml").read_text(encoding="utf-8")
    )
    for case in corpus["cases"]:
        aql = case["expected_aql"]
        assert _parses(db, aql), f"golden {case['name']} does not parse:\n{aql}"


# ---------------------------------------------------------------------------
# Real execution agrees with pyoxigraph
# ---------------------------------------------------------------------------


def _multiset(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(json.dumps(r, sort_keys=True) for r in rows)


def _arango(db: Any, body: str) -> list[dict[str, Any]]:
    res = translate(_PREFIX + body, resolver=SchemaResolver.from_turtle(_TTL))
    assert _parses(db, res.aql), f"AQL does not parse:\n{res.aql}"
    return [drop_null_bindings(r) for r in db.aql.execute(res.aql, bind_vars=res.bind_vars)]


def _oxigraph(store: Any, body: str) -> list[dict[str, Any]]:
    return [normalize_oxi_row(r) for r in oxi_bindings(store, _PREFIX + body)]


_SHAPES = [
    # #75 — variables ArangoDB cannot take bare
    "SELECT ?_2 WHERE { ?_2 a :Doc }",
    "SELECT ?_ WHERE { ?_ a :Doc }",
    "SELECT ?2 WHERE { ?2 a :Doc }",
    "SELECT ?_2 WHERE { ?d a :Doc . BIND(1 AS ?_2) }",
    "SELECT (COUNT(?d) AS ?_2) WHERE { ?d a :Doc }",
    "SELECT ?_2 WHERE { { SELECT ?_2 WHERE { ?_2 a :Doc } ORDER BY ?_2 LIMIT 2 } }",
    "SELECT ?_2 WHERE { { ?_2 a :Doc } UNION { ?_2 a :Tag } }",
    "SELECT ?_2 WHERE { VALUES ?_2 { 1 2 } }",
    "SELECT ?d ?_2 WHERE { ?d a :Doc OPTIONAL { ?d :_2 ?_2 } }",
    # #76 — keywords as attributes and variables
    "SELECT ?desc WHERE { ?desc a :Doc }",
    "SELECT ?d ?f WHERE { ?d a :Doc ; :filter ?f }",
    "SELECT ?d ?filter WHERE { ?d a :Doc ; :filter ?filter }",
    "SELECT ?d WHERE { ?d a :Doc ; :in 5 }",
    "SELECT ?d WHERE { ?d a :Doc ; :filter ?f FILTER(?f > 1) }",
    "SELECT ?d ?g WHERE { ?d a :Doc OPTIONAL { ?d :desc ?g } }",
    "SELECT ?graph (COUNT(?d) AS ?_n) WHERE { ?d a :Doc ; :graph ?graph } GROUP BY ?graph",
    "SELECT DISTINCT ?graph WHERE { ?d a :Doc ; :graph ?graph }",
    "SELECT ?desc WHERE { { SELECT ?desc WHERE { ?d a :Doc ; :desc ?desc } } }",
    "SELECT ?in WHERE { { ?d a :Doc ; :in ?in } UNION { ?d a :Doc ; :filter ?in } }",
    "SELECT ?d WHERE { ?d a :Doc MINUS { ?d a :Doc ; :desc ?t } }",
    # A context word stays bare and still reads the field
    "SELECT ?d ?count WHERE { ?d a :Doc ; :count ?count }",
    # RPT mapping columns named graph / in / _2
    "SELECT ?s ?v WHERE { ?s a :Fact ; :label ?v }",
]


@pytest.mark.parametrize("body", _SHAPES)
def test_matches_oxigraph(db: Any, oxi_store: Any, body: str) -> None:
    assert _multiset(_arango(db, body)) == _multiset(_oxigraph(oxi_store, body))


def test_order_by_a_keyword_attribute_matches_oxigraph(db: Any, oxi_store: Any) -> None:
    body = "SELECT ?d ?f WHERE { ?d a :Doc ; :filter ?f } ORDER BY DESC(?f)"
    assert _arango(db, body) == _oxigraph(oxi_store, body)
