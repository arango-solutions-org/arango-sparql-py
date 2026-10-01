#!/usr/bin/env python3
"""Translation-only W3C SPARQL 1.1 DAWG coverage analyzer.

Mirrors :mod:`references.arango_cypher_py.tests.tck.analyze_coverage` —
walks every manifest under ``tests/w3c/data/``, attempts to parse and
translate each test case, and prints a Markdown coverage table plus a
short list of the most common skip reasons.

Run::

    python tests/w3c/analyze_coverage.py
    python tests/w3c/analyze_coverage.py --write   # rewrite COVERAGE_REPORT.md

No live ArangoDB is required — this is the upper-bound translation
coverage. End-to-end coverage will be lower until the AQL executor
lands behind the ``integration`` marker.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import warnings
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

# rdflib emits noisy ``logger.warning`` lines about borderline-valid IRIs
# while parsing the W3C corpus (e.g. ``http://example/c:d\?``). They are
# not actionable here — quiet them so the Markdown output isn't polluted
# when ``--write`` is not used.
logging.getLogger("rdflib").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", category=UserWarning, module=r"rdflib(\..*)?")

# Allow ``python tests/w3c/analyze_coverage.py`` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from arango_sparql.api import translate
from arango_sparql.errors import (
    AqlEmitError,
    SchemaResolutionError,
    SparqlParseError,
    UnsupportedSparqlError,
)
from arango_sparql.translate.parser import parse_sparql
from arango_sparql.translate.resolver import SchemaResolver
from tests.w3c.runner import (
    NEG_SYNTAX_11,
    OUT_OF_SCOPE_TYPES,
    POS_SYNTAX_11,
    QUERY_EVAL,
    W3CTestCase,
    collect_cases,
    w3c_corpus_root,
)

# What the harness treats as a "pass":
#  - QueryEvaluationTest: translation produced non-empty AQL.
#  - PositiveSyntaxTest11: rdflib parsed the query.
#  - NegativeSyntaxTest11: parser raised SparqlParseError.
#
# Anything else is an XFAIL bucket (still counted, but tracked so the
# coverage report makes the gap visible).


@dataclass
class CategoryStats:
    total: int = 0
    passed: int = 0
    xfailed: int = 0
    failed: int = 0
    skipped: int = 0
    xfail_reasons: Counter = None  # type: ignore[assignment]
    xfail_ids: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.xfail_reasons is None:
            self.xfail_reasons = Counter()

    @property
    def coverage(self) -> float:
        return (self.passed / self.total * 100.0) if self.total else 0.0


def _empty_resolver() -> SchemaResolver:
    """Build a permissive :class:`SchemaResolver` for the translation-
    only coverage analyzer.

    The harness intentionally runs every query against an empty
    ontology — its purpose is to measure the *visitor*'s coverage,
    not the operator's schema-mapping discipline. The
    ``permissive_class_resolution=True`` flag lets unknown class IRIs
    degrade to the default ``Document`` collection rather than
    raising, mirroring how :meth:`SchemaResolver.resolve_property`
    already handles unmapped property IRIs. This converts the bulk
    of the historical ``schema`` XFAIL bucket into measurable
    translation PASSes; the ``algebra`` bucket continues to track
    real roadmap gaps.
    """
    return SchemaResolver.from_turtle(
        "",
        default_collection="Document",
        permissive_class_resolution=True,
    )


def _read(case: W3CTestCase) -> str | None:
    if case.query_path is None or not case.query_path.is_file():
        return None
    return case.query_path.read_text(encoding="utf-8")


# Synthetic out-of-scope category for query-evaluation tests that use the
# ``SERVICE`` keyword (SPARQL 1.1 Federated Query). Federation dispatches a
# sub-pattern to a *remote* SPARQL endpoint at run time; there is no AQL
# analog and it is deliberately excluded (docs/architecture/proposals/
# federation-entry-point.md). These live in the W3C ``QueryEvaluationTest``
# type, so — unlike Protocol / Service-Description / Update, which are
# distinct mf: types already excluded — they must be detected by content and
# lifted out of the query-evaluation denominator, the same way those sibling
# federation-adjacent suites already are. Handled honestly (a named
# out-of-scope row), never counted as a passing translation.
FEDERATION = "FederationTest"
_FEDERATION_REASON = (
    "SPARQL 1.1 Federated Query (`SERVICE`) dispatches to a remote endpoint "
    "at run time — no AQL analog; out of scope like Protocol / "
    "Service-Description (federation-entry-point.md)."
)


def _uses_service(query: str) -> bool:
    """``True`` iff *query* uses the ``SERVICE`` keyword (federation).

    Comment lines are stripped first so a ``# … SERVICE …`` note never
    triggers a false positive. This exactly matches the W3C ``service/``
    manifest (verified: 7 of 7, zero false positives across the 253
    query-evaluation cases).
    """
    return any(re.search(r"\bSERVICE\b", line.split("#", 1)[0]) for line in query.splitlines())


# Synthetic out-of-scope category for the TSV / JSON result-serialization test
# suites. Like the CSV suite — which the W3C manifest already types as
# ``mf:CSVResultFormatTest`` (in OUT_OF_SCOPE_TYPES) — these check the shape of
# the *serialized result document* (TSV / JSON), not the SPARQL→AQL translation:
# their queries are incidental vehicles. The translation-only harness cannot
# evaluate a serialized result anyway (it checks only that AQL was produced), so
# these belong with their CSV siblings. The W3C manifest merely reuses the
# ``QueryEvaluationTest`` mf: type for TSV/JSON (a typing inconsistency vs CSV),
# so they must be detected by suite and lifted out here. (Content negotiation /
# result serialization is a service-layer concern, PRD §3.2 — not the transpiler.)
RESULT_FORMAT = "ResultFormatTest"
_RESULT_FORMAT_SUITES = frozenset({"csv-tsv-res", "json-res"})
_RESULT_FORMAT_REASON = (
    "SPARQL result-serialization tests (TSV / JSON output) — the transpiler "
    "emits AQL, not result documents; out of scope like the CSV result-format "
    "tests (`mf:CSVResultFormatTest`) already are."
)


def _is_result_format(case: W3CTestCase) -> bool:
    """``True`` iff *case* is a TSV/JSON result-serialization test.

    Identified by its W3C suite (the ``csv-tsv-res`` / ``json-res`` manifests
    are wholly result-format suites — verified 7 of 7, every one named
    ``… Result Format``). The CSV members of ``csv-tsv-res`` are already
    excluded by their distinct ``mf:CSVResultFormatTest`` type; this catches
    the TSV/JSON members the manifest typed as ``QueryEvaluationTest``.
    """
    return case.manifest_path.parent.name in _RESULT_FORMAT_SUITES


def _classify_query_eval(case: W3CTestCase) -> tuple[str, str]:
    query = _read(case)
    if query is None:
        return "skipped", "missing query file"
    try:
        result = translate(query, resolver=_empty_resolver())
    except UnsupportedSparqlError as exc:
        return "xfailed", f"UnsupportedSparql: {_short(exc)}"
    except SchemaResolutionError as exc:
        return "xfailed", f"SchemaResolution: {_short(exc)}"
    except AqlEmitError as exc:
        return "xfailed", f"AqlEmit: {_short(exc)}"
    except SparqlParseError as exc:
        return "xfailed", f"SparqlParse: {_short(exc)}"
    if not result.aql:
        return "failed", "empty AQL"
    return "passed", ""


def _classify_positive_syntax(case: W3CTestCase) -> tuple[str, str]:
    query = _read(case)
    if query is None:
        return "skipped", "missing query file"
    try:
        parse_sparql(query)
    except SparqlParseError as exc:
        return "xfailed", f"rdflib parse failure: {_short(exc)}"
    return "passed", ""


def _classify_negative_syntax(case: W3CTestCase) -> tuple[str, str]:
    query = _read(case)
    if query is None:
        return "skipped", "missing query file"
    try:
        parse_sparql(query)
    except SparqlParseError:
        return "passed", ""
    return "xfailed", "rdflib accepted invalid query"


def _short(exc: Exception) -> str:
    s = str(exc)
    return s[:80] + ("..." if len(s) > 80 else "")


# ---------------------------------------------------------------------------
# XFAIL-reason → "implication bucket" classifier
# ---------------------------------------------------------------------------
#
# The same Markdown report has historically used a single "port the
# corresponding visitor method" implication line for *every* XFAIL
# bucket. That is misleading: roughly half of the W3C DAWG XFAILs the
# harness reports are not algebra gaps but artefacts of the fact that
# ``analyze_coverage`` runs every query against an EMPTY resolver
# (``SchemaResolver.from_turtle("", default_collection="Document")``).
# Queries that name owl:Restriction / owl:DatatypeProperty / arbitrary
# ad-hoc test classes therefore fail at schema-resolution time even
# though the visitor is perfectly capable of translating them when
# given the matching ontology.
#
# Categorising each reason as one of four buckets lets the PRD §13.5
# tracker — and the next person picking a slice — distinguish:
#
#   * ``algebra``  — real roadmap gap; porting a visitor method moves
#                    the W3C pass-count directly.
#   * ``schema``   — harness artefact; would pass against a populated
#                    resolver. Roadmap fix is either to enhance the
#                    harness (per-fixture mini-ontologies) or to
#                    accept these as a permanent measurement floor.
#   * ``rdflib``   — rdflib parser disagrees with the W3C grammar
#                    (negative-syntax tests). Out of our hands short
#                    of forking rdflib.
#   * ``other``    — uncategorised; surface for manual triage.
#
# Mapping is by substring match on the truncated reason string the
# Counter already keys on. The substrings are deliberately short and
# stable (they live in ``arango_sparql/errors.py`` and the visitor's
# ``UnsupportedSparqlError`` messages).


BUCKET_ALGEBRA = "algebra"
BUCKET_SCHEMA = "schema"
BUCKET_RDFLIB = "rdflib"
BUCKET_OTHER = "other"

_BUCKET_RULES: tuple[tuple[str, str], ...] = (
    # rdflib disagreements (negative-syntax XFAILs) come first because
    # the substring ``rdflib`` could otherwise be matched by a future
    # algebra error that mentions rdflib in its message.
    ("rdflib accepted invalid query", BUCKET_RDFLIB),
    ("rdflib parse failure", BUCKET_RDFLIB),
    # Schema-resolution failures are the empty-resolver artefacts —
    # the visitor never got to the algebra step.
    ("SchemaResolution:", BUCKET_SCHEMA),
    # Everything else under ``UnsupportedSparql:`` / ``AqlEmit:`` /
    # ``SparqlParse:`` is, by construction, a real algebra-or-emit
    # gap the roadmap can close.
    ("UnsupportedSparql:", BUCKET_ALGEBRA),
    ("AqlEmit:", BUCKET_ALGEBRA),
    ("SparqlParse:", BUCKET_ALGEBRA),
)


def _bucket(reason: str) -> str:
    """Classify an XFAIL reason string into one of four buckets.

    The mapping is intentionally substring-based and stable: every
    reason the harness emits is prefixed with the error class
    (``UnsupportedSparql:`` / ``SchemaResolution:`` / etc.) or — for
    negative-syntax tests — the fixed phrase ``rdflib accepted invalid
    query``. Adding a new error class downstream requires a one-line
    update here, surfaced by ``test_bucket_classifier``.
    """
    for needle, bucket in _BUCKET_RULES:
        if needle in reason:
            return bucket
    return BUCKET_OTHER


_BUCKET_IMPLICATION: dict[str, str] = {
    BUCKET_ALGEBRA: "port the corresponding visitor method",
    BUCKET_SCHEMA: (
        "real schema-resolution failure even under permissive mode "
        "(should be 0 — investigate any non-zero count)"
    ),
    BUCKET_RDFLIB: "rdflib parser disagreement; out of scope here",
    BUCKET_OTHER: "uncategorised — triage manually",
}


def _classify(case: W3CTestCase) -> tuple[str, str]:
    if case.test_type == QUERY_EVAL:
        if _is_result_format(case):
            # TSV/JSON result-serialization test — out of scope, like CSV.
            return "skipped", _RESULT_FORMAT_REASON
        query = _read(case)
        if query is not None and _uses_service(query):
            # Federation (SERVICE) — out of scope, not a translation gap.
            return "skipped", _FEDERATION_REASON
        return _classify_query_eval(case)
    if case.test_type == POS_SYNTAX_11:
        return _classify_positive_syntax(case)
    if case.test_type == NEG_SYNTAX_11:
        return _classify_negative_syntax(case)
    if case.test_type in OUT_OF_SCOPE_TYPES:
        return "skipped", f"out-of-scope test type: {case.test_type}"
    return "skipped", f"unknown test type: {case.test_type}"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _heading(title: str) -> list[str]:
    return ["", title, "=" * len(title)]


def _format_markdown(
    by_category: dict[str, CategoryStats],
    live_stats: CategoryStats | None = None,
    live_profile: str | None = None,
) -> str:
    lines: list[str] = []
    lines.append("# W3C SPARQL 1.1 DAWG coverage — measured")
    lines.append("")
    lines.append(
        "> Methodology: translation-only dry run "
        "(`python tests/w3c/analyze_coverage.py`). Each query is parsed "
        "and (for evaluation tests) handed to "
        "`arango_sparql.api.translate`. A scenario passes when:"
    )
    lines.append(">")
    lines.append("> * **Syntax (positive)** — `rdflib` accepts the query;")
    lines.append(
        "> * **Syntax (negative)** — `rdflib` raises a `SparqlParseError` (the test deliberately ill-formed);"
    )
    lines.append(
        "> * **Query evaluation** — the visitor produces non-empty AQL "
        "without raising `UnsupportedSparqlError`."
    )
    if live_stats is not None:
        profile_label = f" ({live_profile})" if live_profile else ""
        lines.append(
            "> * **Live execution** — the translated AQL was run against a "
            "real ArangoDB and the bindings matched the W3C-expected "
            "`.srx` results."
        )
        if live_profile:
            lines.append(
                f"> * **Live storage profile** — `{live_profile}`. "
                "Profiles are measured independently; their denominators "
                "must not be merged."
            )
    lines.append("")
    lines.append(
        "Query-evaluation coverage measures translation acceptance; live "
        "coverage separately measures storage and execution fidelity. "
        "The profiles below deliberately keep those signals distinct."
    )
    lines.append("")

    lines.append("## Headline numbers")
    lines.append("")
    lines.append("| Category | Total | Pass | Fail | Xfail | Skip | Coverage |")
    lines.append("| -------- | -----:| ----:| ----:| -----:| ----:| --------:|")
    order = [
        ("Syntax (positive)", POS_SYNTAX_11),
        ("Syntax (negative)", NEG_SYNTAX_11),
        ("Query evaluation", QUERY_EVAL),
    ]
    for label, key in order:
        stats = by_category.get(key, CategoryStats())
        lines.append(
            f"| {label} | {stats.total} | {stats.passed} | {stats.failed} | "
            f"{stats.xfailed} | {stats.skipped} | {stats.coverage:.1f}% |"
        )
    if live_stats is not None:
        # Live execution row always reports against the *translatable*
        # subset (the only set of cases the live harness can attempt),
        # so its denominator is intentionally smaller than the
        # Query-evaluation row's. Read it as "of the cases translation
        # accepts today, how many AQL-execute to the spec-correct
        # bindings".
        lines.append(
            f"| Live execution{profile_label} | {live_stats.total} | {live_stats.passed} | "
            f"{live_stats.failed} | {live_stats.xfailed} | {live_stats.skipped} | "
            f"{live_stats.coverage:.1f}% |"
        )
    lines.append("")

    out_keys = sorted(k for k in by_category if k in OUT_OF_SCOPE_TYPES)
    if out_keys or FEDERATION in by_category or RESULT_FORMAT in by_category:
        lines.append("## Out-of-scope test types (counted, not run)")
        lines.append("")
        lines.append("| Test type | Total | Reason |")
        lines.append("| --------- | -----:| ------ |")
        oos_reason = (
            "SPARQL 1.1 Update / Protocol / Service-Description / CSV "
            "result-format are not v0 targets — the transpiler ports query "
            "semantics first."
        )
        for key in out_keys:
            stats = by_category[key]
            lines.append(f"| `mf:{key}` | {stats.total} | {oos_reason} |")
        # Federation (SERVICE) is a QueryEvaluationTest by mf: type but a
        # federation feature by content — reported here, lifted out of the
        # query-evaluation denominator above.
        if FEDERATION in by_category:
            lines.append(
                f"| SPARQL Federated Query (`SERVICE`) | "
                f"{by_category[FEDERATION].total} | {_FEDERATION_REASON} |"
            )
        # TSV/JSON result-serialization tests — QueryEvaluationTest by mf: type
        # but result-format by content; reported here with their CSV siblings.
        if RESULT_FORMAT in by_category:
            lines.append(
                f"| TSV / JSON result format | {by_category[RESULT_FORMAT].total} | {_RESULT_FORMAT_REASON} |"
            )
        lines.append("")

    aggregate: Counter = Counter()
    for stats in by_category.values():
        aggregate.update(stats.xfail_reasons)

    # Per-bucket roll-up tells the reader at a glance how many of the
    # XFAILs are actionable roadmap items vs. harness artefacts. This
    # is the table the PRD §13.5 tracker reads from when planning the
    # next slice.
    bucket_totals: Counter = Counter()
    for reason, count in aggregate.items():
        bucket_totals[_bucket(reason)] += count

    lines.append("## XFAIL implication summary")
    lines.append("")
    lines.append(
        "Each XFAIL is bucketed by what fixing it would require — "
        "this distinguishes real roadmap gaps (``algebra``) from "
        "out-of-our-hands rdflib disagreements (``rdflib``). The "
        "translation-only harness runs every query against a permissive "
        "empty resolver (`SchemaResolver.from_turtle('', "
        "default_collection='Document', permissive_class_resolution="
        "True)`), so unknown class IRIs degrade to the default collection "
        "rather than masking algebra gaps behind schema XFAILs."
    )
    lines.append("")
    lines.append("| Bucket | Count | Implication |")
    lines.append("| ------ | -----:| ----------- |")
    for bucket in (BUCKET_ALGEBRA, BUCKET_SCHEMA, BUCKET_RDFLIB, BUCKET_OTHER):
        count = bucket_totals.get(bucket, 0)
        if count == 0 and bucket == BUCKET_OTHER:
            # Don't print an empty ``other`` row — it adds noise. The
            # other three buckets are always shown even at zero so the
            # column structure is stable across runs.
            continue
        lines.append(f"| `{bucket}` | {count} | {_BUCKET_IMPLICATION[bucket]} |")
    lines.append("")

    lines.append("## Top XFAIL reasons")
    lines.append("")
    lines.append("| Count | Bucket | Reason | Implication |")
    lines.append("| -----:| ------ | ------ | ----------- |")
    for reason, count in aggregate.most_common(15):
        bucket = _bucket(reason)
        implication = _BUCKET_IMPLICATION[bucket]
        lines.append(f"| {count} | `{bucket}` | `{reason}` | {implication} |")
    if not aggregate:
        lines.append("| _(none)_ |  |  |  |")
    lines.append("")

    if live_stats is not None and live_stats.xfail_reasons:
        # Live-execution divergences have very different implications
        # from translation-time xfails — they're spec-vs-AQL gaps the
        # operator needs to hand-verify, not "port a visitor method".
        # Surface them in their own table so the two are not visually
        # conflated in PR review.
        lines.append("## Live-execution divergences")
        lines.append("")
        lines.append("| Count | Test ID | Divergence reason |")
        lines.append("| -----:| ------- | ----------------- |")
        for reason, count in live_stats.xfail_reasons.most_common(20):
            lines.append(f"| {count} | _(see test)_ | `{reason}` |")
        lines.append("")

    lines.append("## How to reproduce")
    lines.append("")
    lines.append("```bash")
    lines.append("python tests/w3c/analyze_coverage.py            # print")
    lines.append("python tests/w3c/analyze_coverage.py --write    # update this file")
    lines.append("pytest -q tests/w3c -m w3c                      # full pytest run")
    lines.append(
        "RUN_INTEGRATION=1 python tests/w3c/analyze_coverage.py --live --profile document_edge --write"
    )
    lines.append("                                                # canonical live baseline")
    lines.append("RUN_INTEGRATION=1 python tests/w3c/analyze_coverage.py --live --profile rpt")
    lines.append("                                                # separate RPT discovery")
    lines.append("```")
    lines.append("")
    if live_stats is None:
        lines.append(
            "End-to-end (live ArangoDB) coverage is computed by re-running "
            "with `--live` after `RUN_INTEGRATION=1` is set; without it "
            "the live row is omitted so the report stays reproducible "
            "without Docker."
        )
    else:
        lines.append(
            "Live-execution numbers are scoped to the translatable subset "
            "(cases that the visitor accepts today). They surface AQL ↔ "
            "SPARQL semantic divergences caught against a real ArangoDB."
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def analyze() -> dict[str, CategoryStats]:
    if w3c_corpus_root() is None:
        print(
            "W3C corpus not on disk; run scripts/fetch_w3c.sh first.",
            file=sys.stderr,
        )
        sys.exit(2)

    by_category: dict[str, CategoryStats] = {}
    for case in collect_cases():
        status, reason = _classify(case)
        # Federation query-eval cases are tallied under their own synthetic
        # out-of-scope category so they leave the QueryEvaluationTest
        # denominator (mirrors how the distinct out-of-scope mf: types are
        # already separate categories).
        if reason == _FEDERATION_REASON:
            key = FEDERATION
        elif reason == _RESULT_FORMAT_REASON:
            key = RESULT_FORMAT
        else:
            key = case.test_type
        stats = by_category.setdefault(key, CategoryStats())
        stats.total += 1
        if status == "passed":
            stats.passed += 1
        elif status == "xfailed":
            stats.xfailed += 1
            stats.xfail_reasons[reason] += 1
        elif status == "failed":
            stats.failed += 1
        else:
            stats.skipped += 1
    return by_category


def analyze_live() -> CategoryStats:
    """Run the live-execution suite via pytest and tally pass / xfail.

    Defers to :mod:`tests.w3c.test_w3c_live_execution` so the live
    row uses *exactly* the same gating, fixtures, and divergence
    registry as the live-execution test does — there is no drift
    between "what the harness reports" and "what the live test
    asserts".

    Requires ``RUN_INTEGRATION=1``; when unset the function returns
    a stats object with ``total=0`` so the caller knows to skip the
    row.
    """
    stats = CategoryStats()

    # Late import keeps the live-execution path off the default
    # analyze() codepath — translation-only mode never pulls in
    # python-arango, docker helpers, or the SRX comparator.
    from tests.w3c.test_w3c_live_execution import (
        _LIVE_CASES,
        SKIP_REASONS,
        is_live_mode_enabled,
    )

    if not is_live_mode_enabled():
        return stats  # caller treats total=0 as "live mode disabled"

    stats.total = len(_LIVE_CASES)

    # Re-running pytest from inside a script is awkward; instead we
    # spawn it as a subprocess and parse its summary line. This
    # mirrors the methodology in arango-cypher-py's live-coverage
    # tooling: one source of truth (pytest) drives both CI and the
    # coverage report.
    import subprocess

    repo_root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="w3c-live-") as temp_dir:
        junit_path = Path(temp_dir) / "live-results.xml"
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit_path}",
            "tests/w3c/test_w3c_live_execution.py::test_live_execution",
            "-m",
            "w3c and integration",
        ]
        proc = subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            env=dict(os.environ),  # forward RUN_INTEGRATION + ARANGO_* env vars
        )
        summary = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            raise RuntimeError(f"live W3C pytest run failed; coverage cannot be reported:\n{summary[-4000:]}")

        # Pytest's compact summary line has the canonical counters: e.g.
        # ``2 passed, 36 xfailed in 4.21s``. The token immediately
        # before each counter name is its integer count.
        import re as _re

        def _scan(name: str) -> int:
            match = _re.search(rf"(\d+)\s+{name}\b", summary)
            return int(match.group(1)) if match else 0

        stats.passed = _scan("passed")
        stats.xfailed = _scan("xfailed")
        stats.failed = _scan("failed")
        stats.skipped = _scan("skipped")

        observed = stats.passed + stats.xfailed + stats.failed + stats.skipped
        if observed != stats.total:
            raise RuntimeError(
                "live W3C pytest summary reported "
                f"{observed} outcomes for {stats.total} W3C cases; "
                "refusing to publish an inconsistent coverage denominator"
            )

        stats.xfail_ids = parse_live_xfail_ids(junit_path)

    # Surface the divergence reasons we already know about so the
    # report's "Live-execution divergences" table has something to
    # render even when the operator can't run pytest in verbose
    # mode (CI shells frequently buffer test-by-test output).
    for reason in SKIP_REASONS.values():
        stats.xfail_reasons[reason] += 1

    return stats


LIVE_FAILURE_LABELS_PATH = Path(__file__).with_name("LIVE_FAILURE_LABELS.json")
LIVE_FAILURES_PATH = Path(__file__).with_name("LIVE_FAILURES.md")
LIVE_FAILURE_LABELS = (
    "needs inference",
    "language tags lost",
    "text stored the wrong way",
    "genuine bug",
)
_LIVE_TEST_NAME = re.compile(r"^test_live_execution\[(?P<short_id>.+)\]$")


def parse_live_xfail_ids(junit_path: Path) -> set[str]:
    """Return stable W3C IDs for pytest.xfail cases in a JUnit report."""
    try:
        root = ElementTree.parse(junit_path).getroot()
    except (ElementTree.ParseError, OSError) as exc:
        raise RuntimeError(f"could not parse live W3C JUnit report {junit_path}: {exc}") from exc

    observed: set[str] = set()
    for testcase in root.iter("testcase"):
        skipped = testcase.find("skipped")
        if skipped is None or skipped.get("type") != "pytest.xfail":
            continue
        name = testcase.get("name", "")
        match = _LIVE_TEST_NAME.fullmatch(name)
        if match is None:
            raise RuntimeError(f"unexpected xfail testcase name in live W3C JUnit report: {name!r}")
        short_id = match.group("short_id")
        if short_id in observed:
            raise RuntimeError(f"duplicate live W3C xfail ID in JUnit report: {short_id}")
        observed.add(short_id)
    return observed


def load_live_failure_registry(path: Path = LIVE_FAILURE_LABELS_PATH) -> dict[str, object]:
    """Load the human-reviewed live failure registry."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"could not load live failure registry {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("baseline"), dict):
        raise ValueError("live failure registry must contain a baseline object")
    if not isinstance(data.get("failures"), dict):
        raise ValueError("live failure registry must contain a failures object")
    return data


