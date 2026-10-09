"""The NL prompt carries the analyzer's classes AND their data properties.

The schema analyzer's OWL export (arangodb-schema-analyzer 0.14) declares no
datatype properties, so with only the OWL in the prompt the model never saw a
field name and guessed (prod.demo IAM, 2026-10-08: ``:fileName`` → 0 rows;
``:phys:typeValue`` → invalid AQL). The summary, ported from arango-cypher-py's
``_build_schema_summary``, lists each class with its properties from the
analyzer mapping. Live A/B on IAM: "List the file names of the AWS security
documents" went from 0 rows (``:fileName``) to the real file name
(``:file_name``).
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from arango_sparql.nl2sparql.prompt import PromptBuilder
from arango_sparql.nl2sparql.schema_summary import MAX_PROPS, build_schema_summary, default_namespace

NS = "http://arangodb.com/schema/hybrid#"


def _bundle(**over):
    """Shaped like the analyzer's export (conceptualSchema / physicalMapping)."""
    cs = {
        "entities": [
            {
                "name": "Document",
                "properties": [
                    {"name": "file_name", "type": "string"},
                    {"name": "content", "type": "string"},
                    {"name": "first name", "type": "string"},  # not a PN_LOCAL — omitted
                ],
            },
            {"name": "Chunk", "properties": [{"name": f"p{i}", "type": "string"} for i in range(12)]},
            {"name": "Empty", "properties": []},
        ],
        "relationships": [
            {"type": "PART_OF", "fromEntity": "Chunk", "toEntity": "Document"},
            {"type": "LINKS", "fromEntity": "Any", "toEntity": "Any"},
        ],
    }
    pm = {
        "entities": {
            "Document": {"style": "COLLECTION", "collectionName": "AWS_Docs", "properties": {}},
            "Empty": {
                "style": "COLLECTION",
                "collectionName": "Empties",
                "properties": {"x": {"field": "x"}},
            },
        }
    }
    cs.update(over.pop("cs", {}))
    return SimpleNamespace(conceptual_schema=cs, physical_mapping=pm, owl_turtle=None, **over)


def test_lists_each_class_with_its_properties_as_prefixed_names() -> None:
    s = build_schema_summary(_bundle(), namespace=NS)
    assert "  :Document — :file_name, :content" in s
    assert f"PREFIX : <{NS}>" in s


def test_caps_properties_per_class_like_the_sister_project() -> None:
    s = build_schema_summary(_bundle(), namespace=NS)
    chunk = next(line for line in s.splitlines() if line.startswith("  :Chunk"))
    assert chunk.count(":p") == MAX_PROPS


def test_falls_back_to_physical_property_names() -> None:
    s = build_schema_summary(_bundle(), namespace=NS)
    assert "  :Empty — :x" in s


def test_relationships_with_domain_and_range() -> None:
    s = build_schema_summary(_bundle(), namespace=NS)
    assert "?a :PART_OF ?b   (?a a :Chunk ; ?b a :Document)" in s
    assert "  ?a :LINKS ?b" in s  # Any → Any carries no type hint


def test_never_leaks_physical_names_or_unwritable_terms() -> None:
    s = build_schema_summary(_bundle(), namespace=NS)
    assert "AWS_Docs" not in s and "COLLECTION" not in s and "phys:" not in s
    assert "first name" not in s


def test_is_deterministic_so_the_prompt_stays_cache_stable() -> None:
    a = build_schema_summary(_bundle(), namespace=NS)
    b = build_schema_summary(_bundle(), namespace=NS)
    assert a == b
    assert a.index(":Chunk") < a.index(":Document") < a.index(":Empty")


def test_empty_bundle_renders_nothing() -> None:
    empty = SimpleNamespace(conceptual_schema={}, physical_mapping={}, owl_turtle=None)
    assert build_schema_summary(empty, namespace=NS) == ""


def test_default_namespace_from_turtle_or_sparql_prefix() -> None:
    assert default_namespace(f"@prefix : <{NS}> .\n") == NS
    assert default_namespace(None, f"PREFIX : <{NS}>") == NS
    assert default_namespace("@prefix ex: <http://e/> .") is None


def test_the_summary_reaches_the_system_prompt() -> None:
    s = build_schema_summary(_bundle(), namespace=NS)
    system = PromptBuilder(ontology_ttl="@prefix : <x> .", schema_summary=s).render_system()
    assert "## Schema summary" in system and ":file_name" in system
    assert "or listed in the schema summary" in system


def test_nl_route_feeds_the_sessions_cached_schema_into_the_prompt(monkeypatch) -> None:
    """End-to-end through /nl-translate: the session's cached analyzer bundle
    becomes the prompt's schema summary (cache-only — never waits)."""
    from arango_sparql.service import app
    from arango_sparql.service.routes import nl as nl_routes
    from arango_sparql.service.routes import sparql as sparql_routes

    seen: dict[str, str] = {}

    class _Pipeline:
        def __init__(self, **kw):
            seen["summary"] = kw.get("schema_summary", "")

        def run(self, nl):
            raise RuntimeError("stop after construction")

    monkeypatch.setattr(sparql_routes, "_analyzer_bundle_for_session", lambda session: _bundle())
    app.dependency_overrides[nl_routes._get_optional_session] = lambda: object()
    app.dependency_overrides[nl_routes._llm_client_factory] = lambda: object()
    import arango_sparql.nl2sparql as nl_pkg

    monkeypatch.setattr(nl_pkg, "NlPipeline", _Pipeline)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        client.post("/nl-translate", json={"nl": "file names?", "ontology_ttl": f"@prefix : <{NS}> ."})
    finally:
        app.dependency_overrides.clear()
    assert ":file_name" in seen["summary"]
