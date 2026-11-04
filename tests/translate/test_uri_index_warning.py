"""A union read warns when a member it looks copies up in has no ``_uri`` index.

#146 (issue #124) drops a union row that repeats an earlier member's copy of
the same IRI, found by an ``_uri`` lookup in that member. Without a persistent
index on ``_uri`` the lookup scans the member once per row, which on a large
collection times out instead of answering. Nothing in the database tells the
user why, so the translator reports it (``W_SCHEMA_URI_INDEX_MISSING``)
whenever the schema analysis shows the collection carries ``_uri`` and lists
no index on it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.service.mapping import _resolver_from_request
from arango_sparql.translate.mapping import MappingBundle, MappingSource
from arango_sparql.translate.resolver import SchemaResolver, project_unindexed_uri_collections

_C = "urn:arango-sparql:concept#"
_URI_PROPERTY = {"_uri": {"field": "_uri"}, "name": {"field": "name"}}
_URI_INDEX = {"type": "persistent", "fields": ["_uri"]}


def _member(collection: str, *, indexes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    entity: dict[str, Any] = {
        "style": "COLLECTION",
        "collectionName": collection,
        "properties": _URI_PROPERTY,
    }
    if indexes is not None:
        entity["indexes"] = indexes
    return entity


def _bundle(comment_indexes: list[dict[str, Any]] | None) -> MappingBundle:
    # Comment sorts before Post, so a Post row is checked against Comment.
    return MappingBundle(
        conceptual_schema={"entities": [], "relationships": []},
        physical_mapping={
            "entities": {
                "Comment": _member("Comment", indexes=comment_indexes),
                "Post": _member("Post"),
                "Message": {"style": "UNION", "members": ["Post", "Comment"]},
            },
            "relationships": {},
        },
        source=MappingSource(kind="analyzer"),
    )


def _codes(result: Any) -> set[tuple[str, str]]:
    return {(w.get("code"), w.get("iri")) for w in result.schema_warnings}


@pytest.mark.parametrize(
    ("entity", "expected"),
    [
        (_member("c"), {"c"}),
        (_member("c", indexes=[_URI_INDEX]), set()),
        (_member("c", indexes=[{"type": "hash", "fields": ["_uri"]}]), set()),
        (_member("c", indexes=[{"type": "persistent", "fields": ["_uri", "name"], "unique": True}]), set()),
        # Not usable for ``other._uri == x``: _uri is not the first field, or
        # the index type serves no plain equality FILTER.
        (_member("c", indexes=[{"type": "persistent", "fields": ["name", "_uri"]}]), {"c"}),
        (_member("c", indexes=[{"type": "inverted", "fields": ["_uri"]}]), {"c"}),
        # No ``_uri`` field mapped: nothing is looked up by it.
        ({"style": "COLLECTION", "collectionName": "c", "properties": {"name": {"field": "name"}}}, set()),
        ({"style": "COLLECTION", "collectionName": "c"}, set()),
    ],
)
def test_a_collection_is_listed_when_it_carries_uri_without_an_index(
    entity: dict[str, Any], expected: set[str]
) -> None:
    assert project_unindexed_uri_collections({"entities": {"E": entity}}) == expected


def test_labels_on_one_collection_share_its_indexes() -> None:
    # The analyzer writes the collection's indexes on each label entity; one
    # label listing the index is enough.
    entities = {
        "A": {
            "style": "LABEL",
            "collectionName": "nodes",
            "properties": _URI_PROPERTY,
            "indexes": [_URI_INDEX],
        },
        "B": {"style": "LABEL", "collectionName": "nodes", "properties": _URI_PROPERTY},
    }
    assert project_unindexed_uri_collections({"entities": entities}) == frozenset()


@pytest.mark.parametrize(
    "physical", [{}, {"entities": []}, {"entities": {"E": "x", "F": {"collectionName": 3}}}]
)
def test_a_malformed_mapping_lists_nothing(physical: dict[str, Any]) -> None:
    assert project_unindexed_uri_collections(physical) == frozenset()


def test_a_union_read_warns_about_the_member_it_scans() -> None:
    result = translate(
        f"SELECT ?m WHERE {{ ?m a <{_C}Message> }}",
        resolver=SchemaResolver.from_mapping_bundle(_bundle(None)),
    )
    assert "repeated" in result.aql
    assert ("W_SCHEMA_URI_INDEX_MISSING", "Comment") in _codes(result)
    # Post is the last member: nothing is looked up in it.
    assert ("W_SCHEMA_URI_INDEX_MISSING", "Post") not in _codes(result)


def test_an_indexed_member_is_not_warned_about() -> None:
    result = translate(
        f"SELECT ?m WHERE {{ ?m a <{_C}Message> }}",
        resolver=SchemaResolver.from_mapping_bundle(_bundle([_URI_INDEX])),
    )
    assert "repeated" in result.aql
    assert not any(code == "W_SCHEMA_URI_INDEX_MISSING" for code, _ in _codes(result))


def test_a_read_with_no_copy_check_is_not_warned_about() -> None:
    # One member's class: no union, no lookup.
    result = translate(
        f"SELECT ?m WHERE {{ ?m a <{_C}Post> }}", resolver=SchemaResolver.from_mapping_bundle(_bundle(None))
    )
    assert "repeated" not in result.aql
    assert not any(code == "W_SCHEMA_URI_INDEX_MISSING" for code, _ in _codes(result))


def test_a_mapping_without_index_information_warns_nothing() -> None:
    # A hand-written ontology records no indexes: the translator cannot tell.
    assert SchemaResolver.from_turtle("").unindexed_uri_collections == frozenset()


def test_the_service_reads_the_indexes_from_the_analyzer_bundle() -> None:
    req = SimpleNamespace(ontology_ttl=None, mapping=None)
    resolver = _resolver_from_request(req, analyzer_bundle=_bundle(None))
    assert resolver.unindexed_uri_collections == frozenset({"Comment", "Post"})
    assert _resolver_from_request(req).unindexed_uri_collections == frozenset()