def validate_live_failure_join(
    observed_ids: set[str],
    failures: Mapping[str, object],
) -> Counter[str]:
    """Validate the observed-to-reviewed join and return label counts."""
    registry_ids = set(failures)
    missing = sorted(observed_ids - registry_ids)
    stale = sorted(registry_ids - observed_ids)
    problems: list[str] = []
    if missing:
        problems.append(f"unlabelled live xfail IDs: {', '.join(missing)}")
    if stale:
        problems.append(f"stale live failure registry IDs: {', '.join(stale)}")
    if problems:
        raise ValueError("; ".join(problems))

    counts: Counter[str] = Counter()
    for short_id, raw_entry in failures.items():
        if not isinstance(raw_entry, dict):
            raise ValueError(f"live failure entry {short_id} must be an object")
        label = raw_entry.get("label")
        diagnosis = raw_entry.get("diagnosis")
        if label not in LIVE_FAILURE_LABELS:
            raise ValueError(f"live failure entry {short_id} has invalid label: {label!r}")
        if not isinstance(diagnosis, str) or not diagnosis.strip() or "\n" in diagnosis:
            raise ValueError(f"live failure entry {short_id} must have a nonempty one-line diagnosis")
        counts[label] += 1
    return counts


def validate_live_failure_baseline(
    stats: CategoryStats,
    registry: Mapping[str, object],
    profile: str,
) -> Counter[str]:
    """Fail closed when live outcomes differ from the reviewed baseline."""
    baseline = registry["baseline"]
    failures = registry["failures"]
    if not isinstance(baseline, dict) or not isinstance(failures, dict):
        raise ValueError("live failure registry baseline and failures must be objects")

    expected = {
        "profile": profile,
        "total": stats.total,
        "passed": stats.passed,
        "xfailed": stats.xfailed,
    }
    mismatches = [
        f"{key}: registry={baseline.get(key)!r}, observed={value!r}"
        for key, value in expected.items()
        if baseline.get(key) != value
    ]
    if stats.failed or stats.skipped:
        mismatches.append(f"failed={stats.failed}, skipped={stats.skipped}")
    if mismatches:
        raise ValueError("live failure baseline mismatch: " + "; ".join(mismatches))
    return validate_live_failure_join(stats.xfail_ids, failures)


