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


def test_an_ontology_survey_reports_nothing_on_any_translation() -> None:
    # Routing an untyped subject asks whether the mapping is a triple store,
    # resolving every class; class A's advisory concerns a class the query
    # never names, and was reported on a resolver's first translation only
    # (review of #161). It is not this translation's on any of them.
    ttl = """
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix phys: <https://arango.solutions/phys#> .
    <http://ex.org/A> a owl:Class .
    <http://ex.org/B> a owl:Class ; phys:collectionName "B" .
    <http://ex.org/name> a owl:DatatypeProperty .
    """
    query = "SELECT ?o WHERE { ?s a <http://ex.org/B> . <http://x/s> <http://ex.org/name> ?o }"
    resolver = SchemaResolver.from_turtle(ttl)
    assert [_codes(translate(query, resolver=resolver)) for _ in range(3)] == [[], [], []]
    # A query naming A still reports it, though the survey resolved A first.
    named = translate("SELECT ?s WHERE { ?s a <http://ex.org/A> }", resolver=resolver)
    assert _codes(named) == [("W_SCHEMA_DEFAULT_COLLECTION", "http://ex.org/A")]


def test_the_nl_repair_loop_accepts_a_clean_candidate_after_a_bad_one() -> None:
    # The adapter's misprefixed-term check reads ``result.warnings``: on a
    # reused resolver every candidate after a bad one was refused, so the
    # repair loop could never recover (#157).
    from arango_sparql.nl2sparql.engine_adapter import SparqlAdapter

    ttl = """
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix phys: <https://arango.solutions/phys#> .
    <http://ex.org/Person> a owl:Class ; phys:collectionName "Person" .
    """
    adapter = SparqlAdapter(resolver=SchemaResolver.from_turtle(ttl))
    bad = "PREFIX : <http://ex.org/> SELECT ?s ?v WHERE { ?s a :Person ; <http://ex.org/phys:typeValue> ?v }"
    good = "PREFIX : <http://ex.org/> SELECT ?s WHERE { ?s a :Person }"
    assert [adapter.validate(q).ok for q in (bad, good, bad)] == [False, True, False]


_PERSON_BUNDLE = MappingBundle(
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


def _nl_resolver(monkeypatch: pytest.MonkeyPatch, session: Any) -> Any:
    """The resolver /nl-translate gives the real pipeline for *session*."""
    from arango_sparql.nl2sparql import NlPipeline
    from arango_sparql.service import app
    from arango_sparql.service.routes import nl as nl_routes

    seen: dict[str, Any] = {}

    def run(self: NlPipeline, nl: str) -> Any:
        seen["resolver"] = self.resolver
        raise RuntimeError("stop before the model is asked")

    monkeypatch.setattr(NlPipeline, "run", run)
    app.dependency_overrides[nl_routes._get_optional_session] = lambda: session
    app.dependency_overrides[nl_routes._llm_client_factory] = lambda: object()
    try:
        TestClient(app, raise_server_exceptions=False).post(
            "/nl-translate", json={"nl": "people?", "ontology_ttl": _TTL}
        )
    finally:
        app.dependency_overrides.clear()
    return seen["resolver"]


def test_the_nl_routes_give_the_pipeline_the_sessions_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    # /nl-translate built its resolver without the session's analyzer bundle,
    # so it had neither its physical mapping nor its indexes (#157).
    from arango_sparql.service.routes import sparql as sparql_routes

    monkeypatch.setattr(sparql_routes, "_analyzer_bundle_for_session", lambda session: _PERSON_BUNDLE)
    resolver = _nl_resolver(monkeypatch, object())
    assert resolver.uri_indexes is not None
    assert resolver.uri_indexes.carrying == frozenset({"people"})


def test_a_pinned_project_mapping_still_wins_on_the_nl_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    # Under a governance control plane the session's schema is the project's
    # mapping, never an analysis of the database (PRD §8.7).
    from types import SimpleNamespace

    from arango_sparql.service.routes import schema as schema_routes

    def no_analysis(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a pinned session must not read the database's analysis")

    monkeypatch.setattr(schema_routes, "_read_or_warm", no_analysis)
    session = SimpleNamespace(project_mapping=_PERSON_BUNDLE, db=object(), graph_name=None)
    resolver = _nl_resolver(monkeypatch, session)
    assert resolver.uri_indexes is not None
    assert resolver.uri_indexes.carrying == frozenset({"people"})
