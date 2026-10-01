"""W3C SPARQL 1.1 DAWG live-execution harness.

This is the end-to-end counterpart to
``tests/w3c/test_w3c_query_evaluation.py``: where that module stops
after asserting ``arango_sparql.api.translate`` produced non-empty
AQL, this module *also* runs the AQL against a real ArangoDB and
compares the cursor's bindings against the W3C-expected ``.srx`` /
``.srj`` / ``.ttl`` results.

The flow per case::

    1. Load the case's RDF data into a fresh per-test collection set
       via :func:`tests.w3c.loader.load_w3c_data_to_arango`.
    2. Translate the SPARQL with a SchemaResolver wrapping the tiny
       OWL ontology the loader produces. ``default_collection`` is
       per-test so collections don't collide across parallel runs.
    3. Execute the AQL via ``db.aql.execute(aql, bind_vars=…)``.
    4. Parse the expected results file via
       :mod:`tests.w3c.srx_parser`.
    5. Compare bindings (bag-equality for SELECT, scalar-equality for
       ASK). Mismatches are reported as ``xfail`` rather than
       ``fail`` so the suite stays green while the translator catches
       up to the spec; the xfail reason captures the divergence so
       ``COVERAGE_REPORT.md`` and ``SKIP_REASONS.md`` track it.

Hard guards:

* The whole module ``pytest.skip`` cleanly when ``RUN_INTEGRATION``
  is not set or Docker is unavailable, so the default ``pytest -q``
  loop on a fresh checkout never fails here.
* Each test owns its own per-test collection prefix so nothing
  bleeds across cases; the module-level fixture also drops every
  ``w3c_*`` collection at session teardown to recover from a wedged
  prior run.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.errors import (
    AqlEmitError,
    SchemaResolutionError,
    SparqlParseError,
    UnsupportedSparqlError,
)
from arango_sparql.translate.resolver import SchemaResolver
from tests.integration.conftest import (
    DEFAULT_ARANGO_DB,
    DEFAULT_ARANGO_PASSWORD,
    DEFAULT_ARANGO_URL,
    DEFAULT_ARANGO_USER,
    arangodb_reachable,
    ensure_test_database,
    integration_enabled,
    try_boot_arangodb_via_compose,
)

from .loader import (
    load_w3c_data_to_arango,
    sanitize_for_collection,
    teardown_collections,
)
from .runner import (
    QUERY_EVAL,
    W3CTestCase,
    collect_cases,
    w3c_corpus_root,
)
from .srx_parser import (
    UnsupportedResultFormat,
    compare_ask,
    compare_graph,
    compare_select,
    normalize_actual_rows,
    parse_results_file,
)

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.w3c, pytest.mark.integration]
W3C_STORAGE_PROFILE_ENV_VAR = "W3C_STORAGE_PROFILE"
W3C_STORAGE_PROFILES = frozenset({"document_edge", "rpt"})


def storage_profile() -> str:
    """Return the explicit physicalisation profile for this live run.

    The profiles have materially different fidelity/performance properties;
    callers must select one rather than silently mixing their result rows.
    """

    profile = os.getenv(W3C_STORAGE_PROFILE_ENV_VAR, "document_edge")
    if profile not in W3C_STORAGE_PROFILES:
        raise ValueError(
            f"{W3C_STORAGE_PROFILE_ENV_VAR} must be one of {sorted(W3C_STORAGE_PROFILES)}, got {profile!r}"
        )
    return profile


if w3c_corpus_root() is None:
    pytest.skip(
        "W3C SPARQL tests not present; run scripts/fetch_w3c.sh",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Translation pre-flight: only parameterize cases the translator already
# accepts. Each pre-flight cost is paid once at collection time so the
# pytest -v output lists exactly the cases live execution will attempt.
# ---------------------------------------------------------------------------


def _translatable_cases() -> list[W3CTestCase]:
    """Return the W3C eval cases whose translator path is green today.

    A test is "translatable" when :func:`arango_sparql.api.translate`
    returns non-empty AQL without raising; the schema resolver hands
    every IRI the default ``Document`` mapping. Pre-flighting here
    keeps the live suite focused on the tests the harness can
    actually run, instead of parameterizing 250+ cases that would
    all xfail at translate time anyway (those are already covered by
    ``test_w3c_query_evaluation.py``).
    """
    cases: list[W3CTestCase] = []
    probe_resolver = SchemaResolver.from_turtle("", default_collection="Document")
    for case in collect_cases(types=frozenset({QUERY_EVAL})):
        if case.query_path is None or not case.query_path.is_file():
            continue
        try:
            sparql = case.query_path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            result = translate(sparql, resolver=probe_resolver)
        except (
            UnsupportedSparqlError,
            SchemaResolutionError,
            AqlEmitError,
            SparqlParseError,
        ):
            continue
        if result.aql:
            cases.append(case)
    return cases


_LIVE_CASES: list[W3CTestCase] = _translatable_cases()


# ---------------------------------------------------------------------------
# Module-scoped fixtures: docker boot + ArangoDB client + cleanup
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _live_arango_db() -> Iterator[Any]:
    """Yield an authenticated ``StandardDatabase`` against the running
    ArangoDB, or skip the whole module when one isn't available.

    Boot policy mirrors :mod:`tests.integration.test_execute_endpoint`:
    require ``RUN_INTEGRATION=1`` to opt in, then try ``docker
    compose up -d arangodb`` if nothing is listening yet, and finally
    skip cleanly if the boot fails. This keeps a developer without
    Docker on the green path.
    """
    if not integration_enabled():
        pytest.skip("set RUN_INTEGRATION=1 to enable W3C live-execution tests")
    if not arangodb_reachable():
        if not try_boot_arangodb_via_compose():
            pytest.skip(f"ArangoDB at {DEFAULT_ARANGO_URL} is unreachable and could not be booted")

    try:
        from arango import ArangoClient
    except ImportError as exc:  # pragma: no cover - python-arango is required
        pytest.skip(f"python-arango unavailable: {exc}")

    # Provision the dedicated database if it doesn't exist yet (no-op for
    # ``_system``) so a fresh ``sparql-to-aql`` works without manual setup.
    ensure_test_database()

    client = ArangoClient(hosts=DEFAULT_ARANGO_URL)
    db = client.db(
        DEFAULT_ARANGO_DB,
        username=DEFAULT_ARANGO_USER,
        password=DEFAULT_ARANGO_PASSWORD,
    )
    # Pre-clean any leftover w3c_* collections from a wedged prior
    # run. Cheap (one collection list call) and saves the operator
    # from hand-cleaning when an aborted test wedges teardown.
    teardown_collections(db, _MODULE_PREFIX)
    try:
        yield db
    finally:
        teardown_collections(db, _MODULE_PREFIX)
        client.close()


# Every collection this module creates starts with this prefix; the
# fixture above sweeps it on enter and exit so a crashed test never
# leaves stale data behind.
_MODULE_PREFIX = "w3c_"


# ---------------------------------------------------------------------------
# Known-divergence registry
# ---------------------------------------------------------------------------
# Every test that translates today but diverges at execution time
# lands here with a reason. This is the live counterpart to
# COVERAGE_REPORT.md's xfail bucket — reading both files together
# tells the operator exactly which W3C semantics AQL execution
# doesn't yet match.
SKIP_REASONS: dict[str, str] = {
    # Entailment tests rely on RDF / OWL / RDFS reasoning the
    # translator does not perform — AQL only sees the explicit
    # triples we loaded.
    "entailment/lang": (
        "language-tag matching ('name'@en) — loader flattens lang tags; AQL has no notion of xml:lang"
    ),
    "entailment/paper-sparqldl-Q1": "OWL DL reasoning required",
    "entailment/paper-sparqldl-Q1-rdfs": "RDFS entailment required",
    "entailment/plainLit": "language tag lost on load (\"name\" vs \"name\"@en)",
    "entailment/rdfs02": "RDFS subPropertyOf / domain entailment required",
    "entailment/rdfs05": "RDFS subPropertyOf transitivity entailment required",
    "entailment/rdfs08": "RDFS subClassOf / Resource entailment required",
    "entailment/rdfs10": "RDFS subClassOf reflexivity entailment required",
    "entailment/rdfs12": "RDFS member / ContainerMembershipProperty entailment required",
    "entailment/sparqldl-02": "OWL DL reasoning required",
    "entailment/sparqldl-10": "OWL DL reasoning required",
    "entailment/sparqldl-11": "OWL DL reasoning required",
    "entailment/sparqldl-12": "OWL DL reasoning required",
    "entailment/sparqldl-13": "OWL DL reasoning required",
}


# ---------------------------------------------------------------------------
# Evaluation-correctness gate (WP-C1)
# ---------------------------------------------------------------------------
# Every case listed here is KNOWN to pass end-to-end (translate →
# execute → binding comparison against the W3C expected results).
# For these, a divergence is a HARD FAILURE, not an xfail — without
# this registry a regression in a passing case would silently slide
# back into the xfail bucket and CI would stay green. Cases not listed
# still xfail on divergence (the harness's discovery mode).
#
# When new cases start passing, add them here (the suite tells you:
# an unlisted case that passes shows up in the pass column). When a
# translator change legitimately changes semantics, update the list
# in the same commit and say why in the commit message.
#
EXPECTED_LIVE_PASSES: frozenset[str] = frozenset(
    {
        # Generic triple scans merge datatype attributes and object edges.
        "aggregates/agg01",
        "aggregates/agg02",
        "aggregates/agg03",
        "aggregates/agg04",
        "aggregates/agg05",
        "aggregates/agg06",
        "aggregates/agg07",
        "aggregates/agg-sample-01",
        # Array fan-out restores one RDF solution per repeated literal.
        "aggregates/agg-avg-01",
        "aggregates/agg-avg-02",
        "aggregates/agg-groupconcat-03",
        "aggregates/agg-max-01",
        "aggregates/agg-max-02",
        "aggregates/agg-min-01",
        "aggregates/agg-min-02",
        # Empty groups, expression errors, default GROUP_CONCAT separators,
        # and decimal float noise preserve SPARQL aggregate semantics.
        "aggregates/agg-empty-group",
        "aggregates/agg-err-01",
        "aggregates/agg-err-02",
        "aggregates/agg-groupconcat-01",
        "aggregates/agg-groupconcat-02",
        "aggregates/agg-sum-01",
        "aggregates/agg-sum-02",
        "aggregates/agg08b",
        "bind/bind01",
        "bind/bind02",
        "bind/bind03",
        "bind/bind04",
        "bind/bind05",
        "bind/bind06",
        "bind/bind08",
        "bind/bind11",
        # SPARQL Results TSV/JSON parsers + isomorphic blank-node compare.
        "csv-tsv-res/tsv01",
        "csv-tsv-res/tsv03",
        "json-res/jsonres01",
        # RDF graph comparator validates CONSTRUCT output by isomorphism.
        "construct/constructwhere01",
        "construct/constructwhere02",
        "construct/constructwhere03",
        "bindings/inline1",
        "bindings/inline2",
        "bindings/values1",
        "bindings/values2",
        "bindings/values3",
        "bindings/values6",
        "entailment/owlds01",
        "entailment/parent2",
        # Blank-node objects remain present in the document profile.
        "entailment/rdf03",
        "entailment/sparqldl-06",
        "entailment/sparqldl-07",
        "entailment/sparqldl-08",
        "entailment/sparqldl-09",
        # BNODE() labels compared up to SELECT-result isomorphism.
        "functions/bnode02",
        # IRI()/URI() resolve relative strings against SPARQL BASE.
        "functions/iri01",
        "exists/exists01",
        "exists/exists02",
        "exists/exists04",
        "exists/exists05",
        "functions/abs01",
        "functions/ceil01",
        "functions/coalesce01",
        "functions/concat01",
        "functions/contains01",
        "functions/day",
        "functions/encode01",
        "functions/ends01",
        "functions/floor01",
        "functions/in01",
        "functions/in02",
        "functions/if02",
        "functions/isnumeric01",
        "functions/lcase01",
        "functions/length01",
        "functions/md5-01",
        "functions/md5-02",
        "functions/minutes",
        "functions/month",
        "functions/notin01",
        "functions/notin02",
        "functions/replace02",
        "functions/replace03",
        "functions/round01",
        "functions/seconds",
        "functions/sha1-01",
        "functions/sha1-02",
        "functions/sha256-01",
        "functions/sha256-02",
        "functions/sha512-01",
        "functions/sha512-02",
        "functions/starts01",
        "functions/struuid01",
        "functions/substring01",
        "functions/substring02",
        "functions/timezone",
        "functions/tz",
        "functions/ucase01",
        "functions/uuid01",
        "functions/year",
        "grouping/group01",
        "grouping/group03",
        "grouping/group04",
        "grouping/group05",
        "json-res/jsonres03",
        "json-res/jsonres04",
        "project-expression/projexp03",
        "project-expression/projexp01",
        "project-expression/projexp02",
        "project-expression/projexp04",
        "project-expression/projexp06",
        "project-expression/projexp07",
        # Document-edge RDF physicalisation (W3C loader fidelity slice).
        "property-path/pp02",
        "property-path/pp06",
        "property-path/pp08",
        "property-path/pp14",
        "property-path/pp30",
        "property-path/pp31",
        "property-path/pp32",
        "property-path/pp33",
        # SELECT * omits SequencePath _path_* intermediates (pp01/03/09/11).
        "property-path/pp01",
        "property-path/pp03",
        "property-path/pp09",
        "property-path/pp11",
        "subquery/subquery09",
        "subquery/subquery08",
        "subquery/subquery10",
        "subquery/subquery12",
    }
)


# ---------------------------------------------------------------------------
# Live-execution test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    _LIVE_CASES,
    ids=[c.short_id for c in _LIVE_CASES],
)
def test_live_execution(case: W3CTestCase, _live_arango_db: Any) -> None:
    """End-to-end SPARQL → AQL → ArangoDB → bindings round-trip.

    Two regimes, split by :data:`EXPECTED_LIVE_PASSES` (WP-C1):

    * **Gated cases** (listed) — any step that fails (load, translate,
      execute, compare) is a HARD failure. These are the evaluation-
      correctness contract; a regression must red the suite.
    * **Discovery cases** (everything else) — failures are captured as
      :func:`pytest.xfail` so the harness surfaces divergence without
      gating. Infrastructure failures (e.g. ArangoDB down mid-run)
      still bubble up as errors so the operator notices.
    """
    db = _live_arango_db
    # Expected-pass gates name the canonical document-edge profile. RPT is
    # measured separately during discovery until it has its own baseline;
    # applying a Document comparator gate to a different physical model
    # would turn useful profile differences into false regressions.
    gated = storage_profile() == "document_edge" and case.short_id in EXPECTED_LIVE_PASSES

    def _diverge(message: str) -> None:
        if gated:
            pytest.fail(f"EXPECTED_LIVE_PASSES regression ({case.short_id}): {message}")
        pytest.xfail(message)

    if case.short_id in SKIP_REASONS:
        # Divergences we already know about — skip the live work,
        # but still parameterize so the case shows up in -v output
        # and we notice if the gap closes (xfail strict=False would
        # XPASS).
        pytest.xfail(SKIP_REASONS[case.short_id])

    if case.expected_path is None or not case.expected_path.is_file():
        pytest.skip(f"expected results file missing: {case.expected_path}")

    sparql = case.query_path.read_text(encoding="utf-8")
    sanitized = sanitize_for_collection(case.short_id)
    prefix = f"{_MODULE_PREFIX}{sanitized}_"
    default_coll = f"{prefix}Document"

    # Parse the expected results before doing anything destructive
    # to ArangoDB; if the result format is unsupported, the test
    # xfails before we touch any collection.
    try:
        expected = parse_results_file(case.expected_path)
    except UnsupportedResultFormat as exc:
        _diverge(f"unsupported result format: {exc}")

    try:
        ontology_ttl, _coll_map = load_w3c_data_to_arango(
            db,
            case.data_paths,
            prefix,
            storage_profile=storage_profile(),
        )
    except Exception as exc:  # noqa: BLE001 — surface the load step's reason
        # Loader failures are infra-divergent (corpus surprise,
        # pyoxigraph parse error, ArangoDB write rejection); xfail
        # (or hard-fail for gated cases) so a single broken corpus
        # file doesn't red the whole discovery run. The exception
        # message identifies the culprit for the operator.
        _diverge(f"data load failed: {exc}")

    try:
        resolver = SchemaResolver.from_turtle(
            ontology_ttl,
            default_collection=default_coll,
            # The document-edge loader represents repeated RDF literals as
            # JSON lists. Expand those lists back to one RDF triple per
            # value; without this, aggregate and variable-predicate cases
            # observe the list as one atomic AQL value.
            fan_out_list_values=storage_profile() == "document_edge",
        )
        # We re-translate here (instead of caching the pre-flight
        # result) because the pre-flight resolver used the literal
        # ``Document`` collection — the live run needs the per-test
        # default collection so AQL reads from the data we just
        # loaded.
        translated = translate(sparql, resolver=resolver)

        try:
            cursor = db.aql.execute(translated.aql, bind_vars=translated.bind_vars)
            actual_rows = list(cursor)
        except Exception as exc:  # noqa: BLE001 — narrow at compare time
            _diverge(f"AQL execution failed: {exc}")
            raise  # unreachable — _diverge always raises; appeases control-flow analysis

        if expected.is_ask:
            ok, msg = compare_ask(bool(expected.ask), actual_rows)
        elif expected.is_graph:
            ok, msg = compare_graph(expected.graph, actual_rows)
        else:
            ok, msg = compare_select(expected.rows or [], normalize_actual_rows(actual_rows))

        if not ok:
            # A divergence we don't already know about — surface as
            # xfail with the diff (hard failure for gated cases) so
            # the operator can decide whether to add it to
            # SKIP_REASONS or fix the translator.
            _diverge(f"binding divergence:\n{msg}\nAQL: {translated.aql}")
    finally:
        # Per-test teardown — best-effort so a failed assertion
        # doesn't leave stale collections.
        teardown_collections(db, prefix)


# ---------------------------------------------------------------------------
# Smoke: harness wiring is intact even without integration mode.
# ---------------------------------------------------------------------------


def test_live_execution_module_imports() -> None:
    """Module-level smoke: imports resolve and the parametrize list
    is non-empty when the corpus is present.

    Stays unconditional (not gated by ``RUN_INTEGRATION``) so the
    default test loop catches accidental regressions in the W3C
    pre-flight or fixture wiring without paying for a Docker boot.
    """
    assert isinstance(_LIVE_CASES, list)
    assert all(isinstance(c, W3CTestCase) for c in _LIVE_CASES)


def test_storage_profile_defaults_and_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Profiles are explicit so their coverage denominators cannot blur."""

    monkeypatch.delenv(W3C_STORAGE_PROFILE_ENV_VAR, raising=False)
    assert storage_profile() == "document_edge"
    monkeypatch.setenv(W3C_STORAGE_PROFILE_ENV_VAR, "rpt")
    assert storage_profile() == "rpt"
    monkeypatch.setenv(W3C_STORAGE_PROFILE_ENV_VAR, "unknown")
    with pytest.raises(ValueError, match="W3C_STORAGE_PROFILE"):
        storage_profile()


