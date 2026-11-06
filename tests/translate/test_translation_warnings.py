"""A translation reports its own advisories, on a resolver reused or not (#157).

A resolver's advisories used to accumulate across translations, and an IRI
resolved from the cache raised none. So a reused resolver (a library caller,
the NL repair loop) reported every earlier query's advisories with each
result, and none of a cached IRI's: the NL loop's misprefixed-property check,
which reads ``result.warnings``, could flag a candidate for an earlier one's
mistake, or miss its own.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from arango_sparql.api import translate
from arango_sparql.partition import translate_partition
from arango_sparql.translate.mapping import MappingBundle, MappingSource
from arango_sparql.translate.resolver import SchemaResolver

_TTL = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<http://ex.org/Person> a owl:Class ; phys:collectionName "Person" .
<http://ex.org/name> a owl:DatatypeProperty ; rdfs:domain <http://ex.org/Person> .
<http://a.example/label> a owl:DatatypeProperty .
<http://b.example/label> a owl:DatatypeProperty .
"""
_UNMAPPED = "SELECT ?s ?o WHERE { ?s a <http://ex.org/Person> ; <http://ex.org/unknown> ?o }"
_MAPPED = "SELECT ?s ?o WHERE { ?s a <http://ex.org/Person> ; <http://ex.org/name> ?o }"
_VARIABLE_PREDICATE = "SELECT ?p ?o WHERE { ?s a <http://ex.org/Person> ; ?p ?o }"


def _codes(result: Any) -> list[tuple[str, str]]:
    return [(w["code"], w["iri"]) for w in result.warnings if str(w.get("code", "")).startswith("W_SCHEMA_")]


def test_a_reused_resolver_reports_only_this_translations_advisories() -> None:
    resolver = SchemaResolver.from_turtle(_TTL)
    first = translate(_UNMAPPED, resolver=resolver)
    assert ("W_SCHEMA_UNMAPPED_IRI", "http://ex.org/unknown") in _codes(first)
    assert _codes(translate(_MAPPED, resolver=resolver)) == []


def test_a_cached_resolution_reports_its_advisory_again() -> None:
    resolver = SchemaResolver.from_turtle(_TTL)
    translate(_UNMAPPED, resolver=resolver)
    # The property is resolved from the cache now.
    again = translate(_UNMAPPED, resolver=resolver)
    assert _codes(again).count(("W_SCHEMA_UNMAPPED_IRI", "http://ex.org/unknown")) == 1


def test_a_cached_attribute_map_reports_its_advisory_again() -> None:
    # Two properties share the attribute ``label``: the map built for a
    # variable predicate says so, on every translation that uses it.
    resolver = SchemaResolver.from_turtle(_TTL)
    for _ in range(2):
        codes = _codes(translate(_VARIABLE_PREDICATE, resolver=resolver))
        assert ("W_SCHEMA_AMBIGUOUS_ATTRIBUTE", "http://b.example/label") in codes


def test_the_resolver_still_accumulates_for_callers_that_read_it() -> None:
    resolver = SchemaResolver.from_turtle(_TTL)
    translate(_UNMAPPED, resolver=resolver)
    translate(_MAPPED, resolver=resolver)
    translate(_UNMAPPED, resolver=resolver)
    assert [w["code"] for w in resolver.warnings] == ["W_SCHEMA_UNMAPPED_IRI"]


def test_a_partition_reports_only_its_own_advisories() -> None:
    resolver = SchemaResolver.from_turtle(_TTL)
    translate(_UNMAPPED, resolver=resolver)
    result = translate_partition(_MAPPED, resolver=resolver, canonical_keys=["s"], source="arango")
    assert [w for w in result.warnings if w["code"].startswith("W_SCHEMA_")] == []


def test_the_nl_routes_give_the_pipeline_the_sessions_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    # /nl-translate built its resolver without the session's analyzer bundle,
    # so it had neither its physical mapping nor its indexes (#157).
    from arango_sparql.service import app
    from arango_sparql.service.routes import nl as nl_routes
    from arango_sparql.service.routes import sparql as sparql_routes

    bundle = MappingBundle(
        physical_mapping={
            "entities": {
                "Person": {
                    "style": "COLLECTION",
                    "collectionName": "people",
                    "properties": {"_uri": {"field": "_uri"}},
                }
            }
        },
        source=MappingSource(kind="analyzer"),
    )
    seen: dict[str, Any] = {}

    class _Pipeline:
        def __init__(self, **kw: Any) -> None:
            seen["resolver"] = kw["resolver"]

        def run(self, nl: str) -> Any:
            raise RuntimeError("stop after construction")

    import arango_sparql.nl2sparql as nl_pkg

    monkeypatch.setattr(sparql_routes, "_analyzer_bundle_for_session", lambda session: bundle)
    monkeypatch.setattr(nl_pkg, "NlPipeline", _Pipeline)
    app.dependency_overrides[nl_routes._get_optional_session] = lambda: object()
    app.dependency_overrides[nl_routes._llm_client_factory] = lambda: object()
    try:
        TestClient(app, raise_server_exceptions=False).post(
            "/nl-translate", json={"nl": "people?", "ontology_ttl": _TTL}
        )
    finally:
        app.dependency_overrides.clear()
    resolver = seen["resolver"]
    assert resolver.uri_indexes is not None
    assert resolver.uri_indexes.carrying == frozenset({"people"})
