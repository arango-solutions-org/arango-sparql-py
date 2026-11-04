"""Leakage gate for the curated few-shot bank (Phase 7 Plan 02, D-01/D-02/B2).

``fewshot_bank.yml`` supplies the question -> gold-SPARQL exemplars the
dense/BM25 few-shot retrievers rank over. This module is the committed,
after-the-fact proof that the bank does NOT contaminate the held-out eval
``corpus.yml``:

1. ``test_bank_disjoint_from_eval_corpus`` -- THREE-way disjointness:
   normalized question text, canonical algebra (alpha-equivalence aware, via
   the existing ``runner._canonical`` judge), and a canonical-algebra
   SKELETON (concrete literals/URIs abstracted) so neither a paraphrase, a
   re-spelled-but-equivalent gold, nor a numerically-nudged near-clone
   (``:age 30`` vs ``:age 40``) can smuggle a corpus case into the bank.
2. ``test_bank_similarity_ceiling`` -- a cosine SIMILARITY CEILING (< 0.95)
   using the same pinned embedding model the dense retriever uses, so a
   near-clone paraphrase can never clear the gate and hand the model a
   template. Skips (does not fail) when the dense stack is not installed --
   this plan is independent of 07-01/07-03's dense-stack sync.
3. ``test_every_bank_gold_parses`` -- a bank exemplar that cannot parse is a
   broken few-shot example.
4. ``test_bank_ontology_matches_corpus`` -- WARNING 4: the duplicated
   ``ontology:`` Turtle block cannot silently drift between the two files.

A collision surfaced by this gate is a signal to RE-AUTHOR the offending
bank item, never to nudge a literal to dodge the check (B2).

Key-free / mostly no-network: reuses ``runner._canonical``/``_load_corpus``
(the existing canonical-algebra judge) and mirrors ``test_gold_transpilable.py``'s
``pytest.mark.eval`` + ``RUN_EVAL`` skip idiom. Only the similarity-ceiling
test touches a (locally cached, pinned-revision) sentence-transformers model,
and it degrades to a skip rather than a hard failure when that stack is
absent.
"""

from __future__ import annotations

import os
import re
import statistics
from pathlib import Path
from typing import Any

import pytest
import yaml

from arango_sparql.nl2sparql.engine_adapter import _FEWSHOT_BANK_PATH
from tests.nl2sparql.eval.runner import _canonical, _load_corpus

# Same "off" semantics as test_eval.py: treat "", "0", "false", "no" as off.
_RUN_EVAL = os.getenv("RUN_EVAL", "").strip().lower() not in ("", "0", "false", "no")

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(not _RUN_EVAL, reason="set RUN_EVAL=1 to run the NL eval gate"),
]

EVAL_DIR = Path(__file__).parent
BANK_PATH = _FEWSHOT_BANK_PATH  # shipped in the package, where the engine reads it
REPORTS_DIR = EVAL_DIR / "reports"

# Cosine similarity ceiling (B2, committed literal): with all-MiniLM-L6-v2,
# genuine paraphrases / near-duplicate questions land at cosine >= 0.95. A
# nearest bank neighbour below this ceiling for EVERY eval-corpus question
# proves the bank item is a materially different question, not a reworded
# corpus clone that would leak a template through dense retrieval.
_SIMILARITY_CEILING = 0.95

_LITERAL_RE = re.compile(r"Literal\([^)]*\)")
_URI_RE = re.compile(r"(?:rdflib\.term\.)?URIRef\('[^']*'\)")


def _normalize_question(q: str) -> str:
    return re.sub(r"[^\w\s]", "", q.strip().lower())


