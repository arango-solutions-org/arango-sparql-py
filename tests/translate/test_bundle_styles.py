"""Issue #126 — a MappingBundle without OWL carries the LABELS, MULTI_LABEL
and UNION styles (#94).

``_synthesize_graph_from_bundle`` copied no ``labelsField`` / ``labelValue``
and turned no ``members`` into ``rdfs:subClassOf``, so those entities failed
on the bundle path. Each case here runs the bundle's translation and the
equivalent OWL mapping's on the AQL interpreter, and both must answer alike.
"""

from __future__ import annotations

from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.translate.mapping import MappingBundle, MappingSource
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.aql_interp import run_aql_subset

_C = "urn:arango-sparql:concept#"
_PHYSICAL = {
    "entities": {
        "Person": {
            "style": "LABELS",
            "collectionName": "nodes",
            "labelsField": "labels",
            "labelValue": "Person",
        },
        "Tag": {
            "style": "MULTI_LABEL",
            "collectionName": "nodes",
            "labelsField": "labels",
            "labelValue": "Tag",
        },
        "Post": {"style": "COLLECTION", "collectionName": "Post"},
        "Comment": {"style": "COLLECTION", "collectionName": "Comment"},
        "Message": {"style": "UNION", "members": ["Post", "Comment"]},
        "Content": {"style": "UNION"},
    },
    "relationships": {},
}
_CONCEPTUAL = {"entities": [{"name": "Content", "subtypes": ["Post", "Comment"]}]}
_OWL = f"""
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{_C}Person> a owl:Class ; phys:mappingStyle "LABELS" ; phys:collectionName "nodes" ;
    phys:labelsField "labels" ; phys:labelValue "Person" .
<{_C}Tag> a owl:Class ; phys:mappingStyle "MULTI_LABEL" ; phys:collectionName "nodes" ;
    phys:labelsField "labels" ; phys:labelValue "Tag" .
<{_C}Post> a owl:Class ; phys:collectionName "Post" ; rdfs:subClassOf <{_C}Message>, <{_C}Content> .
<{_C}Comment> a owl:Class ; phys:collectionName "Comment" ; rdfs:subClassOf <{_C}Message>, <{_C}Content> .
<{_C}Message> a owl:Class ; phys:mappingStyle "UNION" .
<{_C}Content> a owl:Class ; phys:mappingStyle "UNION" .
"""
_DOCS: dict[str, list[dict[str, Any]]] = {
    "nodes": [
        {"_id": "nodes/a", "_uri": "http://ex.org/a", "labels": ["Person"]},
        {"_id": "nodes/b", "_uri": "http://ex.org/b", "labels": ["Person", "Tag"]},
        {"_id": "nodes/t", "_uri": "http://ex.org/t", "labels": ["Tag"]},
    ],
    "Post": [{"_id": "Post/p1", "_uri": "http://ex.org/p1"}],
    "Comment": [{"_id": "Comment/c1", "_uri": "http://ex.org/c1"}],
}


def _rows(resolver: SchemaResolver, cls: str) -> list[str]:
    result = translate(f"SELECT ?s WHERE {{ ?s a <{_C}{cls}> }}", resolver=resolver)
    return sorted(r["s"] for r in run_aql_subset(result.aql, result.bind_vars, _DOCS))


@pytest.mark.parametrize(
    ("cls", "expected"),
    [
        ("Person", ["http://ex.org/a", "http://ex.org/b"]),
        ("Tag", ["http://ex.org/b", "http://ex.org/t"]),
        ("Message", ["http://ex.org/c1", "http://ex.org/p1"]),
        # Members from the conceptual entity's ``subtypes``.
        ("Content", ["http://ex.org/c1", "http://ex.org/p1"]),
    ],
)
def test_a_bundle_without_owl_reads_the_style_as_its_owl_mapping_does(cls: str, expected: list[str]) -> None:
    bundle = MappingBundle(
        conceptual_schema=_CONCEPTUAL, physical_mapping=_PHYSICAL, source=MappingSource(kind="manual")
    )
    from_bundle = _rows(SchemaResolver.from_mapping_bundle(bundle), cls)
    assert from_bundle == expected
    assert from_bundle == _rows(SchemaResolver.from_turtle(_OWL), cls)
