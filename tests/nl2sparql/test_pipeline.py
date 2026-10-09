"""End-to-end tests for :class:`arango_sparql.nl2sparql.NlPipeline`.

Pipeline behaviours covered:

* Happy path: one LLM call, translator succeeds, outcome carries the
  AQL + bind vars.
* Repair loop: first LLM response is broken SPARQL, second response is
  correct. Outcome reports ``repaired=True``, two LLM call records,
  and the W_NL_REPAIRED warning.
* Repair exhaustion: all attempts fail, outcome carries empty AQL +
  W_NL_TRANSLATION_FAILED warning. Bound by ``max_repairs``.
* LLM transport failure: client.generate raises — outcome captures
  the exception in the audit record and surfaces a translation
  failure (no AQL).
* /nl-explain second-pass call: explanation renders into the outcome.
* SPARQL-only explain: skips the LLM translation pass and runs the
  deterministic translator directly.

Tests use a :class:`ScriptedLLMClient` so no real network calls fire.
"""

from __future__ import annotations

import pytest

from arango_sparql.nl2sparql import (
    LLMResponse,
    NlPipeline,
    ScriptedLLMClient,
)
from arango_sparql.translate.resolver import SchemaResolver

ONTOLOGY = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

:Person a owl:Class ;
    phys:collectionName "Person" .

:name a owl:DatatypeProperty ;
    rdfs:domain :Person ;
    rdfs:range <http://www.w3.org/2001/XMLSchema#string> .