def _skeleton(sparql: str) -> str | None:
    """Canonical algebra with concrete literals/URIs abstracted to placeholders.

    Builds on ``_canonical`` (already alpha-equivalence-aware over
    variables) and additionally regex-blanks every ``Literal(...)`` value to
    ``?LIT`` and every quoted ``URIRef('...')`` triple-position token
    (class/predicate references) to ``?URI``, so structure alone remains:
    ``:age 30`` and ``:age 40`` collapse to the SAME skeleton and must
    therefore be rejected as a collision.

    Property-path predicates (rdflib's ``Path`` objects, e.g.
    ``Path(<http://ex.org/knows> / <http://ex.org/name>)``) are NOT matched
    by the quoted ``URIRef('...')`` pattern, so property-path bank/corpus
    items keep their real predicate identity -- appropriate, since the
    bank's property-path examples deliberately reuse the same small set of
    graph edges (``:knows``/``:placed``) the corpus does, and only the
    concrete operator + predicate combination (not a blanked placeholder)
    distinguishes one path query from another.
    """
    canon = _canonical(sparql)
    if canon is None:
        return None
    canon = _LITERAL_RE.sub("?LIT", canon)
    canon = _URI_RE.sub("?URI", canon)
    return canon


def _load_bank() -> dict[str, Any]:
    return yaml.safe_load(BANK_PATH.read_text(encoding="utf-8")) or {}


def _bank_examples() -> list[dict[str, str]]:
    return _load_bank().get("examples", [])


def _positive_corpus_cases() -> list[dict[str, Any]]:
    """Every corpus case WITHOUT ``expect_refusal`` -- the only cases with
    gold SPARQL to compare against (refusal cases carry a rationale, not a
    gold query the bank could possibly leak)."""
    corpus = _load_corpus()
    return [c for c in corpus["cases"] if not c.get("expect_refusal")]


def test_bank_disjoint_from_eval_corpus() -> None:
    """The bank shares NO case with the eval corpus, proven THREE ways.

    (1) normalized question text, (2) canonical algebra (alpha-equivalence
    aware), and (3) canonical-algebra SKELETON (literals/URIs abstracted) --
    so neither a paraphrased question, a re-spelled-but-equivalent gold, nor
    a numerically-nudged near-clone can smuggle a corpus case into the
    retrieval pool (D-02, B2).
    """
    corpus_cases = _positive_corpus_cases()
    bank = _bank_examples()

    corpus_questions = {_normalize_question(c["nl"]) for c in corpus_cases}
    bank_questions = {_normalize_question(e["question"]) for e in bank}
    overlap_q = corpus_questions & bank_questions
    assert not overlap_q, f"bank questions overlap eval corpus (normalized text): {overlap_q}"

    corpus_canon = {_canonical(c["expected"]) for c in corpus_cases}
    corpus_canon.discard(None)
    bank_canon = {_canonical(e["query"]) for e in bank}
    bank_canon.discard(None)
    overlap_c = corpus_canon & bank_canon
    assert not overlap_c, f"bank gold SPARQL overlaps eval corpus (canonical algebra): {overlap_c}"

    corpus_skel = {_skeleton(c["expected"]) for c in corpus_cases}
    corpus_skel.discard(None)
    bank_skel = {_skeleton(e["query"]) for e in bank}
    bank_skel.discard(None)
    overlap_s = corpus_skel & bank_skel
    assert not overlap_s, (
        "bank gold SPARQL overlaps eval corpus at the SKELETON level "
        f"(literals/URIs abstracted -- e.g. `:age 30` vs `:age 40`): {overlap_s}"
    )


def test_every_bank_gold_parses() -> None:
    """A bank exemplar that cannot parse is a broken few-shot example."""
    for example in _bank_examples():
        assert _canonical(example["query"]) is not None, (
            f"bank gold for question {example['question']!r} does not parse via rdflib"
        )


def test_bank_ontology_matches_corpus() -> None:
    """WARNING 4: the duplicated ``ontology:`` Turtle block cannot silently drift."""
    corpus = _load_corpus()
    bank = _load_bank()
    assert bank.get("ontology") == corpus.get("ontology"), (
        "fewshot_bank.yml's `ontology:` block has drifted from corpus.yml's -- "
        "keep them byte-identical (WARNING 4)"
    )


