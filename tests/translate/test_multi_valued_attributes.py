"""Where the multi-valued marker comes from, and what it refuses (CDF #220).

A mapping marks an attribute that holds a list: CSI 1.2 ``multiValued`` on a
property mapping, or ``phys:multiValued`` on an OWL datatype property. The
translation goldens are ``multi_valued.yml``; the answers are checked against
pyoxigraph in ``tests/cross/test_multi_valued_attribute_cross.py``.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from arango_sparql.errors import SchemaResolutionError
from arango_sparql.translate.csi import mapping_bundle_from_csi
from arango_sparql.translate.mapping import MappingError
from arango_sparql.translate.resolver import SchemaResolver

NS = "urn:arango-sparql:concept#"


def _csi(version: str = "1.2", **questions: Any) -> dict[str, Any]:
    return {
        "csiVersion": version,
        "conceptualModel": {"entities": [{"name": "Document"}], "relationships": []},
        "arangoPhysicalMapping": {
            "entities": {
                "Document": {
                    "style": "COLLECTION",
                    "collectionName": "documents",
                    "properties": {
                        "questionsServed": {"field": "questions_served", **questions},
                        "role": {"field": "role"},
                    },
                }
            },
            "relationships": {},
        },
        "provenance": {"producer": "test", "direction": "reverse", "source": {"kind": "arango"}},
    }


def _resolver(csi: dict[str, Any], **kwargs: Any) -> SchemaResolver:
    resolver = SchemaResolver.from_mapping_bundle(mapping_bundle_from_csi(csi))
    for key, value in kwargs.items():
        setattr(resolver, key, value)
    return resolver


def test_csi_1_2_marks_one_attribute():
    resolver = _resolver(_csi(multiValued=True))
    questions = resolver.resolve_property(NS + "questionsServed")
    role = resolver.resolve_property(NS + "role")
    assert questions.multi_valued is True and questions.attribute == "questions_served"
    assert role.multi_valued is False
    assert resolver.fans_out(questions) and not resolver.fans_out(role)
    assert resolver.multi_valued_attributes() == ("questions_served",)


def test_multi_valued_false_is_one_value():
    resolver = _resolver(_csi(multiValued=False))
    assert resolver.resolve_property(NS + "questionsServed").multi_valued is False
    assert resolver.multi_valued_attributes() == ()


def test_the_global_flag_still_fans_out_every_attribute():
    resolver = _resolver(_csi(), fan_out_list_values=True)
    assert resolver.fans_out(resolver.resolve_property(NS + "role"))
    # Variable predicates fan every attribute out themselves; nothing to list.
    assert resolver.multi_valued_attributes() == ()


@pytest.mark.parametrize("version", ["1", "1.1"])
def test_multi_valued_needs_csi_1_2(version):
    # An older reader would ignore the key and bind the whole list as one value.
    with pytest.raises(MappingError, match="needs csiVersion '1.2'"):
        mapping_bundle_from_csi(_csi(version, multiValued=True))


@pytest.mark.parametrize("bad", ["true", 1, None])
def test_multi_valued_must_be_a_boolean(bad):
    with pytest.raises(MappingError, match="multiValued"):
        mapping_bundle_from_csi(_csi(multiValued=bad))


def test_csi_1_2_without_the_key_reads_as_before():
    csi = _csi()
    before = copy.deepcopy(csi)
    resolver = _resolver(csi)
    assert csi == before
    assert resolver.resolve_property(NS + "questionsServed").multi_valued is False


OWL = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <http://arangodb.com/schema/physical#> .
:Document a owl:Class ; phys:collectionName "documents" .
:tags a owl:DatatypeProperty ; phys:multiValued "{flag}" .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; phys:multiValued "true" .
"""


@pytest.mark.parametrize("flag, expected", [("true", True), ("TRUE", True), ("false", False)])
def test_owl_phys_multi_valued(flag, expected):
    resolver = SchemaResolver.from_turtle(OWL.replace("{flag}", flag))
    assert resolver.resolve_property("http://ex.org/tags").multi_valued is expected
    # An edge is walked, not fanned out: the annotation means nothing there.
    knows = resolver.resolve_property("http://ex.org/knows")
    assert knows.multi_valued is False and not resolver.fans_out(knows)


def test_a_malformed_phys_multi_valued_is_refused():
    resolver = SchemaResolver.from_turtle(OWL.replace("{flag}", "several"))
    with pytest.raises(SchemaResolutionError, match="phys:multiValued"):
        resolver.resolve_property("http://ex.org/tags")


def test_one_entity_holding_a_list_marks_the_shared_property():
    # The synthesized ontology has one IRI per property name; a scalar read as a
    # one-element list loses nothing, so one entity's marker is enough.
    csi = _csi(multiValued=True)
    csi["arangoPhysicalMapping"]["entities"]["Ticket"] = {
        "style": "COLLECTION",
        "collectionName": "tickets",
        "properties": {"questionsServed": {"field": "questions_served"}},
    }
    resolver = _resolver(csi)
    assert resolver.resolve_property(NS + "questionsServed").multi_valued is True
