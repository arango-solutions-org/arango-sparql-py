"""NL → SPARQL evaluation harness.

Mirrors ``tests/nl2cypher/eval/runner.py`` from ``arango-cypher-py``:
consumes ``corpus.yml`` + ``configs.yml``, executes each corpus entry
against each configured provider, and writes JSON + Markdown reports
under ``reports/`` (gitignored).

The runner accepts any LLM provider, so unit tests pass a scripted
mock and CI sweeps pass a real ``OpenAIProvider``. Only ``baseline.json``
is checked in — that is the regression gate.
"""

from __future__ import annotations

import json
import math
import random
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from arango_query_core.nl import DenseRetriever, FewShotIndex, cached_few_shot_index
from pydantic import BaseModel, Field, model_validator
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.term import BNode, Variable

from arango_sparql.errors import SparqlParseError
from arango_sparql.nl2sparql import (
    AnthropicClient,
    LLMClient,
    LLMResponse,
    NlPipeline,
    OpenAICompatibleClient,
    ScriptedLLMClient,
)
from arango_sparql.nl2sparql.engine_adapter import _FEWSHOT_BANK_PATH
from arango_sparql.translate.parser import parse_sparql
from arango_sparql.translate.resolver import SchemaResolver

EVAL_DIR = Path(__file__).parent
CORPUS_PATH = EVAL_DIR / "corpus.yml"
CONFIGS_PATH = EVAL_DIR / "configs.yml"
REPORTS_DIR = EVAL_DIR / "reports"
# Curated few-shot bank (07-02) — same path SparqlAdapter's production default
# resolves to (engine_adapter.py::_FEWSHOT_BANK_PATH), shared here so the
# dense/bm25 sweep arms build against the identical bank file.
BANK_PATH = _FEWSHOT_BANK_PATH


@dataclass
class CaseResult:
    name: str
    expected: str
    actual: str
    passed: bool
    elapsed_ms: float = 0.0
    judge_note: str | None = None


@dataclass
class Report:
    config: str
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return sum(1 for c in self.cases if c.passed) / max(len(self.cases), 1)


# ---------------------------------------------------------------------------
# Load-time schema gate — CorpusCase / BaselineConfig (AI-SPEC §4b)
# ---------------------------------------------------------------------------
#
# A malformed/unparseable positive gold must FAIL the corpus load loudly
# rather than be silently dropped — a skipped case is a hidden coverage hole
# (AI-SPEC Critical Failure Mode 2). Pydantic gives us the load-time gate;
# the ``_gold_must_parse`` validator runs the deterministic SPARQL parser on
# every positive gold so a bad gold surfaces as a ``ValidationError`` the
# instant the corpus is read.


class CorpusCase(BaseModel):
    """One eval corpus entry (mirrors the ``corpus.yml`` case shape).

    Positive cases carry a gold ``expected`` SPARQL query the judge targets.
    Negative cases (``expect_refusal: true``) carry a human-readable rationale
    in ``expected`` instead — the honest-refusal convention scores them by the
    inverted signal (no transpilable AQL == PASS), so the gold-must-parse
    validator MUST skip them (AI-SPEC §5 "Scoring negatives").
    """

    name: str = Field(min_length=1)
    nl: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    scripted: str | None = None
    ontology: str | None = None
    params: dict[str, object] | None = None
    data: str | None = None
    # The negatives marker. Pinned exact key — both the corpus and the
    # ``_judge`` inverted branch key on it.
    expect_refusal: bool = False

    @model_validator(mode="after")
    def _gold_must_parse(self) -> CorpusCase:
        # Only positive cases hold gold SPARQL. For refusal cases ``expected``
        # is a rationale string and must NOT be parsed as gold.
        if not self.expect_refusal:
            try:
                parse_sparql(self.expected)
            except SparqlParseError as exc:  # re-raise as pydantic ValueError
                raise ValueError(
                    f"gold `expected` SPARQL for case {self.name!r} does not parse: {exc}"
                ) from exc
        return self


class BaselineConfig(BaseModel):
    """One config's checked-in regression gate (a ``baseline.json`` entry).

    The scripted gate needs only ``pass_rate``/``passed``/``total``/``cases``.
    The optional live-reproducibility fields (``model``, ``temperature``,
    ``corpus_sha``) let Plan 04 fold a live-model run into ``baseline.json``
    without re-touching ``runner.py``. The three ``embedding_*`` fields
    (D-04, Phase 7 07-04) extend that same provenance convention for the
    dense-mode arms — captured at RUN TIME (never hardcoded) so a re-run
    reproduces the same retrieval order.
    """

    pass_rate: float = Field(ge=0.0, le=1.0)
    passed: int = Field(ge=0)
    total: int = Field(ge=1)
    cases: dict[str, bool]
    model: str | None = None
    temperature: float | None = None
    corpus_sha: str | None = None
    # Phase 7 07-04 additions — dense-run provenance (D-04).
    embedding_model: str | None = None
    embedding_revision: str | None = None
    sentence_transformers_version: str | None = None


# ---------------------------------------------------------------------------
# Loaders — trusted checked-in YAML, always via yaml's safe_load only.
# ---------------------------------------------------------------------------