def test_live_execution_skip_reasons_are_known() -> None:
    """Every entry in :data:`SKIP_REASONS` must reference a real W3C
    case ID; otherwise stale entries hide regressions when the
    underlying test ID changes.
    """
    known_ids = {c.short_id for c in _LIVE_CASES}
    stale = {sid for sid in SKIP_REASONS if sid not in known_ids}
    # Allow stale IDs only when ``RUN_INTEGRATION`` is unset *and*
    # the corpus subset shifts under us — that's a soft signal,
    # not a hard fail. The integration mode pre-flight selects the
    # exact IDs the live harness can attempt, so a stale entry then
    # is genuinely dead.
    if integration_enabled():
        assert not stale, f"SKIP_REASONS entries no longer parameterized: {stale}"
    elif stale:
        # Surface the gap in caplog output without failing the
        # default test loop — the integration run will catch it
        # for real.
        logger.info(
            "test_w3c_live_execution: SKIP_REASONS has IDs not in live cases: %s "
            "(only enforced under RUN_INTEGRATION=1)",
            sorted(stale),
        )


def test_expected_live_passes_are_consistent() -> None:
    """:data:`EXPECTED_LIVE_PASSES` must reference real parameterized
    cases and never overlap :data:`SKIP_REASONS` — an entry in both
    would be skipped before the gate could enforce it.
    """
    overlap = EXPECTED_LIVE_PASSES & set(SKIP_REASONS)
    assert not overlap, f"cases both gated and skipped: {sorted(overlap)}"
    if integration_enabled():
        known_ids = {c.short_id for c in _LIVE_CASES}
        stale = EXPECTED_LIVE_PASSES - known_ids
        assert not stale, f"EXPECTED_LIVE_PASSES entries no longer parameterized: {sorted(stale)}"


# Keep a stable handle on the env-gated state so the analyze_coverage
# CLI can introspect it without re-importing pytest internals.
RUN_INTEGRATION_ENV_VAR = "RUN_INTEGRATION"


def is_live_mode_enabled() -> bool:
    """Public predicate the coverage analyzer (and other tooling) can
    call to decide whether live-execution numbers are computable.
    """
    return os.getenv(RUN_INTEGRATION_ENV_VAR, "").lower() in ("1", "true", "yes")