def test_bank_similarity_ceiling() -> None:
    """B2 near-clone gate: no eval-corpus question sits too close to any bank item.

    Skips (does not fail) when the dense stack (``sentence-transformers`` +
    the 07-01 pinned embedding model constants in
    ``arango_query_core.nl.fewshot``) is not importable -- this plan is
    independent of the engine work (07-01) and 07-03's ``uv sync --extra
    dense``; the ceiling becomes an ACTIVE gate from Wave 2 onward, at the
    latest before the 07-04 lift is recorded.

    Also RECORDS the nearest-neighbor bank<->corpus similarity distribution
    (min/median/max cosine + top-5 closest pairs) to stdout and to a
    gitignored ``reports/fewshot_similarity.md`` so a reviewer can rule out
    memorization -- Plan 04's sweep report surfaces this distribution
    alongside the measured lift.
    """
    try:
        from arango_query_core.nl.fewshot import (
            DEFAULT_DENSE_MODEL_ID,
            DEFAULT_DENSE_REVISION,
        )
        from sentence_transformers import SentenceTransformer
    except ImportError:
        pytest.skip(
            "dense stack not installed (sentence-transformers / "
            "arango_query_core.nl.fewshot) -- similarity ceiling becomes "
            "active once the dense extra is synced (07-03)"
        )

    corpus_questions = [c["nl"] for c in _positive_corpus_cases()]
    bank_questions = [e["question"] for e in _bank_examples()]

    model = SentenceTransformer(DEFAULT_DENSE_MODEL_ID, revision=DEFAULT_DENSE_REVISION)
    corpus_emb = model.encode(corpus_questions, normalize_embeddings=True)
    bank_emb = model.encode(bank_questions, normalize_embeddings=True)

    nearest: list[tuple[float, str, str]] = []
    for cq, cvec in zip(corpus_questions, corpus_emb, strict=True):
        scores = bank_emb @ cvec
        best_idx = int(scores.argmax())
        nearest.append((float(scores[best_idx]), cq, bank_questions[best_idx]))

    cosines = [n[0] for n in nearest]
    max_cos = max(cosines)
    min_cos = min(cosines)
    median_cos = statistics.median(cosines)
    closest = sorted(nearest, key=lambda n: -n[0])[:5]

    print(
        f"\nNearest-neighbor bank<->corpus cosine distribution: "
        f"min={min_cos:.4f} median={median_cos:.4f} max={max_cos:.4f} "
        f"ceiling={_SIMILARITY_CEILING}"
    )
    print("Top-5 closest (cosine, corpus question, bank question) pairs:")
    for cos, cq, bq in closest:
        print(f"  {cos:.4f}  corpus={cq!r}  bank={bq!r}")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_lines = [
        "# fewshot_bank.yml <-> corpus.yml nearest-neighbor similarity",
        "",
        f"min={min_cos:.4f} median={median_cos:.4f} max={max_cos:.4f} ceiling={_SIMILARITY_CEILING}",
        "",
        "| cosine | corpus question | nearest bank question |",
        "|---|---|---|",
    ]
    for cos, cq, bq in closest:
        report_lines.append(f"| {cos:.4f} | {cq} | {bq} |")
    (REPORTS_DIR / "fewshot_similarity.md").write_text("\n".join(report_lines) + "\n")

    assert max_cos < _SIMILARITY_CEILING, (
        f"a bank item sits too close (cosine={max_cos:.4f} >= {_SIMILARITY_CEILING}) "
        f"to an eval-corpus question -- possible near-clone leakage: {closest[0]}"
    )


# ---------------------------------------------------------------------------
# Generated-bank overlap audit (Phase 07.5 Plan 05, D-05/REQ-6) -- extends
# the curated-bank disjointness gate above to the GENERATED (query-first
# synthetic) banks. Two DISTINCT axes, per RESEARCH's "Measurement &
# Overlap-Audit Design":
#
#   1. SHAPE overlap -- reuses `_skeleton` (this module's own canonical-
#      algebra-with-abstracted-literals/URIs helper, the seed for this axis
#      per RESEARCH): a generated example whose skeleton equals a held-out
#      gold's skeleton is a near-duplicate-SHAPE. This axis is EXPECTED to
#      be non-trivial by design -- the generator's whole purpose is
#      producing SAME-shape examples for BM25 retrieval -- so it is
#      REPORTED (indices exported for exclusion), never asserted empty.
#   2. ENTITY overlap (new, D-04/D-05 leakage axis, Pitfall 4) -- literal
#      fillers appearing in BOTH the generated bank and the held-out gold
#      queries. Unlike shape overlap, entity-level leakage is a genuine
#      contamination risk (the model could see the SAME real-world name a
#      held-out question is grounded in).
#
# Both helpers are importable so `run_generated_sweep.py` (Plan 05 Task 2)
# computes the overlap-EXCLUDED delta from this exact source of truth,
# never a re-derived copy (REQ-6).
# ---------------------------------------------------------------------------