def _load_corpus(path: Path = CORPUS_PATH) -> dict[str, Any]:
    corpus = yaml.safe_load(path.read_text())
    # Gate every case at load time — a malformed gold fails the load loudly
    # (raises ``ValidationError``) instead of being silently skipped. The
    # validated model is discarded; ``run()`` keeps its existing ``case[...]``
    # dict access unchanged (this is a gate, not a data-flow rewrite).
    for case in corpus.get("cases", []):
        CorpusCase(**case)
    return corpus


def _load_configs() -> dict[str, Any]:
    return yaml.safe_load(CONFIGS_PATH.read_text())


# ---------------------------------------------------------------------------
# Scripted-response helper — mirrors `_wrap` in tests/nl2sparql/test_pipeline.py
# ---------------------------------------------------------------------------


def _wrap_sparql(sparql: str) -> LLMResponse:
    """Wrap a SPARQL string in a fenced ```sparql block, as a real model would.

    ``extract_sparql_from_response`` (arango_sparql/nl2sparql/prompt.py) looks
    for this fence first, so the scripted double must mimic it exactly.
    """
    return LLMResponse(
        content=f"```sparql\n{sparql.strip()}\n```",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
    )


# ---------------------------------------------------------------------------
# Provider factory — config["provider"]["type"] -> LLMClient
# ---------------------------------------------------------------------------


def _client_for(config: dict[str, Any], case: dict[str, Any]) -> LLMClient:
    provider = config["provider"]
    ptype = provider["type"]
    if ptype == "scripted":
        # Fresh client per case — ScriptedLLMClient replays its LAST queued
        # response forever once drained, so sharing one client across cases
        # would leak case N-1's SPARQL into case N.
        canned = case.get("scripted", case["expected"])
        return ScriptedLLMClient([_wrap_sparql(canned)], latency_ms=0)
    if ptype in ("openai", "openrouter"):
        return OpenAICompatibleClient(provider=ptype, model=provider.get("model"))
    if ptype == "anthropic":
        return AnthropicClient(model=provider.get("model"))
    raise ValueError(f"unknown provider type {ptype!r}")


# ---------------------------------------------------------------------------
# Judge — rdflib canonical algebra comparison (rule 200: no string matching)
# ---------------------------------------------------------------------------


def _stable_repr(node: Any) -> str:
    """Return a repr of *node* that is stable across ``PYTHONHASHSEED`` runs.

    rdflib's translated algebra embeds raw Python ``set``/``frozenset``
    objects (e.g. every ``BGP``/``Project`` node's ``_vars``) and, for
    ``SELECT *`` queries specifically, a ``Project.PV`` field built via
    ``list(a_set)`` inside ``rdflib.plugins.sparql.algebra`` — see
    ``arango_sparql/translate/parser.py``'s docstring for the identical
    footgun the transpiler proper works around via ``explicit_projection``.
    Plain ``repr()`` of the algebra is therefore not safe to compare across
    interpreter processes (different ``PYTHONHASHSEED`` -> different set
    iteration order for the *same* logical set of variables).

    This walks the (``CompValue`` / ``dict`` / ``list`` / ``tuple`` /
    ``set``) tree and canonicalizes any set-derived structure (raw sets,
    and the specific ``PV`` key which is list-shaped but set-derived) to a
    sorted tuple before falling back to the builtin ``repr()`` for leaves.
    Explicitly-ordered structures (e.g. ``BGP.triples``) are left alone —
    only unordered/set-derived data is canonicalized.
    """
    if isinstance(node, CompValue):
        inner = ", ".join(
            f"{key!r}: {_stable_repr(sorted(value, key=str) if key == 'PV' and isinstance(value, list) else value)}"
            for key, value in node.items()
        )
        return f"{node.name}_{{{inner}}}"
    if isinstance(node, (set, frozenset)):
        return "{" + ", ".join(_stable_repr(v) for v in sorted(node, key=str)) + "}"
    if isinstance(node, dict):
        inner = ", ".join(f"{k!r}: {_stable_repr(v)}" for k, v in node.items())
        return f"{{{inner}}}"
    if isinstance(node, list):
        return "[" + ", ".join(_stable_repr(v) for v in node) + "]"
    if isinstance(node, tuple):
        return "(" + ", ".join(_stable_repr(v) for v in node) + ")"
    return repr(node)


def _skeleton(node: Any) -> str:
    """A ``repr`` of *node* with every ``Variable``/``BNode`` erased to a
    fixed placeholder.

    Used only to order the elements of a set/frozenset in a way that does
    NOT depend on the original variable names (nor on a blank node's
    randomly-generated internal id — ``rdflib`` mints a fresh skolem id on
    every parse, e.g. ``FILTER NOT EXISTS { [] pv:hasManager ?empl }``'s
    anonymous ``[]``), so that alpha-renaming numbering
    (``_alpha_normalize``) is driven by structure rather than by whatever
    names/ids the model or parser happened to pick.
    """
    if isinstance(node, Variable):
        return "?"
    if isinstance(node, BNode):
        return "?_bnode"
    if isinstance(node, CompValue):
        return f"{node.name}{{" + ",".join(f"{k}:{_skeleton(v)}" for k, v in node.items()) + "}"
    if isinstance(node, (set, frozenset)):
        return "{" + ",".join(sorted(_skeleton(v) for v in node)) + "}"
    if isinstance(node, (list, tuple)):
        return "[" + ",".join(_skeleton(v) for v in node) + "]"
    if isinstance(node, dict):
        return "{" + ",".join(f"{k}:{_skeleton(v)}" for k, v in node.items()) + "}"
    return repr(node)


