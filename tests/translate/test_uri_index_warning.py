"""A union read warns when the copy check scans a member with no ``_uri`` index.

#146 (issue #124) drops a union row that repeats an earlier member's copy of
the same IRI, found by an ``_uri`` lookup in that member for each later
member's row that has a ``_uri``. Without a usable index on the earlier
member's ``_uri`` the lookup scans it once per row, which on a large
collection times out instead of answering. Nothing in the database tells the
user why, so the translator reports it (``W_SCHEMA_URI_INDEX_MISSING``) when
a schema analysis that records indexes shows that case (review of #154).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from arango_sparql.api import translate
from arango_sparql.service import app
from arango_sparql.service.mapping import _resolver_from_request
from arango_sparql.translate.csi import mapping_bundle_from_csi
from arango_sparql.translate.mapping import MappingBundle, MappingSource
from arango_sparql.translate.resolver import SchemaResolver, UriIndexes, analyze_uri_indexes

_C = "urn:arango-sparql:concept#"
_URI_PROPERTY = {"_uri": {"field": "_uri"}, "name": {"field": "name"}}
_NAME_ONLY = {"name": {"field": "name"}}
_URI_INDEX = {"type": "persistent", "fields": ["_uri"]}
_CODE = "W_SCHEMA_URI_INDEX_MISSING"


def _member(
    collection: str, *, uri: bool = True, indexes: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    entity: dict[str, Any] = {
        "style": "COLLECTION",
        "collectionName": collection,
        "properties": _URI_PROPERTY if uri else _NAME_ONLY,
    }
    if indexes is not None:
        entity["indexes"] = indexes
    return entity


def _bundle(
    comment: dict[str, Any] | None = None,
    post: dict[str, Any] | None = None,
    *,
    kind: str = "analyzer",
) -> MappingBundle:
    # Comment sorts before Post, so a Post row is checked against Comment.
    return MappingBundle(
        conceptual_schema={"entities": [], "relationships": []},
        physical_mapping={
            "entities": {
                "Comment": comment or _member("Comment"),
                "Post": post or _member("Post"),
                "Message": {"style": "UNION", "members": ["Post", "Comment"]},
            },
            "relationships": {},
        },
        source=MappingSource(kind=kind),  # type: ignore[arg-type]
    )


def _warned(bundle: MappingBundle, query: str = f"SELECT ?m WHERE {{ ?m a <{_C}Message> }}") -> set[str]:
    result = translate(query, resolver=SchemaResolver.from_mapping_bundle(bundle))
    return {w["iri"] for w in result.schema_warnings if w.get("code") == _CODE}


@pytest.mark.parametrize(
    ("index", "warned"),
    [
        (None, {"Comment"}),
        (_URI_INDEX, set()),
        ({"type": "hash", "fields": ["_uri"]}, set()),
        ({"type": "skiplist", "fields": ["_uri"]}, set()),
        # The check filters ``_uri != null`` in the lookup, so a sparse index
        # serves it (the lookup scanned 25M documents without that filter).
        ({"type": "persistent", "fields": ["_uri"], "sparse": True}, set()),
        ({"type": "persistent", "fields": ["_uri", "name"], "unique": True}, set()),
        # Not usable for ``other._uri == x``: _uri is not the first field, or
        # the index type serves no plain equality FILTER.
        ({"type": "persistent", "fields": ["name", "_uri"]}, {"Comment"}),
        ({"type": "inverted", "fields": ["_uri"]}, {"Comment"}),
    ],
)
def test_the_earlier_member_is_warned_about_unless_an_index_serves_the_lookup(
    index: dict[str, Any] | None, warned: set[str]
) -> None:
    comment = _member("Comment", indexes=[index] if index else None)
    assert _warned(_bundle(comment)) == warned


def test_it_is_the_earlier_member_that_is_scanned_whatever_its_own_documents_hold() -> None:
    # Comment holds no ``_uri`` and has no index; Post's rows have one, so the
    # check scans Comment for each of them (3.6 s on 2 x 5k, review of #154).
    assert _warned(_bundle(_member("Comment", uri=False))) == {"Comment"}


def test_no_warning_when_no_later_member_holds_uri() -> None:
    # Only a later row with ``_uri`` starts the lookup; Post has none.
    assert _warned(_bundle(post=_member("Post", uri=False))) == set()


def test_the_last_member_is_never_looked_in() -> None:
    assert "Post" not in _warned(_bundle())


def test_a_read_with_no_copy_check_is_not_warned_about() -> None:
    # One member's class: no union, no lookup.
    result = translate(
        f"SELECT ?m WHERE {{ ?m a <{_C}Post> }}", resolver=SchemaResolver.from_mapping_bundle(_bundle())
    )
    assert "repeated" not in result.aql
    assert not any(w.get("code") == _CODE for w in result.schema_warnings)


def test_the_lookup_filters_a_non_null_uri_for_a_sparse_index() -> None:
    aql = translate(
        f"SELECT ?m WHERE {{ ?m a <{_C}Message> }}", resolver=SchemaResolver.from_mapping_bundle(_bundle())
    ).aql
    assert "FILTER doc4._uri != null\n  FILTER doc4._uri == doc3._uri\n" in aql


def test_labels_on_one_collection_share_its_indexes() -> None:
    # The analyzer writes the collection's indexes on each label entity; one
    # label listing the index is enough.
    bundle = MappingBundle(
        physical_mapping={
            "entities": {
                "A": {
                    "style": "LABEL",
                    "collectionName": "nodes",
                    "properties": _URI_PROPERTY,
                    "indexes": [_URI_INDEX],
                },
                "B": {"style": "LABEL", "collectionName": "nodes", "properties": _URI_PROPERTY},
            }
        },
        source=MappingSource(kind="analyzer"),
    )
    assert analyze_uri_indexes(bundle) == UriIndexes(
        collections=frozenset({"nodes"}), carrying=frozenset({"nodes"}), indexed=frozenset({"nodes"})
    )


@pytest.mark.parametrize("entities", [[], {"E": "x", "F": {"collectionName": 3}}])
def test_a_malformed_mapping_describes_nothing(entities: Any) -> None:
    bundle = MappingBundle(physical_mapping={"entities": entities}, source=MappingSource(kind="analyzer"))
    assert analyze_uri_indexes(bundle) == UriIndexes()


@pytest.mark.parametrize("kind", ["manual", "heuristic", "imported_owl", "imported_csi"])
def test_a_mapping_that_records_no_indexes_warns_nothing(kind: str) -> None:
    # A missing ``indexes`` key means "none" only in a schema analysis; a
    # hand-written or imported mapping does not list them (review of #154).
    assert analyze_uri_indexes(_bundle(kind=kind)) is None
    assert _warned(_bundle(kind=kind)) == set()


def test_a_hand_written_ontology_warns_nothing() -> None:
    ttl = f"""
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix phys: <https://arango.solutions/phys#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    <{_C}Post> a owl:Class ; phys:collectionName "Post" ; rdfs:subClassOf <{_C}Message> .
    <{_C}Comment> a owl:Class ; phys:collectionName "Comment" ; rdfs:subClassOf <{_C}Message> .
    <{_C}Message> a owl:Class ; phys:mappingStyle "UNION" .
    """
    result = translate(f"SELECT ?m WHERE {{ ?m a <{_C}Message> }}", resolver=SchemaResolver.from_turtle(ttl))
    assert "repeated" in result.aql
    assert not any(w.get("code") == _CODE for w in result.schema_warnings)


def _csi(direction: str, producer: str = "arango-schema-analyzer") -> dict[str, Any]:
    return {
        "csiVersion": "1",
        "conceptualModel": {"entities": [], "relationships": []},
        "arangoPhysicalMapping": {"entities": {"Comment": _member("Comment"), "Post": _member("Post")}},
        "provenance": {"producer": producer, "direction": direction},
    }


def test_a_csi_the_analyzer_wrote_in_reverse_records_indexes() -> None:
    assert analyze_uri_indexes(mapping_bundle_from_csi(_csi("reverse"))) is not None
    assert analyze_uri_indexes(mapping_bundle_from_csi(_csi("forward"))) is None
    assert analyze_uri_indexes(mapping_bundle_from_csi(_csi("reverse", producer="r2g"))) is None


def test_the_service_reads_the_indexes_from_the_analyzer_bundle() -> None:
    req = SimpleNamespace(ontology_ttl=None, mapping=None)
    assert _resolver_from_request(req, analyzer_bundle=_bundle()).uri_indexes == analyze_uri_indexes(
        _bundle()
    )
    assert _resolver_from_request(req).uri_indexes is None


def test_translate_returns_the_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    # The session's analyzer bundle supplies the indexes; the inline ontology
    # the classes (the route merges the two).
    monkeypatch.setattr(
        "arango_sparql.service.routes.sparql._analyzer_bundle_for_session", lambda _session: _bundle()
    )
    ttl = f"""
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix phys: <https://arango.solutions/phys#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    <{_C}Post> a owl:Class ; phys:collectionName "Post" ; rdfs:subClassOf <{_C}Message> .
    <{_C}Comment> a owl:Class ; phys:collectionName "Comment" ; rdfs:subClassOf <{_C}Message> .
    <{_C}Message> a owl:Class ; phys:mappingStyle "UNION" .
    """
    resp = TestClient(app).post(
        "/translate", json={"sparql": f"SELECT ?m WHERE {{ ?m a <{_C}Message> }}", "ontology_ttl": ttl}
    )
    assert resp.status_code == 200, resp.text
    warned = [w for w in resp.json()["schema_warnings"] if w["code"] == _CODE]
    assert [w["collection"] for w in warned] == ["Comment"]
    assert "/schema/introspect?force=true" in warned[0]["message"]