GENERATED_CK25_BANK_PATH = EVAL_DIR / "vendored" / "ck25" / "generated_fewshot_bank.yml"
GENERATED_QALD_BANK_PATH = EVAL_DIR / "vendored" / "qald9plus" / "generated_fewshot_bank.yml"
CK25_CORPUS_PATH = EVAL_DIR / "vendored" / "ck25" / "corpus.yml"
QALD_CORPUS_PATH = EVAL_DIR / "vendored" / "qald9plus" / "corpus.yml"

_QUOTED_LITERAL_RE = re.compile(r'"([^"]*)"')


def _load_generated_bank(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    examples = data.get("examples", [])
    return examples if isinstance(examples, list) else []


def _positive_cases_for(corpus_path: Path) -> list[dict[str, Any]]:
    """Every non-refusal case from an arbitrary (ck25/qald9plus) corpus
    file -- the generalized form of ``_positive_corpus_cases`` (which is
    pinned to the curated bank's own ``corpus.yml``)."""
    corpus = _load_corpus(corpus_path)
    return [c for c in corpus["cases"] if not c.get("expect_refusal")]


def _query_literals(sparql: str) -> set[str]:
    """Quoted string literal fillers appearing in *sparql*'s raw text --
    name-anchor values the generator injects (``rdfs:label "Marketing"``)
    or a corpus gold's own literal FILTER/binding value
    (``pv:addressCountry "France"``). The entity-level axis of the overlap
    audit (D-04/D-05, Pitfall 4)."""
    return set(_QUOTED_LITERAL_RE.findall(sparql))


def shape_overlap_indices(bank: list[dict[str, Any]], corpus: list[dict[str, Any]]) -> set[int]:
    """Indices into *bank* whose canonical-algebra SKELETON (``_skeleton``,
    literals/URIs abstracted) collides with ANY held-out *corpus* gold's
    skeleton -- a near-duplicate-SHAPE example. Exported for the sweep
    harness to compute the shape-overlap-excluded delta (REQ-6); this axis
    is expected to be non-trivial by design (BM25 retrieval WANTS
    same-shape examples) and is never asserted empty.
    """
    corpus_skeletons = {_skeleton(c["expected"]) for c in corpus}
    corpus_skeletons.discard(None)
    overlap: set[int] = set()
    for i, example in enumerate(bank):
        skel = _skeleton(example["query"])
        if skel is not None and skel in corpus_skeletons:
            overlap.add(i)
    return overlap


def entity_overlap(bank: list[dict[str, Any]], corpus: list[dict[str, Any]]) -> set[str]:
    """Literal fillers appearing in BOTH the generated *bank* and the
    held-out *corpus* golds -- the entity-level leakage axis (D-04/D-05,
    Pitfall 4). A real-world label collision is still possible even on a
    leakage-conscious generator (there is only one "Marketing" department
    in the company) and, when found, is reported here (never silently
    dropped) so the sweep can exclude the affected held-out case from the
    measured delta.
    """
    bank_entities: set[str] = set()
    for example in bank:
        bank_entities |= _query_literals(example["query"])
    corpus_entities: set[str] = set()
    for case in corpus:
        corpus_entities |= _query_literals(case["expected"])
    return bank_entities & corpus_entities


def overlapping_case_names(bank: list[dict[str, Any]], corpus: list[dict[str, Any]]) -> set[str]:
    """Held-out *corpus* case NAMES that collide with the generated *bank*
    on EITHER axis (shape skeleton OR shared entity literal) -- the
    case-indexed view ``run_generated_sweep.py`` needs to exclude cases
    from the paired McNemar/bootstrap delta (REQ-6 overlap-excluded
    delta), derived from the exact same ``shape_overlap_indices``/
    ``entity_overlap`` source of truth (never re-implemented).
    """
    bank_skeletons = {_skeleton(e["query"]) for e in bank}
    bank_skeletons.discard(None)
    bank_entities: set[str] = set()
    for example in bank:
        bank_entities |= _query_literals(example["query"])

    names: set[str] = set()
    for case in corpus:
        skel = _skeleton(case["expected"])
        if skel is not None and skel in bank_skeletons:
            names.add(case["name"])
            continue
        if _query_literals(case["expected"]) & bank_entities:
            names.add(case["name"])
    return names


def test_generated_ck25_bank_shape_overlap_reported() -> None:
    """SHAPE overlap axis over the GENERATED CK25 bank (D-05): report,
    don't gate -- a generated example sharing a held-out gold's skeleton
    is the expected, desired retrieval behavior (same-shape few-shot), not
    a defect. ``shape_overlap_indices`` must run cleanly and return valid
    bank indices so ``run_generated_sweep.py`` can exclude the
    corresponding held-out cases from the measured delta."""
    bank = _load_generated_bank(GENERATED_CK25_BANK_PATH)
    corpus = _positive_cases_for(CK25_CORPUS_PATH)
    overlap = shape_overlap_indices(bank, corpus)
    assert isinstance(overlap, set)
    assert all(isinstance(i, int) and 0 <= i < len(bank) for i in overlap)
    overlapping_names = overlapping_case_names(bank, corpus)
    assert isinstance(overlapping_names, set)
    print(
        f"\nCK25 generated-bank shape-overlap: {len(overlap)}/{len(bank)} bank "
        f"examples collide with {len(overlapping_names)}/{len(corpus)} held-out cases"
    )


def test_generated_ck25_bank_entity_overlap_pinned() -> None:
    """ENTITY overlap axis over the GENERATED CK25 bank (D-04/D-05,
    Pitfall 4). EMPIRICAL FINDING (Plan 05): the generator's seeded
    sampler (Plan 02, ``generate_bank_with_report(..., seed=0)``) draws
    filler labels from the FULL CK25 instance graph -- the SAME graph the
    held-out corpus's golds are grounded in -- so a real-world label can
    legitimately appear in both pools by sheer coincidence (there is
    exactly one "Marketing" department in the company; the generator's
    ``two_hop`` shape sampled it independently of ck25-4/ck25-10's own
    "Marketing" literal). This is precisely the scenario D-05's mitigation
    targets: REPORT the overlap and EXCLUDE the affected held-out case
    from the measured delta (never force a false ``== set()`` by mutating
    the committed, already regression-tested Plan 02/03 bank --
    ``test_committed_ck25_bank_matches_fresh_regeneration`` pins that
    artifact byte-for-byte). Pinned to the single known collision so the
    audit doubles as a regression gate: a NEW, larger collision surfacing
    on a future regeneration must be investigated, not silently absorbed.
    """
    bank = _load_generated_bank(GENERATED_CK25_BANK_PATH)
    corpus = _positive_cases_for(CK25_CORPUS_PATH)
    overlap = entity_overlap(bank, corpus)
    assert overlap == {"Marketing"}, (
        f"CK25 generated-bank entity-overlap axis drifted from the known "
        f"Plan-05 finding ({overlap!r}) -- investigate before excluding "
        "in run_generated_sweep.py; if this shrank to empty, tighten this "
        "assertion to `== set()`"
    )


def test_generated_qald_bank_overlap_axes_vacuously_empty() -> None:
    """QALD's generated bank is empty (0 examples -- Plan 04's honest,
    TBox-only finding); both overlap axes are therefore vacuously empty --
    an empty bank cannot near-duplicate or leak anything. Guards against a
    future QALD bank regeneration silently growing without this test
    covering it."""
    bank = _load_generated_bank(GENERATED_QALD_BANK_PATH)
    assert bank == []
    corpus = _positive_cases_for(QALD_CORPUS_PATH)
    assert shape_overlap_indices(bank, corpus) == set()
    assert entity_overlap(bank, corpus) == set()
    assert overlapping_case_names(bank, corpus) == set()
