"""Adapter seams that plug ``arango_sparql`` into the shared NL engine.

The language-agnostic :class:`arango_query_core.nl.engine.NLQueryEngine`
owns the generate → validate → repair *flow* and token accounting, but it
cannot know anything SPARQL-specific or anything about *this* service's
audit/cost bookkeeping. Those concerns live here, as two small pieces the
pipeline injects into the engine:

* :class:`EngineProviderBridge` — adapts our
  :class:`~arango_sparql.nl2sparql.client.LLMClient`
  (``generate(messages) -> LLMResponse``) to the engine's
  :class:`~arango_query_core.nl.providers.LLMProvider` protocol
  (``generate(system, user) -> (content, usage_dict)``). It also owns the
  per-call :class:`~arango_sparql.nl2sparql.models.LLMCallRecord` audit
  trail (RESEARCH work-item 3, option (b): the bridge records one record
  per provider call, since the engine's ``NLResult`` only carries token
  *totals*, not per-call provider/model/cost).

* :class:`SparqlAdapter` — implements the five
  :class:`~arango_query_core.nl.seams.QueryLanguageAdapter` seams, each
  mapped onto a shipped ``nl2sparql`` / transpiler piece. Its ``validate``
  seam runs against the resolver **injected into the constructor** (the
  pipeline's own ``self.resolver``), never a resolver rebuilt from
  ``ontology_ttl`` — so a mapping-JSON / analyzer-enriched request (where
  ``ontology_ttl`` is ``""`` but the resolver is populated) validates
  against the same schema the pipeline's final re-translate will use.

The pipeline (see :mod:`arango_sparql.nl2sparql.pipeline`) wires these two
into ``NLQueryEngine`` in Wave 3; isolating them here lets the
verdict-reproduction, record-accounting, and resolver-parity invariants be
proven independently of the pipeline re-point.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from arango_query_core.nl import FewShotIndex, cached_few_shot_index
from arango_query_core.nl.grounding import LabelIndex, _local_name
from arango_query_core.nl.seams import GuardrailVerdict, ValidationResult

if TYPE_CHECKING:
    # Seam 6/7/8 index types exist only in newer query-core (seam 8's
    # ClassPathIndex from >= 0.2.0). They are used solely in (lazy, __future__)
    # annotations here, so importing them at runtime would break `[nl]` against
    # an older pin — guard behind TYPE_CHECKING so the adapter loads on either
    # query-core version.
    from arango_query_core.nl import ClassPathIndex, LabelIndex, PredicateIndex

from ..api import translate as _api_translate
from ..errors import SparqlError, UnsupportedSparqlError
from ..translate.resolver import SchemaResolver
from .client import LLMClient
from .cost import estimate_llm_cost_usd
from .models import LLMCallRecord, LLMResponse
from .prompt import PromptBuilder, extract_sparql_from_response
from .repair import format_repair_context

if TYPE_CHECKING:
    # Seam 7 (predicate/schema-convention grounding, 07.4) — PredicateIndex is
    # only added to arango_query_core.nl.grounding at the seam-7 SHA Plan 02
    # Task 2 pins. Guard the import so this module still loads cleanly against
    # the still-old pin between Task 1 (this file) and Task 2 (the pin bump) —
    # Pitfall 1's atomicity requirement is about BOTH adapters implementing the
    # seam before the pin makes it mandatory, not about importing a symbol that
    # doesn't exist yet at Task-1 time. Purely a type-checking-time import; no
    # runtime dependency (the adapter never constructs a PredicateIndex itself,
    # only calls methods on one the caller injects).
    from arango_query_core.nl.grounding import PredicateIndex

if TYPE_CHECKING:
    # Seam 8 (relationship-path grounding, 07.6) — ClassPathIndex is only
    # added to arango_query_core.nl.pathindex at Plan 01's still-unpushed,
    # still-unpinned engine commit (30ac7f5). Guard the import so this module
    # still loads cleanly against the still-old 3438305 pin, which this plan
    # deliberately does NOT bump (Plan 03 owns push + pin bump, D-01) — mirrors
    # the PredicateIndex guard above for the identical reason. Purely a
    # type-checking-time import; no runtime dependency (the adapter never
    # constructs a ClassPathIndex itself, only calls methods on one the
    # caller injects).
    from arango_query_core.nl.pathindex import ClassPathIndex

# The four usage keys the engine's LLMProvider protocol expects in the
# returned usage dict — kept in sync with ``arango_query_core.nl.engine._USAGE_KEYS``.
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")

# Dump-vs-retrieve mode threshold (D-01): schemas at or below this many total
# predicates render in FULL ("dump" mode); larger schemas fall back to
# retrieval-limited to k per call. CK25 (30 predicates) dumps; QALD-9-plus
# (250) retrieves — both real fixtures already in this repo (RESEARCH Pattern 4).
PREDICATE_DUMP_THRESHOLD = 40

# Curated few-shot bank (07-02) — disjoint from tests/nl2sparql/eval/corpus.yml.
# parents[2] = repo root: engine_adapter.py -> nl2sparql -> arango_sparql -> repo.
_FEWSHOT_BANK_PATH = Path(__file__).resolve().parents[2] / "tests" / "nl2sparql" / "eval" / "fewshot_bank.yml"


class EngineProviderBridge:
    """Adapt an :class:`LLMClient` to the engine's ``LLMProvider`` protocol.

    The engine calls :meth:`generate` with pre-rendered ``system`` / ``user``
    strings and expects ``(content, usage_dict)`` back. This bridge turns that
    into the ``[{role, content}, …]`` message list our clients consume, and —
    critically — records one :class:`LLMCallRecord` per call on :attr:`records`
    so the pipeline can reconstruct the exact audit trail the engine's
    ``NLResult`` token totals alone cannot express (provider, model, per-call
    cost). This mirrors ``pipeline._call_llm_raw`` field-for-field.

    A transport / provider exception is recorded as an error record (with
    ``cost_usd == 0.0``) and then **re-raised**, so the engine loop terminates
    on a real transport failure rather than validating an empty string and
    burning its retry budget.

    The returned ``content`` is the completion run through
    :func:`extract_sparql_from_response` — the same robust extractor the
    standalone pipeline used. The engine then applies its own
    ``_strip_code_fence`` to that text, but since already-extracted SPARQL is
    fence-free that call is a no-op. Doing extraction here (rather than relying
    on the engine's stricter, prefix-sensitive stripper) preserves the
    standalone pipeline's extraction semantics exactly, so a completion with
    leading prose (``"Here you go:\\n\\n```sparql…"``) is handled identically.
    """

    def __init__(self, client: LLMClient) -> None:
        self._client = client
        self.records: list[LLMCallRecord] = []

    def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        provider = getattr(self._client, "provider", "unknown")
        model = getattr(self._client, "model", "unknown")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        t0 = time.perf_counter()
        try:
            response: LLMResponse = self._client.generate(messages)
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            self.records.append(
                LLMCallRecord(
                    provider=provider,
                    model=model,
                    sparql="",
                    cost_usd=0.0,
                    latency_ms=elapsed_ms,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            # Re-raise so the engine loop stops on a genuine transport failure
            # instead of retrying against an empty candidate.
            raise
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        cost = estimate_llm_cost_usd(provider, model, response.prompt_tokens, response.completion_tokens)
        self.records.append(
            LLMCallRecord(
                provider=provider,
                model=model,
                sparql="",
                cost_usd=cost,
                latency_ms=elapsed_ms,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                cached_tokens=response.cached_tokens,
            )
        )
        return extract_sparql_from_response(response.content), {
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "total_tokens": response.total_tokens,
            "cached_tokens": response.cached_tokens,
        }


#: An LLM-generated query used a "prefixed name pasted after a prefix"
#: (``:phys:typeValue``). Rejected in NL validation so the repair loop can fix
#: it; hand-written SPARQL keeps the translator's local-name fallback.
E_NL_MISPREFIXED_TERM = "E_NL_MISPREFIXED_TERM"


def _misprefixed_properties(warnings: list[dict]) -> list[str]:
    """IRIs of properties whose local name itself contains a ``:``.

    The translator falls back to an IRI's local name as the document
    attribute when the property is not declared (``W_SCHEMA_UNMAPPED_IRI``).
    That fallback is correct for a real field the ontology does not declare
    (e.g. ``:file_name``) — analyzer ontologies declare no datatype properties
    at all, so NL relies on it. A local name containing ``:`` is different: it
    is a prefixed name pasted after another prefix, almost always one of the
    ontology's ``phys:`` mapping annotations (``:phys:typeValue``, seen on
    prod.demo IAM, 2026-10-08) — storage metadata, not data, so the query is
    wrong even though it translates.
    """
    out: list[str] = []
    for w in warnings or []:
        if not isinstance(w, dict) or w.get("code") != "W_SCHEMA_UNMAPPED_IRI":
            continue
        fallback = str(w.get("fallback") or "")
        if ":" in fallback:
            out.append(str(w.get("iri") or fallback))
    return out


def _misprefixed_message(iris: list[str]) -> str:
    listed = ", ".join(f"<{iri}>" for iri in iris)
    return (
        f"Predicate(s) {listed} have a local name containing ':' — a prefixed name "
        "written after another prefix (e.g. ':phys:typeValue'). Terms in the 'phys:' "
        "namespace (phys:typeValue, phys:collectionName, phys:mappingStyle, …) are "
        "storage annotations describing how classes are stored; they are not data and "
        "cannot be queried. Use rdf:type (`a`) with a declared class, a declared "
        "property, or a plain attribute name such as `:file_name`."
    )


class SparqlAdapter:
    """The five ``QueryLanguageAdapter`` seams for SPARQL.

    Each seam maps onto a shipped ``nl2sparql`` / transpiler piece:

    ==========================  ============================================
    Seam                        Maps to
    ==========================  ============================================
    ``grammar_prompt_section``  :class:`PromptBuilder`'s system turn
    ``few_shot_index``          populated ``FewShotIndex`` via
                                 ``cached_few_shot_index`` (mode=auto)
    ``validate``                :func:`arango_sparql.api.translate`
    ``repair_hint``             :func:`format_repair_context`
    ``guardrails``              allow-all (no tenant/write-op checks yet)
    ``grounding_index``         injected ``LabelIndex`` (explicit-injection
                                 only this phase — no production default)
    ``predicate_index``         injected ``PredicateIndex`` (explicit-injection
                                 only this phase — no production default)
    ``path_index``              injected ``ClassPathIndex`` (explicit-injection
                                 only this phase — no production default);
                                 ``path_prompt_section`` resolves anchors from
                                 THIS adapter's own seam-6 index and targets
                                 from its own seam-7 index (D-02)
    ==========================  ============================================

    The constructor takes the caller's **already-built** ``resolver`` (in
    production the pipeline's ``self.resolver``) and a SEPARATE ``ontology_ttl``
    used ONLY to embed the Turtle text into the prompt. The two are decoupled
    on purpose: a mapping-JSON / analyzer-enriched request populates the
    resolver while leaving ``ontology_ttl`` empty. Rebuilding a resolver from
    ``ontology_ttl`` inside :meth:`validate` would drive the engine's
    accept/reject loop against the WRONG (empty) schema and diverge from the
    pipeline's final re-translate.
    """

    language = "sparql"

    def __init__(
        self,
        *,
        resolver: SchemaResolver,
        ontology_ttl: str = "",
        few_shot_index: FewShotIndex | None = None,
        few_shot_mode: str = "auto",
        grounding_index: LabelIndex | None = None,
        predicate_index: PredicateIndex | None = None,
        path_index: ClassPathIndex | None = None,
        schema_summary: str = "",
    ) -> None:
        self.resolver = resolver
        self.ontology_ttl = ontology_ttl
        # Classes + their data properties from the analyzer's mapping
        # (nl2sparql/schema_summary.py) — the OWL alone carries no fields.
        self.schema_summary = schema_summary
        self._few_shot_index = few_shot_index
        self._few_shot_mode = few_shot_mode
        self._grounding_index = grounding_index
        self._predicate_index = predicate_index
        self._path_index = path_index

    def grammar_prompt_section(self, schema_context: str) -> str:  # seam 1
        # Reuse the shipped system-prompt template so the grammar + ontology
        # block stays byte-aligned with the standalone PromptBuilder.
        return PromptBuilder(
            ontology_ttl=self.ontology_ttl,
            schema_summary=self.schema_summary or None,
        ).render_system()

    def few_shot_index(self) -> FewShotIndex | None:  # seam 2
        # Explicit injection wins (tests / the Plan 04 sweep); otherwise return
        # the memoized, populated index built from the curated bank (07-02) via
        # the shared engine's module-scope cached_few_shot_index factory — never
        # constructed inline here (Pitfall 1: a fresh FewShotIndex per adapter
        # construction would reload the SentenceTransformer model + re-embed the
        # whole bank on every request/eval case).
        #
        # WARNING: this PRODUCTION seam requests mode="auto" (D-05) — a
        # deployment lacking the `.[dense]` extra (no torch/sentence-transformers
        # installed) gracefully degrades to BM25, then to a no-op retriever,
        # rather than crashing. This means the measured NL-FEW-02 dense-mode
        # pass-rate lift applies in production ONLY when the service is
        # installed with `.[dense]` (`pip install '.[dense]'`); a default
        # `.[nl]`-only install silently runs BM25/no-op, not dense. Plan 04's
        # eval sweep therefore reports the bm25 arm as the honest DEFAULT-INSTALL
        # number and scopes the dense-lift headline to `.[dense]` deployments.
        if self._few_shot_index is not None:
            return self._few_shot_index
        return cached_few_shot_index(str(_FEWSHOT_BANK_PATH), self._few_shot_mode)

    def validate(self, query: str) -> ValidationResult:  # seam 3
        # Validate against the INJECTED resolver — the same schema the
        # pipeline's final re-translate uses — never one rebuilt from
        # ``ontology_ttl`` (which may be empty for mapping-JSON requests).
        try:
            result = _api_translate(query, resolver=self.resolver)
        except SparqlError as exc:
            return ValidationResult(ok=False, error=str(exc), code=getattr(exc, "code", ""))
        misprefixed = _misprefixed_properties(result.warnings)
        if misprefixed:
            return ValidationResult(
                ok=False,
                error=_misprefixed_message(misprefixed),
                code=E_NL_MISPREFIXED_TERM,
            )
        return ValidationResult(ok=True)

    def repair_hint(self, query: str, failure: ValidationResult) -> str:  # seam 4
        # Reproduce ``format_repair_context`` output. The engine hands us a
        # ValidationResult (code + error), not a SparqlError, so reconstruct
        # the matching error type — this preserves the exact ``[CODE] msg`` +
        # SPARQL-1.1 hint wording the repair-loop tests assert on.
        if failure.code == UnsupportedSparqlError.code:
            error: SparqlError = UnsupportedSparqlError(failure.error)
        else:
            error = SparqlError(failure.error, code=failure.code or SparqlError.code)
        return format_repair_context(error)

    def guardrails(self, query: str, context: dict) -> GuardrailVerdict:  # seam 5
        # Allow-all — no tenant/write-op checks this phase.
        return GuardrailVerdict(allowed=True)

    def grounding_index(self) -> LabelIndex | None:  # seam 6
        # Explicit injection only this phase — no cached-default fallback
        # exists yet (unlike few_shot_index's cached_few_shot_index branch):
        # there is no canonical, production-owned source of instance/entity
        # label data the way there is a curated few-shot bank (RESEARCH Open
        # Question 2). A deployment that never injects a LabelIndex runs
        # ungrounded (grounding_index() -> None), which the engine treats
        # identically to seam 2's "no index" case.
        return self._grounding_index

    def grounding_prompt_section(
        self, question: str, index: LabelIndex, k: int = 20
    ) -> str:  # seam 6 (renderer)
        # Verbatim wording from the spike's entity_block
        # (scratchpad/nl-grounding-spike/grounding_spike.py::entity_block) —
        # this exact header + instruction text is what was empirically
        # measured to lift CK25 execution-graded accuracy 12.2% -> 24.5%
        # (McNemar p=0.031). Do NOT paraphrase; rephrasing invalidates the
        # measured lift. The retrieval/rendering machinery (sanitization,
        # ranking, "no matches -> empty string") lives in LabelIndex
        # (Plan 01) — this method only supplies the fixed SPARQL-specific
        # header/instruction/id_prefix/id_suffix constants.
        return index.format_prompt_section(
            question,
            k=k,
            header="## Known entities (use these EXACT IRIs)",
            instruction=(
                "The question may refer to specific named individuals/things below. When it "
                "does, use the entity's EXACT IRI directly in your query (e.g. `<IRI> pv:... ?x`) "
                "instead of matching on a name literal. Not all listed entities are relevant."
            ),
            id_prefix="<",
            id_suffix=">",
        )

    def predicate_index(self) -> PredicateIndex | None:  # seam 7
        # Explicit injection only this phase — same "no production-default
        # source" rationale as grounding_index (seam 6): no canonical,
        # production-owned TBox predicate index exists yet. A deployment
        # that never injects one runs ungrounded (predicate_index() -> None),
        # which the engine treats identically to seam 6's "no index" case.
        return self._predicate_index

    def predicate_prompt_section(
        self, question: str, index: PredicateIndex, k: int = 20
    ) -> str:  # seam 7 (renderer)
        # D-01 dump-vs-retrieve mode selection lives HERE (adapter-level),
        # never inside PredicateIndex itself (RESEARCH Pattern 4 — "not
        # inside PredicateIndex"). PredicateIndex exposes no public
        # __len__/count() (verified against the pushed seam-7 source,
        # commit 8adc0de), and this plan is in-repo only (no cross-repo edit
        # permitted to add one) — so the total-predicate-count is read off
        # the index's private ``_predicates`` list, a same-workstream,
        # tightly-coupled sibling call rather than a guaranteed public API.
        #
        # CR-01 fix: widening k to total alone is NOT dump mode — the
        # shared scorer's zero-hit filter drops every predicate that shares
        # no label/domain/range token with the question regardless of k.
        # Pass the pinned arango_query_core dump=True kwarg (upstream
        # b669320, CR-01) so a schema at/under the threshold genuinely
        # dumps every predicate, not just the question-matching subset.
        total = len(getattr(index, "_predicates", ()))
        is_dump = 0 < total <= PREDICATE_DUMP_THRESHOLD
        effective_k = total if is_dump else k
        # Wording is PROVISIONAL this phase (RESEARCH Open Question 2) — no
        # "do not paraphrase" freeze yet (unlike seam 6's empirically-measured
        # entity block); byte-identity across both adapters IS still required
        # and enforced by the Plan 04 parity test regardless of freeze status.
        return index.format_prompt_section(
            question,
            k=effective_k,
            header="## Known schema predicates (bind to these EXACT predicates in this EXACT shape)",
            instruction=(
                "Use only the predicates listed below, with the exact direction and shape shown. "
                "Predicates marked VALUE OBJECT or CATEGORY require an extra hop — do not flatten "
                "them into a single triple or invent a class not listed here."
            ),
            dump=is_dump,
        )

    def path_index(self) -> ClassPathIndex | None:  # seam 8
        # Explicit injection only — same "no production-default source"
        # rationale as grounding_index/predicate_index (seams 6/7): no
        # canonical, production-owned TBox path-connectivity index exists
        # yet. A deployment that never injects one runs ungrounded
        # (path_index() -> None), which the engine treats identically to
        # seam 6/7's "no index" case.
        return self._path_index

    def path_prompt_section(
        self, question: str, index: ClassPathIndex, k: int = 5
    ) -> str:  # seam 8 (renderer)
        # D-02 / Pitfall 4: this is the ONE adapter method that does more
        # than delegate. It resolves anchor classes from THIS adapter's own
        # injected seam-6 grounding index (the grounded entity's ``.type``,
        # already a LOCAL NAME — Pitfall 3) and target tokens from THIS
        # adapter's own injected seam-7 predicate index (the retrieved
        # predicates' IRI local names), then feeds those PRE-RESOLVED
        # identifiers into ``ClassPathIndex.shortest_paths`` — it never
        # re-parses the raw question and never introduces a third scorer.
        # Anchors/targets pool ALL retrieved hits (distinct types x top
        # targets, not strict top-1, per D-02) — the <=5-path global-pool
        # budget inside ClassPathIndex is the anti-distraction valve, not
        # a top-1 restriction here. Returns '' when either side is
        # unresolved (no grounding_index / no predicate_index injected, or
        # neither retrieves a hit) or shortest_paths finds nothing — an
        # honest no-op, mirroring seam 6/7's own no-match contract.
        grounding = self._grounding_index
        predicates = self._predicate_index
        if grounding is None or predicates is None:
            return ""

        anchor_classes: list[str] = []
        for entity in grounding.retrieve(question, k=20):
            if entity.type and entity.type not in anchor_classes:
                anchor_classes.append(entity.type)

        targets: list[str] = []
        for predicate in predicates.retrieve(question, k=20):
            name = _local_name(predicate.iri)
            if name and name not in targets:
                targets.append(name)

        if not anchor_classes or not targets:
            return ""

        # Wording is PROVISIONAL this phase (D-04: shared-variable join/star
        # framing, NOT a directed A-to-B-to-C walk) — tunable during the
        # credentialed sweep, not frozen; byte-identity across both
        # adapters IS still required and enforced by the 3rd parity test.
        return index.format_prompt_section(
            anchor_classes,
            targets,
            k=k,
            header="## Known navigation paths (use a SHARED variable per hop)",
            instruction=(
                "These are the ONLY valid multi-hop joins from the grounded entity above "
                "to the requested target. Render each hop as a shared-variable join (reusing "
                "one intermediate variable across hops), never as separate, unconnected triples."
            ),
        )