def _alpha_normalize(node: Any, mapping: dict[Variable | BNode, Variable | BNode]) -> Any:
    """Rebuild *node*, replacing each ``Variable``/``BNode`` with a canonical
    ``?v0``/``?v1``/... (or ``_:b0``/``_:b1``/...) assigned on first
    occurrence in a deterministic, name/id-independent walk.

    This makes the canonical judge *alpha-equivalent*: two queries that are
    identical up to a consistent bijective variable renaming (e.g. the gold's
    ``?s ?n`` vs a model's ``?person ?name``) collapse to one canonical form.
    It is SOUND — only consistent renamings unify, because a single bijection
    (``mapping``) is applied across the whole tree before comparison; a
    genuinely different query (extra triple, different projection, swapped
    predicate) cannot collide. Ordered structures (``BGP.triples``, ``PV``)
    seed the numbering; set-derived structures are ordered by ``_skeleton``
    so numbering never depends on the original names.

    ``BNode`` gets the same alpha-renaming treatment as ``Variable`` for the
    same reason: an anonymous blank node (SPARQL's ``[]`` shorthand, e.g. in
    ``FILTER NOT EXISTS { [] pv:hasManager ?empl }``) is assigned a fresh,
    randomly-generated skolem id by rdflib's parser on *every* parse call —
    without this, ``_canonical(same_sparql_string)`` would not even equal
    itself across two separate parses, permanently failing the judge for any
    otherwise-identical gold that happens to use a blank node (discovered via
    CK25's ``ck25-27``/``ck25-40`` gold queries).
    """
    if isinstance(node, Variable):
        if node not in mapping:
            mapping[node] = Variable(f"v{len(mapping)}")
        return mapping[node]
    if isinstance(node, BNode):
        if node not in mapping:
            mapping[node] = BNode(f"b{len(mapping)}")
        return mapping[node]
    if isinstance(node, CompValue):
        return CompValue(
            node.name,
            **{key: _alpha_normalize(value, mapping) for key, value in node.items()},
        )
    if isinstance(node, (set, frozenset)):
        ordered = sorted(node, key=_skeleton)
        return type(node)(_alpha_normalize(v, mapping) for v in ordered)
    if isinstance(node, list):
        return [_alpha_normalize(v, mapping) for v in node]
    if isinstance(node, tuple):
        return tuple(_alpha_normalize(v, mapping) for v in node)
    if isinstance(node, dict):
        return {key: _alpha_normalize(value, mapping) for key, value in node.items()}
    return node


def _canonical(sparql: str) -> str | None:
    try:
        algebra = parse_sparql(sparql).algebra
    except SparqlParseError:
        return None
    return _stable_repr(_alpha_normalize(algebra, {}))


def _judge_canonical(expected: str, outcome: Any) -> bool:
    if not outcome.aql:
        # Transpiler rejected the generated SPARQL (or repair exhausted) —
        # outcome.aql == "" is the authoritative accept signal.
        return False
    canonical_expected = _canonical(expected)
    canonical_actual = _canonical(outcome.sparql)
    return canonical_expected is not None and canonical_expected == canonical_actual


