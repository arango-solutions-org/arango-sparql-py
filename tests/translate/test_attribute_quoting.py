"""Attribute names that are not AQL identifiers are quoted — or refused.

Regression (prod.demo IAM, 2026-10-08): NL generated
``?document :phys:typeValue ?t``. The property is undeclared, so the resolver
fell back to the local name ``phys:typeValue`` as the document attribute and
the visitor spliced it in bare — ``doc1.phys:typeValue`` — which ArangoDB
rejects (ERR 1501). The hard rule is never to emit plausible-but-wrong AQL.

Now (``translate/builder.py::attribute_ref``, mirroring arango-cypher-py's
backtick rule): an identifier stays bare, anything else is backtick-quoted, a
name AQL cannot quote (backtick / backslash) raises ``UnsupportedSparqlError``,
and ``HAS()`` receives a non-identifier name as a bind variable.
"""

from __future__ import annotations

import pytest

from arango_sparql.api import translate
from arango_sparql.errors import AqlEmitError, UnsupportedSparqlError
from arango_sparql.translate.builder import (
    AQL_RESERVED_NAMES,
    attribute_ref,
    is_aql_identifier,
    object_key,
)
from arango_sparql.translate.resolver import SchemaResolver

_TTL = """
@prefix :     <http://example.org/> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .

:Doc a owl:Class ;
  phys:mappingStyle "COLLECTION" ;
  phys:collectionName "Docs" .
"""

_PREFIX = "PREFIX : <http://example.org/>\n"


def _translate(body: str):
    return translate(_PREFIX + body, resolver=SchemaResolver.from_turtle(_TTL))


def test_identifier_names_stay_bare() -> None:
    assert attribute_ref("doc1", "name") == "doc1.name"
    assert attribute_ref("doc1", "_key") == "doc1._key"


@pytest.mark.parametrize("name", ["_a2", "__x", "a_2", "_from"])
def test_underscore_then_letter_stays_bare(name: str) -> None:
    assert is_aql_identifier(name)
    assert attribute_ref("doc1", name) == f"doc1.{name}"


@pytest.mark.parametrize("name", ["_2", "_", "__", "_2a"])
def test_underscore_not_followed_by_a_letter_is_quoted(name: str) -> None:
    # ArangoDB's lexer rejects these bare (ERR 1501): a leading underscore must
    # be followed by a letter. Found by differential testing against
    # pyoxigraph on W3C entailment/rdfs12, whose rdf:_2 became ``doc1._2``.
    assert not is_aql_identifier(name)
    assert attribute_ref("doc1", name) == f"doc1.`{name}`"


def test_rdf_container_membership_property_emits_valid_aql() -> None:
    res = _translate("SELECT ?f WHERE { ?d a :Doc . ?d :_2 ?f . }")
    assert "doc1.`_2`" in res.aql
    assert "doc1._2" not in res.aql


@pytest.mark.parametrize("name", ["phys:typeValue", "first name", "é", "a-b", "1st"])
def test_non_identifier_names_are_backtick_quoted(name: str) -> None:
    assert not is_aql_identifier(name)
    assert attribute_ref("doc1", name) == f"doc1.`{name}`"


@pytest.mark.parametrize("name", ["a`b", "a\\b", ""])
def test_names_aql_cannot_quote_are_refused(name: str) -> None:
    with pytest.raises(UnsupportedSparqlError):
        attribute_ref("doc1", name)


def test_the_prod_demo_query_emits_valid_aql() -> None:
    res = _translate("SELECT DISTINCT ?t WHERE { ?d a :Doc . ?d :phys:typeValue ?t . }")
    assert "doc1.`phys:typeValue`" in res.aql
    assert "doc1.phys:typeValue" not in res.aql.replace("`phys:typeValue`", "")
    # The HAS() existence check takes the name as a bind variable, not text.
    assert "HAS(doc1, @" in res.aql
    assert "phys:typeValue" in res.bind_vars.values()
    assert any(w.get("code") == "W_SCHEMA_UNMAPPED_IRI" for w in res.warnings)


def test_a_constant_object_on_a_non_identifier_property_no_longer_crashes() -> None:
    # The bind-variable hint used to be the attribute name, and a non-
    # identifier hint raised ValueError -> an unhandled 500.
    res = _translate('SELECT ?d WHERE { ?d a :Doc . ?d :phys:typeValue "x" . }')
    assert "doc1.`phys:typeValue`" in res.aql
    assert "x" in res.bind_vars.values()


def test_identifier_attribute_output_is_unchanged() -> None:
    res = _translate("SELECT ?n WHERE { ?d a :Doc . ?d :file_name ?n . }")
    assert 'HAS(doc1, "file_name")' in res.aql
    assert "doc1.file_name" in res.aql


# ---------------------------------------------------------------------------
# AQL keywords (issue #76) and object keys (issue #75)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["filter", "FILTER", "Desc", "graph", "in", "null", "k_shortest_paths", "with"]
)
def test_aql_keywords_are_quoted_as_attributes(name: str) -> None:
    # ArangoDB rejects a keyword bare after a dot, case-insensitively (ERR 1501).
    assert not is_aql_identifier(name)
    assert attribute_ref("doc1", name) == f"doc1.`{name}`"


@pytest.mark.parametrize(
    "name", ["count", "keep", "options", "search", "prune", "to", "new", "old", "current"]
)
def test_aql_context_words_stay_bare(name: str) -> None:
    # Context words parse bare (``doc.count``), so they keep their spelling.
    assert is_aql_identifier(name)
    assert name not in AQL_RESERVED_NAMES
    assert attribute_ref("doc1", name) == f"doc1.{name}"


def test_reserved_names_are_lower_case_and_lexer_shaped() -> None:
    # Lookups lower-case the name, so every entry must be lower case to match.
    assert all(w == w.lower() and w.replace("_", "").isalpha() for w in AQL_RESERVED_NAMES)
    assert len(AQL_RESERVED_NAMES) == 37


@pytest.mark.parametrize(
    "name, key",
    [
        ("name", "name"),
        ("_n", "_n"),
        ("count", "count"),
        ("_2", "`_2`"),
        ("_", "`_`"),
        ("2", "`2`"),
        ("desc", "`desc`"),
        ("FILTER", "`FILTER`"),
    ],
)
def test_object_key_quotes_only_what_arangodb_rejects_bare(name: str, key: str) -> None:
    assert object_key(name) == key


@pytest.mark.parametrize("name", ["", "a`b", "a\\b"])
def test_object_key_refuses_names_aql_cannot_quote(name: str) -> None:
    with pytest.raises(AqlEmitError, match="invalid RETURN key"):
        object_key(name)


@pytest.mark.parametrize("name", ["_2", "desc", "2"])
def test_return_object_quotes_keys(name: str) -> None:
    res = _translate(f"SELECT ?{name} WHERE {{ ?{name} a :Doc }}")
    assert f"RETURN {{ `{name}`: " in res.aql