def render_live_failures(registry: Mapping[str, object], counts: Counter[str]) -> str:
    """Render the standalone reviewed live failure report."""
    baseline = registry["baseline"]
    failures = registry["failures"]
    if not isinstance(baseline, dict) or not isinstance(failures, dict):
        raise ValueError("live failure registry baseline and failures must be objects")

    lines = [
        "# W3C live failure classifications",
        "",
        "> Generated file. Edit `LIVE_FAILURE_LABELS.json`, then run the regeneration command below.",
        "",
        "## Baseline",
        "",
        f"- Commit: `{baseline['commit']}`",
        f"- Storage profile: `{baseline['profile']}`",
        f"- Passing: `{baseline['passed']}/{baseline['total']}`",
        f"- Xfailed: `{baseline['xfailed']}`",
        "",
        "## Label summary",
        "",
        "| Label | Count |",
        "| ----- | ----: |",
    ]
    for label in LIVE_FAILURE_LABELS:
        lines.append(f"| {label} | {counts[label]} |")

    lines.extend(
        [
            "",
            "## Classified failures",
            "",
            "| Test ID | Label | Diagnosis |",
            "| ------- | ----- | --------- |",
        ]
    )
    for short_id in sorted(failures):
        entry = failures[short_id]
        if not isinstance(entry, dict):
            raise ValueError(f"live failure entry {short_id} must be an object")
        diagnosis = str(entry["diagnosis"]).replace("|", "\\|")
        lines.append(f"| `{short_id}` | {entry['label']} | {diagnosis} |")

    lines.extend(
        [
            "",
            "## Regeneration",
            "",
            "```bash",
            "RUN_INTEGRATION=1 W3C_STORAGE_PROFILE=document_edge \\",
            "  uv run python tests/w3c/analyze_coverage.py \\",
            "  --live --profile document_edge --write-live-failures",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def write_live_failures(stats: CategoryStats, profile: str) -> Counter[str]:
    """Validate and write the reviewed live failure report."""
    registry = load_live_failure_registry()
    counts = validate_live_failure_baseline(stats, registry, profile)
    LIVE_FAILURES_PATH.write_text(render_live_failures(registry, counts), encoding="utf-8")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument(
        "--write",
        action="store_true",
        help="overwrite tests/w3c/COVERAGE_REPORT.md with the latest numbers",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "include a Live-execution row sourced from "
            "tests/w3c/test_w3c_live_execution.py. Requires "
            "RUN_INTEGRATION=1; when unset the live row is omitted."
        ),
    )
    parser.add_argument(
        "--profile",
        choices=("document_edge", "rpt"),
        default="document_edge",
        help=(
            "physical RDF profile for --live. Profiles are reported "
            "separately; do not mix their denominators."
        ),
    )
    parser.add_argument(
        "--write-live-failures",
        action="store_true",
        help="validate LIVE_FAILURE_LABELS.json against --live and write LIVE_FAILURES.md",
    )
    args = parser.parse_args()

    if args.write and args.write_live_failures:
        parser.error("--write cannot be used with --write-live-failures")
    if args.write_live_failures and not args.live:
        parser.error("--write-live-failures requires --live")
    if args.write_live_failures and args.profile != "document_edge":
        parser.error("--write-live-failures supports only the document_edge profile")

    by_category = analyze()
    live_stats: CategoryStats | None = None
    live_profile: str | None = None
    if args.live:
        os.environ["W3C_STORAGE_PROFILE"] = args.profile
        live_stats = analyze_live()
        if live_stats.total == 0:
            # The flag was passed but the gate is closed — emit an
            # informational note rather than a silent omission so the
            # operator notices.
            print(
                "--live requested but RUN_INTEGRATION is unset; live row omitted.",
                file=sys.stderr,
            )
            live_stats = None
        else:
            live_profile = args.profile

    report = _format_markdown(
        by_category,
        live_stats=live_stats,
        live_profile=live_profile,
    )

    if args.write:
        out_path = Path(__file__).parent / "COVERAGE_REPORT.md"
        out_path.write_text(report, encoding="utf-8")
        print(f"wrote {out_path}")
    elif args.write_live_failures:
        if live_stats is None or live_profile is None:
            parser.error("--write-live-failures requires RUN_INTEGRATION=1")
        counts = write_live_failures(live_stats, live_profile)
        print(f"live baseline: {live_stats.passed}/{live_stats.total} passing")
        print(f"live xfails: {live_stats.xfailed}")
        for label in LIVE_FAILURE_LABELS:
            print(f"{label}: {counts[label]}")
        print(f"wrote {LIVE_FAILURES_PATH}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