def _canon_row(row: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """Canonicalize a single binding row so key-insertion order (and
    therefore SELECT-projection column order) doesn't affect equality —
    only the (variable, value) pairs it actually contains do."""
    return tuple(sorted(row.items()))


def _strip_execution_literal(raw: str) -> str:
    """Strip pyoxigraph's N-Triples lexical envelope (surrounding quotes,
    optional ``^^<datatype>``/``@lang`` suffix) from a stringified literal
    term, e.g. ``'"Alice"'`` -> ``'Alice'``, ``'"72"^^<...#integer>'`` ->
    ``'72'``, ``'"Alice"@en'`` -> ``'Alice'``."""
    return raw.split('"^^')[0].strip('"').split('"@')[0]


def _build_label_map(store: Any) -> dict[str, str]:
    """One `IRI -> rdfs:label` lookup per judged store (Pattern 2).

    Verified against the real CK25 instance graph: `rdfs:label` strictly
    covers `pv:name` (0 counterexamples), so no domain-specific predicate
    fallback is needed.
    """
    from tests.helpers.oxi import oxi_query

    result = oxi_query(
        store,
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
        "SELECT ?s ?label WHERE { ?s rdfs:label ?label }",
    )
    out: dict[str, str] = {}
    for row in result.rows or []:
        subj = row.get("s")
        label = row.get("label")
        if subj is None or label is None:
            continue
        iri = subj[1:-1] if subj.startswith("<") and subj.endswith(">") else subj
        out[iri] = _strip_execution_literal(label)
    return out


def _normalize_execution_value(raw: str, label_map: dict[str, str]) -> str:
    """Map a stringified pyoxigraph term to a comparable plain-text form.

    An IRI with a known `rdfs:label` normalizes to that label (an entity's
    IRI and its label are equivalent answers, D-03); an IRI with no known
    label falls back to its bare (unbracketed) form. A quoted literal is
    stripped of its N-Triples envelope so it compares equal to a label
    looked up the same way — both sides must land on the SAME plain-text
    representation, or an IRI-normalized-to-label value would never equal
    a literal-projecting candidate's raw quoted string.
    """
    if raw.startswith("<") and raw.endswith(">"):
        iri = raw[1:-1]
        return label_map.get(iri, iri)
    if raw.startswith('"'):
        return _strip_execution_literal(raw)
    return raw


def _execution_row_key(row: dict[str, str], label_map: dict[str, str]) -> tuple[str, ...]:
    """Sorted-tuple row key (Pattern 4) — var-name/column-order insensitive
    AND duplicate-safe (a `frozenset` would silently collapse two equal
    values projected into the same row)."""
    return tuple(sorted(_normalize_execution_value(v, label_map) for v in row.values()))


def _projection_tolerant_match(
    gold_set: set[tuple[str, ...]],
    cand_rows: list[dict[str, str]],
    gold_arity: int,
    label_map: dict[str, str],
) -> bool:
    """True if some column-subset of the candidate reproduces the gold answer set.

    Tolerates a candidate that projects the gold answer PLUS extra descriptive
    columns — the common "a superlative also selects its sort key" / "an entity
    also selects its label or price" shape (e.g. gold ``SELECT ?result`` vs.
    candidate ``SELECT ?oscillator ?price``). Compared as SETS, and only reached
    after exact-multiset and set equality both miss, so it is a strict superset
    of the strict check: it can only ever turn a strict FAIL into a pass, never
    the reverse, so gold-vs-gold identity is unaffected. A genuinely wrong
    candidate still fails — the projected column-set must equal the gold answer
    set EXACTLY (no missing and no extra distinct values), so extra wrong rows or
    a wrong entity in the answer column never match.
    """
    import itertools

    if not cand_rows:
        return not gold_set
    cols = sorted({k for r in cand_rows for k in r})
    # Bound the search: CK25 candidates project a handful of columns; refuse to
    # combinatorially explode on a pathologically wide SELECT.
    if len(cols) > 8:
        return False
    k = gold_arity or 1
    for subset in itertools.combinations(cols, min(k, len(cols))):
        projected = {
            tuple(sorted(_normalize_execution_value(r[c], label_map) for c in subset if r.get(c) is not None))
            for r in cand_rows
        }
        if projected == gold_set:
            return True
    return False


def _judge_execution(expected: str, outcome: Any, data_ttl: str) -> tuple[bool, str | None]:
    """Answer-set execution judge (NL-EVAL-05, D-02..D-05).

    Runs gold + candidate SPARQL through pyoxigraph and compares ANSWERS
    (not query structure): up to variable renaming and IRI<->label
    normalization for SELECT, boolean comparison for ASK. Gold-side and
    candidate-side pyoxigraph failures are caught SEPARATELY and tagged
    with a distinguishing `judge_note` — never blended into a bare `False`
    (Pitfall 3). Returns `(passed, judge_note)`; `judge_note` is `None` for
    an ordinary pass/fail and a short tag string for the two D-05 visible
    buckets.
    """
    if not outcome.aql:
        return False, None

    from tests.helpers.oxi import load_store_from_string, oxi_query

    store = load_store_from_string(data_ttl)

    # Pattern 3: xsd:int is a *derived* XSD type with no implicit SPARQL
    # constructor function; xsd:integer (the primitive supertype) is
    # supported and semantically equivalent for this dataset's untyped
    # decimal-string literals. This is a KNOWN-SAFE engine-compat shim for a
    # valid XSD constructor pyoxigraph simply does not implement — applied
    # SYMMETRICALLY to gold and candidate so a query text is never accepted on
    # one side and rejected on the other (the scripted gold-vs-gold
    # self-consistency invariant, Plan 03 SC3). It is NOT model-error masking:
    # the candidate_engine_rejected bucket (D-05) still fires for genuinely
    # malformed/unsupported candidate SPARQL — only this one valid-but-
    # unimplemented constructor is normalized, identically on both sides.
    gold_sparql = expected.replace("xsd:int(", "xsd:integer(")
    cand_sparql = outcome.sparql.replace("xsd:int(", "xsd:integer(")

    try:
        gold_result = oxi_query(store, gold_sparql)
    except (SyntaxError, RuntimeError) as exc:
        return False, f"gold_engine_limitation: {exc}"

    try:
        cand_result = oxi_query(store, cand_sparql)
    except (SyntaxError, RuntimeError) as exc:
        return False, f"candidate_engine_rejected: {exc}"

    if gold_result.kind != cand_result.kind:
        return False, None  # e.g. gold ASK vs candidate SELECT — a genuine mismatch

    if gold_result.kind == "ask":
        return gold_result.boolean == cand_result.boolean, None

    label_map = _build_label_map(store)
    gold_keys = [_execution_row_key(r, label_map) for r in gold_result.rows or []]
    cand_keys = [_execution_row_key(r, label_map) for r in cand_result.rows or []]
    if sorted(gold_keys) == sorted(cand_keys):
        return True, None  # exact answer (multiset) — the strict path, unchanged

    # Answer-set relaxations. Each is a strict SUPERSET of the exact check above
    # (reached only when it misses), so a previously-passing case can never
    # regress and the scripted gold-vs-gold self-consistency invariant holds:
    #   (1) set equality — the candidate has the right answers but with duplicate
    #       or symmetric rows (a self-join emitting both (a,b) and (b,a): ck25-43);
    #   (2) projection tolerance — the candidate projects the gold answer PLUS
    #       extra descriptive columns beyond the gold's arity (a superlative that
    #       also selects its price/label: ck25-18/19/24). See docs/ck25-failure-atlas.md.
    gold_set = set(gold_keys)
    if gold_set == set(cand_keys):
        return True, None
    gold_arity = len({k for r in (gold_result.rows or []) for k in r})
    if _projection_tolerant_match(gold_set, cand_result.rows or [], gold_arity, label_map):
        return True, None
    return False, None


def _judge(
    judge_name: str,
    case: dict[str, Any],
    outcome: Any,
    data_ttl: str | None = None,
) -> tuple[bool, str | None]:
    if case.get("expect_refusal"):
        # Inverted refusal signal (AI-SPEC §5 "Scoring negatives"): a negative
        # case PASSES iff the pipeline produced NO transpilable AQL. The
        # pipeline surfaces refusal as ``outcome.aql == ""`` + a
        # ``W_NL_TRANSLATION_FAILED`` warning (it never raises), so ``aql`` is
        # the authoritative signal — mirroring ``_judge_canonical``'s empty-AQL
        # check, but inverted. A non-empty AQL over invented terms FAILS.
        return not outcome.aql, None
    # BLOCKER fix: CK25 cases carry NO per-case `data:` field — the instance
    # graph is a CORPUS-LEVEL `data_path:` key threaded in via `data_ttl`.
    # The guard must fire on that corpus-level fixture, not solely on
    # `case.get("data")` (which would never fire for CK25 and would silently
    # reproduce the canonical 0% floor this phase exists to eliminate). A
    # per-case `data` still overrides when present.
    if judge_name == "execution" and (data_ttl is not None or case.get("data")):
        return _judge_execution(case["expected"], outcome, case.get("data") or data_ttl)
    return _judge_canonical(case["expected"], outcome), None


# ---------------------------------------------------------------------------
# run() — drive every corpus case through NlPipeline for one config
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Shape-aware few-shot retrieval (opt-in) — guarantees a query-shape-appropriate
# exemplar for analytic questions, on top of lexical BM25/dense retrieval.
# Motivation (docs/ck25-failure-atlas.md): BM25 ranks by question WORDING, so a
# superlative like "heaviest coil" retrieves lexical "coil" lookups instead of a
# superlative exemplar. For a question whose wording signals an analytic shape,
# this ensures >=1 same-shape exemplar is present; non-analytic questions
# retrieve unchanged (so simple lookups are never displaced).
# ---------------------------------------------------------------------------

_SHAPE_FAMILY: dict[str, set[str]] = {
    "top_n": {"SUP"},
    "top_n_category": {"SUP"},
    "offset": {"SUP"},
    "grouped_top_n": {"SUP", "GRP"},
    "grouped_aggregation": {"GRP"},
    "scalar_count": {"CNT"},
    "negation": {"NEG"},
    "negation_ask": {"NEG"},
    "nested_subquery": {"NST"},
}


def _analytic_families(question: str) -> set[str]:
    """Query-shape families a question's wording signals (empty => non-analytic)."""
    q = question.lower()
    fams: set[str] = set()
    if re.search(r"how many|number of", q):
        fams.add("CNT")
    if re.search(r"\bno\b|without|have no|not .*(manage|assigned)|no .*(manager|assigned)", q):
        fams.add("NEG")
    if re.search(
        r"highest|lowest|cheapest|most expensive|heaviest|lightest|smallest|largest"
        r"|\btop\b|best|densest|most reliable|maximum|minimum",
        q,
    ):
        fams.add("SUP")
    if re.search(r"\bper \b|for each|\beach \b|average|avg| top \d+ .*(by|over|per)|most .*among", q):
        fams.add("GRP")
    if re.search(r"percentage|percentile|top 10 ?%|more than the average|highest average", q):
        fams.update({"NST", "GRP"})
    return fams


class _ShapeAwareFewShotIndex(FewShotIndex):
    """Wraps a built ``FewShotIndex``; overrides ``retrieve`` to guarantee a
    shape-appropriate exemplar for analytic questions. ``format_prompt_section``
    (inherited) calls ``retrieve``, so the engine picks this up unchanged."""

    def __init__(self, inner: FewShotIndex, shape_by_question: dict[str, str]) -> None:
        super().__init__(inner.retriever, inner.examples)
        self._inner = inner
        self._shape_by_q = shape_by_question

    def _fam(self, example_question: str) -> set[str]:
        return _SHAPE_FAMILY.get(self._shape_by_q.get(example_question, ""), set())

    def retrieve(self, question: str, k: int = 3) -> list[tuple[str, str]]:
        base = self._inner.retrieve(question, k)
        need = _analytic_families(question)
        if not need or any(self._fam(nl) & need for nl, _q in base):
            return base
        # None of the top-k match the needed shape family; pull a larger pool and
        # swap the best same-family exemplar into the last slot.
        pool = self._inner.retrieve(question, max(k * 4, 12))
        for nl, query in pool:
            if self._fam(nl) & need:
                return list(base[: max(k - 1, 0)]) + [(nl, query)]
        return base


@dataclass
class Arm:
    """One config's loaded corpus and seams, built once, for any number of runs.

    ``run()`` drives each case once; ``sample_vote.py`` drives each case N
    times through the same arm. Both build pipelines with :meth:`pipeline`.
    """

    name: str
    config: dict[str, Any]
    cases: list[dict[str, Any]]
    shared_ontology: str
    judge_name: str
    data_ttl: str | None
    max_repairs: int
    few_shot_k: int
    few_shot_index: FewShotIndex | None
    grounding_k: int
    grounding_index: Any
    predicate_k: int
    predicate_index: Any
    path_k: int
    path_index: Any

    def pipeline(
        self,
        case: dict[str, Any],
        client: LLMClient | None = None,
        *,
        postconditions: Sequence[Any] = (),
    ) -> NlPipeline:
        """One case's pipeline. ``postconditions`` reach the engine's
        semantic-check seam (W1b's execution feedback); none by default."""
        ontology_ttl = case.get("ontology", self.shared_ontology)
        return NlPipeline(
            client=client if client is not None else _client_for(self.config, case),
            resolver=SchemaResolver.from_turtle(ontology_ttl),
            ontology_ttl=ontology_ttl,
            max_repairs=self.max_repairs,
            postconditions=postconditions,
            few_shot_k=self.few_shot_k,
            few_shot_index=self.few_shot_index,
            grounding_k=self.grounding_k,
            grounding_index=self.grounding_index,
            predicate_k=self.predicate_k,
            predicate_index=self.predicate_index,
            path_k=self.path_k,
            path_index=self.path_index,
        )


def run(config_name: str) -> Report:
    arm = build_arm(config_name)
    cases: list[CaseResult] = []
    for case in arm.cases:
        pipeline = arm.pipeline(case)

        t0 = time.perf_counter()
        outcome = pipeline.run(case["nl"], params=case.get("params"))
        elapsed_ms = (time.perf_counter() - t0) * 1000

        passed, judge_note = _judge(arm.judge_name, case, outcome, arm.data_ttl)
        cases.append(
            CaseResult(
                name=case["name"],
                expected=case["expected"],
                actual=outcome.sparql,
                passed=passed,
                elapsed_ms=elapsed_ms,
                judge_note=judge_note,
            )
        )

    return Report(config=config_name, cases=cases)


def build_arm(config_name: str) -> Arm:
    configs = _load_configs()["configs"]
    config = configs[config_name]
    # Additive `corpus:` config-key read (07.1-01 / RESEARCH Pattern 1):
    # absent `corpus:` == today's corpus.yml, byte-identical for every
    # existing config (NL-BENCH-05). This is the ONE additive change this
    # phase makes to run() — everything below this line is unchanged.
    corpus_path = EVAL_DIR / config.get("corpus", "corpus.yml")
    corpus = _load_corpus(corpus_path)
    shared_ontology = corpus.get("ontology", "")
    judge_name = config.get("judge", "canonical")
    max_repairs = config.get("max_repairs", 2)

    # Additive `data_path:` config-key read (RESEARCH Pitfall 1/5): a
    # corpus-level instance-graph file, resolved relative to the corpus
    # file's directory and read ONCE (not per-case) — mirrors the existing
    # `shared_ontology` pattern above, but for a file path rather than
    # inline text. Absent `data_path` == `data_ttl` stays `None`, the
    # execution-judge guard in `_judge` never fires, and every existing
    # config's behavior stays byte-identical.
    data_path = corpus.get("data_path")
    data_ttl: str | None = None
    if data_path:
        data_ttl = (corpus_path.parent / data_path).read_text()

    # Additive few_shot config read (Phase 7 07-04 / RESEARCH Pitfall 5):
    # `run(config_name) -> Report`'s signature and Report's shape stay
    # byte-identical; absent `few_shot:` == today's zero-shot behavior.
    few_shot_cfg = config.get("few_shot", {})
    few_shot_mode = few_shot_cfg.get("mode", "zero")
    few_shot_k = few_shot_cfg.get("k", 0)
    # Additive `few_shot.bank:` sub-key (07.5-05 / RESEARCH OQ-3): mirrors
    # the corpus:/data_path: additive precedent — an arm names its OWN bank
    # file (e.g. a generated query-first-synthetic bank) without
    # monkeypatching `BANK_PATH`. Absent `bank:` == today's curated
    # `fewshot_bank.yml`, byte-identical for every existing few_shot arm.
    few_shot_bank_path = EVAL_DIR / few_shot_cfg["bank"] if few_shot_cfg.get("bank") else BANK_PATH

    # Build the index ONCE per arm, outside the per-case loop (Pitfall 1 —
    # never per-case; a fresh FewShotIndex would reload the SentenceTransformer
    # model + re-embed the whole bank on every one of the 25 corpus cases).
    few_shot_index: FewShotIndex | None = None
    if few_shot_mode in ("dense", "bm25"):
        few_shot_index = cached_few_shot_index(str(few_shot_bank_path), few_shot_mode)
        if few_shot_mode == "dense":
            # D-06 belt-and-suspenders: a wrong-mode/degraded retriever must
            # never be silently filed as a dense number.
            assert isinstance(few_shot_index.retriever, DenseRetriever), (
                f"D-06 guard failed: config {config_name!r} requested mode='dense' "
                f"but the built index's retriever is {type(few_shot_index.retriever).__name__!r}, "
                "not DenseRetriever. This means sentence-transformers is not "
                "installed/importable (install `.[dense]` before running this arm) — "
                "never record this as a dense-mode measurement."
            )

        # Shape-aware retrieval (opt-in via `few_shot.shape_aware: true`):
        # guarantee a query-shape-appropriate exemplar for analytic questions on
        # top of lexical retrieval; non-analytic questions retrieve unchanged.
        if few_shot_cfg.get("shape_aware") and few_shot_index is not None:
            _bank = yaml.safe_load(Path(few_shot_bank_path).read_text())
            _bank_examples = _bank["examples"] if isinstance(_bank, dict) else _bank
            _shape_by_q = {e["question"]: e.get("shape", "") for e in _bank_examples}
            few_shot_index = _ShapeAwareFewShotIndex(few_shot_index, _shape_by_q)

    # Additive `grounding:` config read (07.3-05 / RESEARCH Pattern 3): entity/
    # instance grounding (seam 6) mirrors the `few_shot:` precedent exactly.
    # Absent `grounding:` == today's ungrounded behavior (`grounding_index=None`
    # is the honest no-op NlPipeline already understands). The global default
    # `label_predicates` MUST stay schema-agnostic (`rdfs:label`) so the
    # mechanism transfers to CDF unchanged — any dataset-specific predicate
    # lives ONLY in that config entry's `label_predicates:` list, never
    # hardcoded here (Pitfall 2).
    grounding_cfg = config.get("grounding", {})
    grounding_k = grounding_cfg.get("k", 0)
    grounding_index = None
    if grounding_cfg and data_ttl:
        # Build the LabelIndex ONCE here, outside the per-case loop below
        # (Pitfall 3 — never per-case; mirrors the few_shot_index build-once
        # block above). Imported function-locally so pyoxigraph stays off
        # runner.py's module import path (mirrors the existing lazy
        # `from tests.helpers.oxi import ...` pattern used elsewhere in this
        # file).
        from tests.nl2sparql.eval.grounding_index_builder import build_label_index

        label_predicates = grounding_cfg.get("label_predicates", ["rdfs:label"])
        # WR-01: `grounding.prefixes:` (optional) lets a corpus config
        # register/override prefix->IRI mappings for its own
        # `label_predicates:` entries without a code change to
        # grounding_index_builder.py — absent, `build_label_index` falls
        # back to its built-in CK25-shaped default.
        prefixes = grounding_cfg.get("prefixes")
        grounding_index = build_label_index(data_ttl, label_predicates, prefixes=prefixes)

    # Additive `predicate_grounding:` config read (Phase 07.4 seam 7 / RESEARCH
    # Pitfall 5): predicate/schema-convention grounding mirrors the `grounding:`
    # (seam 6) precedent exactly, EXCEPT the gate — this builds from the
    # corpus's TBox (`shared_ontology`, always present) not its instance graph
    # (`data_ttl`, CK25-only; QALD has no `data_path`). Absent
    # `predicate_grounding:` == today's ungrounded behavior (`predicate_index=
    # None` is the honest no-op NlPipeline already understands).
    predicate_cfg = config.get("predicate_grounding", {})
    predicate_k = predicate_cfg.get("k", 0)
    predicate_index = None
    if predicate_cfg and shared_ontology:
        # Build the PredicateIndex ONCE here, outside the per-case loop below
        # (same build-once discipline as few_shot_index/grounding_index above).
        # Imported function-locally so pyoxigraph stays off runner.py's module
        # import path (mirrors the grounding_index_builder import above).
        from tests.nl2sparql.eval.grounding_index_builder import build_predicate_index

        predicate_index = build_predicate_index(shared_ontology)

    # Additive `path_grounding:` config read (Phase 07.6 seam 8 / R3):
    # relationship-path grounding mirrors the `predicate_grounding:` (seam 7)
    # precedent exactly, including the gate — this builds from the corpus's
    # TBox (`shared_ontology`, always present) not its instance graph
    # (`data_ttl`, CK25-only; QALD has no `data_path`), since the class-
    # connectivity graph is TBox-only (domain/range + subClassOf). Absent
    # `path_grounding:` == today's ungrounded behavior (`path_index=None` is
    # the honest no-op NlPipeline already understands).
    path_cfg = config.get("path_grounding", {})
    path_k = path_cfg.get("k", 0)
    path_index = None
    if path_cfg and shared_ontology:
        # Build the ClassPathIndex ONCE here, outside the per-case loop below
        # (same build-once discipline as few_shot_index/grounding_index/
        # predicate_index above). Imported function-locally so pyoxigraph
        # stays off runner.py's module import path (mirrors the
        # grounding_index_builder import above).
        from tests.nl2sparql.eval.grounding_index_builder import build_path_index

        path_index = build_path_index(shared_ontology)

    return Arm(
        name=config_name,
        config=config,
        cases=corpus["cases"],
        shared_ontology=shared_ontology,
        judge_name=judge_name,
        data_ttl=data_ttl,
        max_repairs=max_repairs,
        few_shot_k=few_shot_k,
        few_shot_index=few_shot_index,
        grounding_k=grounding_k,
        grounding_index=grounding_index,
        predicate_k=predicate_k,
        predicate_index=predicate_index,
        path_k=path_k,
        path_index=path_index,
    )


# ---------------------------------------------------------------------------
# Paired-analysis helpers (B1) — the primary confirmatory signal.
#
# Pure-Python, no scipy: `paired_mcnemar` computes the EXACT two-sided
# McNemar test over the (b, c) discordant-pair counts between two Reports'
# per-case verdicts (aligned by case name — both arms MUST run the same 25
# cases); `bootstrap_paired_delta` resamples the shared case keys with
# replacement to report a 95% CI on the paired pass-rate delta. Neither
# function makes a network call or constructs a Report itself — they operate
# purely on the `{case_name: bool}` dicts a caller extracts from two Reports.
# ---------------------------------------------------------------------------


def _cases_as_dict(report_cases: dict[str, bool] | list[CaseResult]) -> dict[str, bool]:
    """Normalize either a raw ``{name: passed}`` dict or a ``Report.cases``
    list of :class:`CaseResult` into a ``{name: passed}`` dict."""
    if isinstance(report_cases, dict):
        return report_cases
    return {c.name: c.passed for c in report_cases}


def paired_mcnemar(
    zero_cases: dict[str, bool] | list[CaseResult],
    dense_cases: dict[str, bool] | list[CaseResult],
) -> tuple[int, int, float]:
    """Exact two-sided McNemar test over paired zero-shot vs dense verdicts.

    Returns ``(b, c, p_value)`` where ``b`` = count(zero False & dense True)
    (the "lift" flips) and ``c`` = count(zero True & dense False) (the
    "regression" flips). ``p_value`` is the exact binomial two-sided McNemar
    p-value: ``min(1.0, 2 * sum(C(n, i) for i in 0..min(b, c)) / 2**n)`` with
    ``n = b + c`` (returns ``1.0`` when ``n == 0`` — no discordant pairs means
    no evidence of a difference either way).

    Raises ``ValueError`` if the two case-verdict sets don't share identical
    keys — this guards against comparing misaligned arms (e.g. a dense run
    against a stale/partial zero-shot run over a different case subset).
    """
    zero = _cases_as_dict(zero_cases)
    dense = _cases_as_dict(dense_cases)
    if zero.keys() != dense.keys():
        raise ValueError(
            "paired_mcnemar requires zero_cases and dense_cases to share "
            f"identical keys; zero-only={set(zero) - set(dense)!r} "
            f"dense-only={set(dense) - set(zero)!r}"
        )
    b = sum(1 for name in zero if zero[name] is False and dense[name] is True)
    c = sum(1 for name in zero if zero[name] is True and dense[name] is False)
    n = b + c
    if n == 0:
        return b, c, 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1))
    p_value = min(1.0, 2 * tail / (2**n))
    return b, c, p_value


