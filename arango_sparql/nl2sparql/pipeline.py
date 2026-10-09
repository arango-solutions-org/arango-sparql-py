"""End-to-end NL → SPARQL → AQL orchestration.

Drives the shared, language-agnostic
:class:`arango_query_core.nl.engine.NLQueryEngine` (the generate → validate →
repair loop) through two injected seams —
:class:`~arango_sparql.nl2sparql.engine_adapter.EngineProviderBridge` and
:class:`~arango_sparql.nl2sparql.engine_adapter.SparqlAdapter` — then maps the
engine's ``NLResult`` back onto the public
:class:`~arango_sparql.nl2sparql.models.PipelineOutcome`. This is consumed by
the FastAPI route layer (``/nl-translate``, ``/nl-execute``) and the
``/nl-explain`` second-pass call.

Pipeline contract:

1. Construct the bridge (LLMClient → engine ``LLMProvider``) and the
   ``SparqlAdapter`` (the five language seams), built with the pipeline's OWN
   ``self.resolver`` so ``validate()`` and the final re-translate share one
   schema even for mapping-JSON / analyzer-enriched requests.
2. Let ``NLQueryEngine.generate`` run the generate → validate → repair loop,
   bounded by ``self.repair_loop.max_repairs``.
3. The engine only knows "valid / not" (the ``validate`` seam discards the
   ``TranslateResult``), so on success re-translate the final query ONCE to
   recover ``aql`` / ``bind_vars`` / translator warnings.
4. Return a :class:`~arango_sparql.nl2sparql.models.PipelineOutcome` with the
   final SPARQL, AQL, bind vars, warnings, per-call ``LLMCallRecord`` audit
   trail (recorded by the bridge), total wall-clock latency, and a ``repaired``
   flag.

Cost / audit accounting (ROADMAP Success Criterion 6): the engine's
``NLResult`` carries only token *totals*, so this pipeline uses
option **(b)** from ``06.1-RESEARCH.md`` — the ``EngineProviderBridge`` records
one ``LLMCallRecord`` per provider call (provider, model, tokens, cost),
reconstructing the exact per-call audit trail the standalone loop produced.

Fence extraction is kept faithful to the standalone pipeline: the
``EngineProviderBridge`` runs each completion through
``extract_sparql_from_response`` before handing it to the engine, so the
engine's stricter, prefix-sensitive ``_strip_code_fence`` sees already-clean
SPARQL and is a no-op. The one remaining, accepted, non-gating deviation is the
engine's retry-prompt WORDING (it builds its own "your previous query was
rejected" turn rather than reusing ``_REPAIR_USER_SUFFIX``); the repair *hint*
still carries the exact ``[CODE] msg`` + SPARQL-1.1 text via
``format_repair_context``. Both are identical on the scripted eval corpus (the
CI gate); only the retry template could shift *live-model* output, which is why
the live-provider sweep is non-gating.

The pipeline never raises on translation failure — it returns the outcome with
empty AQL and a ``W_NL_TRANSLATION_FAILED`` warning. Because the final
re-translate additionally applies ``params`` (unlike the params-blind
``validate()`` seam), it is guarded so a params-driven ``SparqlError`` OR a
reserved-bind-name ``ValueError`` degrades gracefully rather than crashing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from arango_query_core.nl import FewShotIndex
from arango_query_core.nl.engine import NLQueryEngine
from arango_query_core.nl.grounding import LabelIndex

if TYPE_CHECKING:
    # Seam 7 (predicate/schema-convention grounding, 07.4) — PredicateIndex is
    # only added to arango_query_core.nl.grounding at the seam-7 SHA Plan 02
    # Task 2 pins. Guard the import so this module still loads cleanly against
    # the still-old pin between Task 1 (this file) and Task 2 (the pin bump).
    from arango_query_core.nl.grounding import PredicateIndex

if TYPE_CHECKING:
    # Seam 8 (relationship-path grounding, 07.6) — ClassPathIndex is only
    # added to arango_query_core.nl.pathindex at Plan 01's still-unpushed,
    # still-unpinned engine commit (30ac7f5). Guard the import so this module
    # still loads cleanly against the still-old 3438305 pin, which this plan
    # deliberately does NOT bump (Plan 03 owns push + pin bump, D-01) —
    # mirrors the PredicateIndex guard above for the identical reason.
    from arango_query_core.nl.pathindex import ClassPathIndex

from ..api import TranslateResult
from ..api import translate as _translate
from ..errors import SparqlError
from ..translate.resolver import SchemaResolver
from .client import LLMClient
from .cost import estimate_llm_cost_usd
from .engine_adapter import EngineProviderBridge, SparqlAdapter
from .models import LLMCallRecord, LLMResponse, PipelineOutcome
from .prompt import PromptBuilder, build_explain_messages, extract_sparql_from_response
from .repair import RepairLoop

logger = logging.getLogger(__name__)


@dataclass
class _SingleCallOutcome:
    """Internal envelope for one LLM round-trip (carried into the audit trail)."""

    sparql: str
    record: LLMCallRecord
    raw_content: str


class NlPipeline:
    """End-to-end orchestrator for the NL → SPARQL → AQL flow.

    Construct once per request (the LLM client is typically a process-
    wide singleton, the resolver is per-request because the ontology
    can vary per call). Call :meth:`run` for ``/nl-translate`` and
    :meth:`explain` for ``/nl-explain``.

    Multitenancy / entity-resolution hooks listed in rule 300 are
    deliberately deferred — they will land as separate submodules
    (``tenant_guardrail.py``, ``entity_resolution.py``) once the
    deterministic translator is far enough along to validate the LLM's
    output against a real schema. Few-shot (``fewshot.py``) has landed
    (Phase 7). Entity/instance grounding (seam 6) also lands here: this
    pipeline threads ``grounding_k``/``grounding_index`` through to the
    ``SparqlAdapter``/``NLQueryEngine`` construction in :meth:`run`, but
    only the eval-harness-injected path is wired this phase — production
    ``db``-handle-backed grounding (building a ``LabelIndex`` from a
    live ArangoDB instance graph) remains deferred (RESEARCH Open
    Question 2), so production callers that omit ``grounding_index``
    get the honest degraded no-op (``grounding_index=None`` ->
    ungrounded prompts, byte-identical to pre-Phase-07.3 behavior).
    Predicate/schema-convention grounding (seam 7, 07.4) follows the exact
    same pattern one level up the stack: this pipeline threads
    ``predicate_k``/``predicate_index`` through to the
    ``SparqlAdapter``/``NLQueryEngine`` construction in :meth:`run`,
    explicit-injection-only (no production-default TBox predicate index
    exists yet), so callers that omit ``predicate_index`` get the same
    honest degraded no-op.
    Relationship-path grounding (seam 8, 07.6) follows the exact same
    pattern one level up the stack again: this pipeline threads
    ``path_k``/``path_index`` through to the ``SparqlAdapter``/
    ``NLQueryEngine`` construction in :meth:`run`, explicit-injection-only
    (no production-default path-connectivity index exists yet), so
    callers that omit ``path_index`` get the same honest degraded no-op.
    The pipeline shape exposed here will absorb the remaining hooks via
    constructor args without breaking the API contract.
    """

    def __init__(
        self,
        *,
        client: LLMClient,
        resolver: SchemaResolver,
        ontology_ttl: str = "",
        max_repairs: int = 2,
        few_shot_k: int = 3,
        few_shot_index: FewShotIndex | None = None,
        grounding_k: int = 20,
        grounding_index: LabelIndex | None = None,
        predicate_k: int = 20,
        predicate_index: PredicateIndex | None = None,
        path_k: int = 5,
        path_index: ClassPathIndex | None = None,
        schema_summary: str = "",
    ) -> None:
        self.client = client
        self.resolver = resolver
        self.ontology_ttl = ontology_ttl
        self.schema_summary = schema_summary
        self.repair_loop = RepairLoop(max_repairs=max_repairs)
        # rule-300 caps few-shot at <=3 shots; the Plan 04 sweep overrides
        # both via explicit passthrough (zero/dense/bm25 arm selection)
        # without needing to edit this file again.
        self.few_shot_k = few_shot_k
        self.few_shot_index = few_shot_index
        # Entity/instance grounding (seam 6, 07.3) — explicit-injection-only
        # this phase (mirrors few_shot_index's injection branch); production
        # callers that omit grounding_index get grounding_index=None, which
        # SparqlAdapter.grounding_index() returns as-is (ungrounded, no-op).
        self.grounding_k = grounding_k
        self.grounding_index = grounding_index
        # Predicate/schema-convention grounding (seam 7, 07.4) —
        # explicit-injection-only this phase (mirrors grounding_index's
        # injection branch); production callers that omit predicate_index
        # get predicate_index=None, which SparqlAdapter.predicate_index()
        # returns as-is (ungrounded, no-op).
        self.predicate_k = predicate_k
        self.predicate_index = predicate_index
        # Relationship-path grounding (seam 8, 07.6) — explicit-injection-only
        # this phase (mirrors predicate_index's injection branch); production
        # callers that omit path_index get path_index=None, which
        # SparqlAdapter.path_index() returns as-is (ungrounded, no-op).
        self.path_k = path_k
        self.path_index = path_index

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def run(
        self,
        nl: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> PipelineOutcome:
        """Translate ``nl`` → SPARQL → AQL by driving the shared engine.

        The private PromptBuilder → LLMClient → RepairLoop loop is replaced by
        :class:`arango_query_core.nl.engine.NLQueryEngine`, wired with the
        ``EngineProviderBridge`` (per-call audit records) and a ``SparqlAdapter``
        built with the pipeline's OWN ``self.resolver`` (so ``validate()`` and
        the final re-translate use the same schema). The engine's ``NLResult``
        is mapped back to the public ``PipelineOutcome`` shape.
        """
        bridge = EngineProviderBridge(self.client)
        adapter = SparqlAdapter(
            resolver=self.resolver,
            ontology_ttl=self.ontology_ttl,
            schema_summary=self.schema_summary,
            few_shot_index=self.few_shot_index,
            grounding_index=self.grounding_index,
            predicate_index=self.predicate_index,
            path_index=self.path_index,
        )
        engine = NLQueryEngine(
            provider=bridge,
            adapter=adapter,
            few_shot_k=self.few_shot_k,
            grounding_k=self.grounding_k,
            predicate_k=self.predicate_k,
            path_k=self.path_k,
            max_retries=self.repair_loop.max_repairs,
        )
        t0 = time.perf_counter()

        try:
            result = engine.generate(nl, schema_context="")
        except Exception as exc:
            # A genuine transport failure is recorded by the bridge and
            # re-raised (so the engine loop doesn't validate an empty string).
            reason = bridge.records[-1].error if bridge.records else f"{type(exc).__name__}: {exc}"
            return self._failure_outcome(
                nl=nl,
                sparql="",
                records=list(bridge.records),
                warnings=[],
                t0=t0,
                repaired=False,
                reason=reason or "LLM transport failure",
            )

        repaired = result.retries > 0

        if not result.ok:
            return self._failure_outcome(
                nl=nl,
                sparql=result.query,
                records=list(bridge.records),
                warnings=[],
                t0=t0,
                repaired=repaired,
                reason=result.error or "translation failed",
            )

        # The ``validate`` seam only reports valid/not and discards the
        # TranslateResult (RESEARCH work-item 1). Re-translate the final query
        # ONCE (deterministic, cheap) to recover aql/bind_vars/warnings. This
        # pass ALSO applies ``params`` — which the params-blind ``validate()``
        # seam omitted — so it is guarded: a params-driven SparqlError, or a
        # reserved-bind-name ValueError the builder raises at compose time, must
        # degrade to a graceful W_NL_TRANSLATION_FAILED outcome, never crash
        # out of run() (preserving the pipeline's never-raise contract).
        try:
            translate_result: TranslateResult = _translate(
                result.query, resolver=self.resolver, params=params
            )
        except SparqlError as exc:
            return self._failure_outcome(
                nl=nl,
                sparql=result.query,
                records=list(bridge.records),
                warnings=[],
                t0=t0,
                repaired=repaired,
                reason=str(exc),
            )
        except ValueError as exc:
            return self._failure_outcome(
                nl=nl,
                sparql=result.query,
                records=list(bridge.records),
                warnings=[],
                t0=t0,
                repaired=repaired,
                reason=str(exc),
            )

        warnings: list[dict[str, Any]] = []
        if repaired:
            warnings.append(
                {
                    "code": "W_NL_REPAIRED",
                    "message": (
                        f"NL → SPARQL translation succeeded after {result.retries} repair attempt(s)."
                    ),
                }
            )
        return self._success_outcome(
            nl=nl,
            sparql=result.query,
            translate_result=translate_result,
            records=list(bridge.records),
            warnings=warnings,
            t0=t0,
            repaired=repaired,
        )

    def explain(
        self,
        *,
        nl: str | None = None,
        sparql: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> PipelineOutcome:
        """Translate (if needed) and ask the LLM for a plain-English summary."""
        if not nl and not sparql:
            raise ValueError("explain() requires at least one of nl=, sparql=")

        if nl:
            outcome = self.run(nl, params=params)
        else:
            # SPARQL-only path — translate the user-supplied SPARQL
            # without an LLM call. Synthesises an empty audit trail so
            # the response shape stays uniform.
            outcome = self._translate_only(sparql or "", params=params)

        # Use the resulting SPARQL (translated or user-supplied) for
        # the explanation pass. Empty SPARQL → emit an empty
        # explanation with a warning rather than a second LLM call.
        target_sparql = outcome.sparql or sparql or ""
        if not target_sparql.strip():
            outcome.warnings.append(
                {
                    "code": "W_NL_EXPLAIN_EMPTY",
                    "message": "No SPARQL available to explain; skipping LLM round-trip.",
                }
            )
            return outcome

        record, content = self._call_llm_raw(build_explain_messages(target_sparql))
        outcome.llm_call_records.append(record)
        outcome.explanation = content.strip() if not record.error else ""
        if record.error:
            outcome.warnings.append(
                {
                    "code": "W_NL_EXPLAIN_FAILED",
                    "message": f"Explanation LLM call failed: {record.error}",
                }
            )
        return outcome

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _translate_only(self, sparql: str, *, params: dict[str, Any] | None) -> PipelineOutcome:
        """SPARQL-in / AQL-out without an LLM call (used by ``/nl-explain`` SPARQL path)."""
        t0 = time.perf_counter()
        try:
            tr = _translate(sparql, resolver=self.resolver, params=params)
        except SparqlError as exc:
            return PipelineOutcome(
                nl="",
                sparql=sparql,
                aql="",
                bind_vars={},
                warnings=[{"code": exc.code, "message": str(exc)}],
                latency_ms=int((time.perf_counter() - t0) * 1000),
                repaired=False,
            )
        return PipelineOutcome(
            nl="",
            sparql=sparql,
            aql=tr.aql,
            bind_vars=dict(tr.bind_vars),
            warnings=list(tr.warnings or []),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            repaired=False,
        )

    def _call_llm(
        self,
        builder: PromptBuilder,
        nl: str,
        *,
        repair_context: str,
    ) -> _SingleCallOutcome:
        """Send one prompt to the LLM and parse the response into SPARQL."""
        builder.repair_context = repair_context
        messages = builder.render_messages(nl)
        record, content = self._call_llm_raw(messages)
        sparql = extract_sparql_from_response(content) if not record.error else ""
        record.sparql = sparql
        return _SingleCallOutcome(sparql=sparql, record=record, raw_content=content)

    def _call_llm_raw(self, messages: list[dict[str, str]]) -> tuple[LLMCallRecord, str]:
        """Invoke the underlying client; return (audit record, raw content).

        Wraps every transport / provider exception so a network blip in
        a long-running request doesn't propagate raw — the pipeline
        records the failure on the audit trail and the caller decides
        whether to surface a warning, a 502, or a 503.
        """
        provider = getattr(self.client, "provider", "unknown")
        model = getattr(self.client, "model", "unknown")
        t0 = time.perf_counter()
        try:
            response: LLMResponse = self.client.generate(messages)
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.warning("LLM call failed: %s", exc)
            record = LLMCallRecord(
                provider=provider,
                model=model,
                sparql="",
                cost_usd=0.0,
                latency_ms=elapsed_ms,
                error=f"{type(exc).__name__}: {exc}",
            )
            return record, ""
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        cost = estimate_llm_cost_usd(provider, model, response.prompt_tokens, response.completion_tokens)
        record = LLMCallRecord(
            provider=provider,
            model=model,
            sparql="",
            cost_usd=cost,
            latency_ms=elapsed_ms,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            cached_tokens=response.cached_tokens,
        )
        return record, response.content

    def _success_outcome(
        self,
        *,
        nl: str,
        sparql: str,
        translate_result: TranslateResult,
        records: list[LLMCallRecord],
        warnings: list[dict[str, Any]],
        t0: float,
        repaired: bool,
    ) -> PipelineOutcome:
        merged_warnings = list(warnings) + list(translate_result.warnings or [])
        return PipelineOutcome(
            nl=nl,
            sparql=sparql,
            aql=translate_result.aql,
            bind_vars=dict(translate_result.bind_vars),
            warnings=merged_warnings,
            llm_call_records=records,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            repaired=repaired,
        )

    def _failure_outcome(
        self,
        *,
        nl: str,
        sparql: str,
        records: list[LLMCallRecord],
        warnings: list[dict[str, Any]],
        t0: float,
        repaired: bool,
        reason: str,
    ) -> PipelineOutcome:
        warnings = list(warnings)
        warnings.append(
            {
                "code": "W_NL_TRANSLATION_FAILED",
                "message": f"NL → SPARQL → AQL translation failed: {reason}",
            }
        )
        return PipelineOutcome(
            nl=nl,
            sparql=sparql,
            aql="",
            bind_vars={},
            warnings=warnings,
            llm_call_records=records,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            repaired=repaired,
        )