""".strip()

GOOD_SPARQL = """
PREFIX : <http://ex.org/>
SELECT ?s ?n WHERE {
  ?s a :Person ;
     :name ?n .
}
LIMIT 5
""".strip()

ANOTHER_GOOD_SPARQL = """
PREFIX : <http://ex.org/>
SELECT ?s WHERE { ?s a :Person }
""".strip()

BAD_SPARQL = "SELECT WHERE { broken syntax"

UNSUPPORTED_SPARQL = """
PREFIX : <http://ex.org/>
SELECT ?s WHERE { SERVICE <http://other.example/sparql> { ?s a :Person } }
""".strip()


def _resolver() -> SchemaResolver:
    return SchemaResolver.from_turtle(ONTOLOGY)


def _wrap(sparql: str) -> str:
    """Wrap a SPARQL string in a fenced block — what the LLM would emit."""
    return f"Here you go:\n\n```sparql\n{sparql}\n```"


def _llm_response(content: str, *, prompt: int = 100, completion: int = 50) -> LLMResponse:
    return LLMResponse(
        content=content,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_single_call_returns_aql_with_bind_vars(self) -> None:
        client = ScriptedLLMClient([_llm_response(_wrap(GOOD_SPARQL))], latency_ms=0)
        pipeline = NlPipeline(client=client, resolver=_resolver(), ontology_ttl=ONTOLOGY)
        outcome = pipeline.run("Find all people with their names")

        assert outcome.aql, "expected non-empty AQL on the happy path"
        assert "FOR " in outcome.aql
        assert isinstance(outcome.bind_vars, dict)
        assert outcome.repaired is False
        assert outcome.llm_calls == 1
        assert len(outcome.llm_call_records) == 1
        record = outcome.llm_call_records[0]
        assert record.provider == "openai"
        assert record.model == "gpt-4o-mini"
        assert record.error is None
        assert record.prompt_tokens == 100
        assert record.completion_tokens == 50

    def test_warnings_from_translator_pass_through(self) -> None:
        """Translator-side warnings (e.g. unmapped predicate) reach the outcome."""
        client = ScriptedLLMClient([_llm_response(_wrap(ANOTHER_GOOD_SPARQL))], latency_ms=0)
        pipeline = NlPipeline(client=client, resolver=_resolver(), ontology_ttl=ONTOLOGY)
        outcome = pipeline.run("Find all people")
        assert outcome.aql
        # ``warnings`` is a list of dicts (translator may or may not add some);
        # the pipeline's own ``W_NL_REPAIRED`` warning must NOT appear on the
        # happy path.
        assert all(w.get("code") != "W_NL_REPAIRED" for w in outcome.warnings)

    def test_cost_is_aggregated_from_records(self) -> None:
        """``cost_usd`` is the sum across every LLM call in the run."""
        client = ScriptedLLMClient(
            [_llm_response(_wrap(GOOD_SPARQL), prompt=1000, completion=500)],
            provider="openai",
            model="gpt-4o",
            latency_ms=0,
        )
        pipeline = NlPipeline(client=client, resolver=_resolver(), ontology_ttl=ONTOLOGY)
        outcome = pipeline.run("Find all people")
        # gpt-4o pricing: 1k prompt @ $0.0025 + 0.5k completion @ $0.010 = 0.0075
        assert outcome.cost_usd == pytest.approx(0.0075, abs=1e-6)


# ---------------------------------------------------------------------------
# Repair loop
# ---------------------------------------------------------------------------


class TestRepairLoop:
    def test_first_failure_then_success_marks_repaired(self) -> None:
        client = ScriptedLLMClient(
            [_llm_response(_wrap(BAD_SPARQL)), _llm_response(_wrap(GOOD_SPARQL))],
            latency_ms=0,
        )
        pipeline = NlPipeline(client=client, resolver=_resolver(), ontology_ttl=ONTOLOGY, max_repairs=2)
        outcome = pipeline.run("Find all people with their names")

        assert outcome.aql
        assert outcome.repaired is True
        assert outcome.llm_calls == 2
        # The W_NL_REPAIRED warning must surface so the UI can show the badge.
        assert any(w.get("code") == "W_NL_REPAIRED" for w in outcome.warnings)
        # The repair message embedded into the second user turn must
        # carry the stable error code so the LLM can disambiguate the
        # failure mode.
        second_call_messages = client.calls[1]
        user_turn = second_call_messages[-1]["content"]
        assert "[E_SPARQL_PARSE]" in user_turn

    def test_unsupported_construct_repair_includes_hint(self) -> None:
        """Unsupported-feature errors get an extra nudge in the repair message."""
        client = ScriptedLLMClient(
            [
                _llm_response(_wrap(UNSUPPORTED_SPARQL)),
                _llm_response(_wrap(GOOD_SPARQL)),
            ],
            latency_ms=0,
        )
        pipeline = NlPipeline(client=client, resolver=_resolver(), ontology_ttl=ONTOLOGY, max_repairs=2)
        outcome = pipeline.run("Find all people")
        assert outcome.aql
        assert outcome.repaired is True
        repair_user_turn = client.calls[1][-1]["content"]
        assert "[E_SPARQL_UNSUPPORTED]" in repair_user_turn
        # The hint must mention SPARQL 1.1 alternatives explicitly.
        assert "SPARQL 1.1" in repair_user_turn

    def test_repair_exhausted_returns_failure_outcome(self) -> None:
        """Every attempt fails → outcome carries empty AQL + W_NL_TRANSLATION_FAILED."""
        client = ScriptedLLMClient(
            [
                _llm_response(_wrap(BAD_SPARQL)),
                _llm_response(_wrap(BAD_SPARQL)),
                _llm_response(_wrap(BAD_SPARQL)),
            ],
            latency_ms=0,
        )
        pipeline = NlPipeline(client=client, resolver=_resolver(), ontology_ttl=ONTOLOGY, max_repairs=2)
        outcome = pipeline.run("Find all people")

        assert outcome.aql == ""
        assert outcome.bind_vars == {}
        # Total LLM calls = 1 (first) + 2 (repairs) = 3
        assert outcome.llm_calls == 3
        # The failure marker must surface so the route layer can map to 422.
        codes = {w.get("code") for w in outcome.warnings}
        assert "W_NL_TRANSLATION_FAILED" in codes

    def test_max_repairs_zero_disables_loop(self) -> None:
        """``max_repairs=0`` → exactly one LLM call; no retry on failure."""
        client = ScriptedLLMClient([_llm_response(_wrap(BAD_SPARQL))], latency_ms=0)
        pipeline = NlPipeline(client=client, resolver=_resolver(), ontology_ttl=ONTOLOGY, max_repairs=0)
        outcome = pipeline.run("Find all people")
        assert outcome.aql == ""
        assert outcome.llm_calls == 1
        assert outcome.repaired is False


# ---------------------------------------------------------------------------
# LLM transport failure
# ---------------------------------------------------------------------------


class TestLlmTransportFailure:
    def test_transport_exception_recorded_on_audit(self) -> None:
        client = ScriptedLLMClient([RuntimeError("boom")], latency_ms=0)
        pipeline = NlPipeline(client=client, resolver=_resolver(), ontology_ttl=ONTOLOGY)
        outcome = pipeline.run("anything")
        assert outcome.aql == ""
        assert outcome.llm_calls == 1
        record = outcome.llm_call_records[0]
        assert record.error is not None
        assert "boom" in record.error
        assert record.cost_usd == 0.0


# ---------------------------------------------------------------------------
# Engine re-point regression guards (Phase 06.1)
# ---------------------------------------------------------------------------

# Mapping-shaped ontology: physical collection name differs from the class
# local name, so AQL over a populated resolver mentions "PhysWidgets".
MAPPING_ONTOLOGY = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .

:Widget a owl:Class ; phys:collectionName "PhysWidgets" .
""".strip()

WIDGET_SPARQL = "PREFIX : <http://ex.org/>\nSELECT ?s WHERE { ?s a :Widget }"


class TestEngineRepointRegressions:
    def test_mapping_only_resolver_parity(self) -> None:
        """A mapping-JSON / analyzer-enriched request populates the resolver
        while ``ontology_ttl`` is "". The engine's validate() seam AND the final
        re-translate must both use the pipeline's own resolver — so the AQL
        reflects the mapped physical collection, not an empty-graph fallback."""
        populated = SchemaResolver.from_turtle(MAPPING_ONTOLOGY)
        client = ScriptedLLMClient([_llm_response(_wrap(WIDGET_SPARQL))], latency_ms=0)
        pipeline = NlPipeline(client=client, resolver=populated, ontology_ttl="", max_repairs=2)

        outcome = pipeline.run("find widgets")

        assert outcome.aql, "expected a success outcome over the populated resolver"
        assert "PhysWidgets" in outcome.aql
        assert outcome.llm_calls == 1

    def test_params_conflict_degrades_gracefully(self) -> None:
        """The final re-translate applies ``params`` (unlike the params-blind
        validate() seam). A reserved bind-var name makes the builder raise at
        compose time; run() must catch it and return a graceful failure outcome
        rather than crashing."""
        # Control: without the conflicting param the same run succeeds.
        control_client = ScriptedLLMClient([_llm_response(_wrap(GOOD_SPARQL))], latency_ms=0)
        control = NlPipeline(client=control_client, resolver=_resolver(), ontology_ttl=ONTOLOGY)
        control_outcome = control.run("people with names")
        assert control_outcome.aql, "control (no params) must succeed"

        # Conflicting param: a reserved (builder-generated) bind name.
        client = ScriptedLLMClient([_llm_response(_wrap(GOOD_SPARQL))], latency_ms=0)
        pipeline = NlPipeline(client=client, resolver=_resolver(), ontology_ttl=ONTOLOGY)
        outcome = pipeline.run("people with names", params={"_p0": 1})

        assert outcome.aql == ""
        assert outcome.llm_calls >= 1
        codes = {w.get("code") for w in outcome.warnings}
        assert "W_NL_TRANSLATION_FAILED" in codes


# ---------------------------------------------------------------------------
# explain()
# ---------------------------------------------------------------------------


class TestExplain:
    def test_explain_with_nl_runs_translate_then_explain(self) -> None:
        client = ScriptedLLMClient(
            [
                _llm_response(_wrap(GOOD_SPARQL)),
                _llm_response("This query selects every Person and their name."),
            ],
            latency_ms=0,
        )
        pipeline = NlPipeline(client=client, resolver=_resolver(), ontology_ttl=ONTOLOGY)
        outcome = pipeline.explain(nl="people with names")
        assert outcome.aql, "translation pass must produce AQL"
        assert outcome.explanation.startswith("This query selects")
        assert outcome.llm_calls == 2

    def test_explain_with_sparql_only_skips_translation_call(self) -> None:
        """SPARQL-only explain does NOT call the LLM for translation —
        only the explanation pass fires."""
        client = ScriptedLLMClient(
            [_llm_response("Selects ?s and ?n for every Person.")],
            latency_ms=0,
        )
        pipeline = NlPipeline(client=client, resolver=_resolver(), ontology_ttl=ONTOLOGY)
        outcome = pipeline.explain(sparql=GOOD_SPARQL)
        assert outcome.aql, "deterministic translator must run"
        assert outcome.explanation.startswith("Selects")
        # Only the explanation pass — not a translation pass — should
        # have hit the LLM.
        assert outcome.llm_calls == 1

    def test_explain_with_neither_raises_value_error(self) -> None:
        client = ScriptedLLMClient([_llm_response("ignored")], latency_ms=0)
        pipeline = NlPipeline(client=client, resolver=_resolver(), ontology_ttl=ONTOLOGY)
        with pytest.raises(ValueError):
            pipeline.explain()

    def test_explain_with_whitespace_only_sparql_skips_explain_call(self) -> None:
        """Whitespace-only SPARQL → translator surfaces a parse error;
        the explain pass is skipped via the W_NL_EXPLAIN_EMPTY warning
        and never reaches the LLM."""
        client = ScriptedLLMClient([_llm_response("ignored")], latency_ms=0)
        pipeline = NlPipeline(client=client, resolver=_resolver(), ontology_ttl=ONTOLOGY)
        outcome = pipeline.explain(sparql="   \n   ")
        assert outcome.explanation == ""
        # Either the translator bailed (W_*_PARSE-style warning) or
        # the explain skip-marker fired — both signal "no LLM call".
        assert any(w.get("code") in ("W_NL_EXPLAIN_EMPTY", "E_SPARQL_PARSE") for w in outcome.warnings)
        # The scripted client must not have been called for the
        # explain pass — the queue still has its single response.
        assert client.calls == []


def test_a_non_transport_failure_reports_itself_not_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (prod.demo IAM, 2026-10-09): a translator ValueError raised
    while validating a candidate surfaced as "LLM transport failure", which
    sent the investigation to the network instead of the translator."""
    import arango_sparql.nl2sparql.pipeline as pipeline_mod

    def _boom(self, *args, **kwargs):
        raise ValueError("invalid collection name: 'IAM-TERRAFORM-DOCS-DEMO_Relations'")

    monkeypatch.setattr(pipeline_mod.NLQueryEngine, "generate", _boom)
    client = ScriptedLLMClient([_llm_response(_wrap(GOOD_SPARQL))], latency_ms=0)
    outcome = NlPipeline(client=client, resolver=_resolver(), ontology_ttl=ONTOLOGY).run("anything")
    messages = " ".join(w.get("message", "") for w in outcome.warnings)
    assert "ValueError" in messages and "IAM-TERRAFORM" in messages
    assert "transport" not in messages