def bootstrap_paired_delta(
    zero_cases: dict[str, bool] | list[CaseResult],
    dense_cases: dict[str, bool] | list[CaseResult],
    iters: int = 10000,
    seed: int = 1234,
) -> tuple[float, float, float]:
    """Bootstrap CI on the paired pass-rate delta (dense - zero).

    Returns ``(delta, lo, hi)``: ``delta`` is the observed
    ``dense_pass_rate - zero_pass_rate`` over the shared case keys; ``lo``/
    ``hi`` are the 2.5th/97.5th percentile of the delta resampled (with
    replacement) ``iters`` times over the shared case keys, using a seeded
    ``random.Random`` for reproducibility.

    Raises ``ValueError`` if the two case-verdict sets don't share identical
    keys (same guard as :func:`paired_mcnemar`).
    """
    zero = _cases_as_dict(zero_cases)
    dense = _cases_as_dict(dense_cases)
    if zero.keys() != dense.keys():
        raise ValueError(
            "bootstrap_paired_delta requires zero_cases and dense_cases to "
            f"share identical keys; zero-only={set(zero) - set(dense)!r} "
            f"dense-only={set(dense) - set(zero)!r}"
        )
    names = sorted(zero.keys())
    n = len(names)
    if n == 0:
        return 0.0, 0.0, 0.0

    def _pass_rate(sample_names: list[str], verdicts: dict[str, bool]) -> float:
        return sum(1 for name in sample_names if verdicts[name]) / len(sample_names)

    observed_delta = _pass_rate(names, dense) - _pass_rate(names, zero)

    rng = random.Random(seed)
    deltas: list[float] = []
    for _ in range(iters):
        sample = [names[rng.randrange(n)] for _ in range(n)]
        deltas.append(_pass_rate(sample, dense) - _pass_rate(sample, zero))
    deltas.sort()
    lo_idx = max(0, int(0.025 * len(deltas)))
    hi_idx = min(len(deltas) - 1, int(0.975 * len(deltas)))
    return observed_delta, deltas[lo_idx], deltas[hi_idx]


# ---------------------------------------------------------------------------
# write_report() — JSON + Markdown under REPORTS_DIR (gitignored)
# ---------------------------------------------------------------------------


def write_report(report: Report, *, out_dir: Path = REPORTS_DIR) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{report.config}.json"
    md_path = out_dir / f"{report.config}.md"

    payload = {
        "config": report.config,
        "pass_rate": report.pass_rate,
        "cases": [
            {"name": c.name, "passed": c.passed, "elapsed_ms": c.elapsed_ms, "sparql": c.actual}
            for c in report.cases
        ],
    }
    json_path.write_text(json.dumps(payload, indent=2) + "\n")

    lines = [
        f"# NL->SPARQL eval report: `{report.config}`",
        "",
        f"**Pass rate:** {report.pass_rate:.3f} "
        f"({sum(1 for c in report.cases if c.passed)}/{len(report.cases)})",
        "",
        "| Case | Passed | Elapsed (ms) |",
        "|------|--------|---------------|",
    ]
    for c in report.cases:
        lines.append(f"| {c.name} | {'✓' if c.passed else '✗'} | {c.elapsed_ms:.1f} |")
    md_path.write_text("\n".join(lines) + "\n")

    return json_path, md_path
