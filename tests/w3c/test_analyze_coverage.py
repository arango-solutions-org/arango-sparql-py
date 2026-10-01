"""Unit tests for W3C coverage accounting."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from tests.w3c import analyze_coverage


def _completed(*, returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


# --- Federation (SERVICE) reclassification ---------------------------------


def test_uses_service_detects_federation_keyword() -> None:
    assert analyze_coverage._uses_service("SELECT * { SERVICE <http://x/> { ?s ?p ?o } }")
    assert analyze_coverage._uses_service("SELECT * {\n  SERVICE SILENT <http://x/> { ?s ?p ?o }\n}")


def test_uses_service_ignores_the_keyword_in_comments() -> None:
    # A '# ... SERVICE ...' note must not trip detection — only real use counts.
    assert not analyze_coverage._uses_service("# this query has no SERVICE\nSELECT * { ?s ?p ?o }")
    assert not analyze_coverage._uses_service("SELECT * { ?s ?p ?o }")


def test_federation_cases_leave_the_query_eval_denominator() -> None:
    # Integration against the real corpus: every SERVICE query-eval case is
    # lifted into the out-of-scope FEDERATION category (not counted as an
    # algebra XFAIL, not in the QueryEvaluationTest denominator).
    from tests.w3c.runner import QUERY_EVAL, w3c_corpus_root

    if w3c_corpus_root() is None:
        pytest.skip("W3C corpus not on disk; run scripts/fetch_w3c.sh first")

    by_category = analyze_coverage.analyze()
    fed = by_category.get(analyze_coverage.FEDERATION)
    assert fed is not None and fed.total >= 1, "expected federation cases to be reclassified"
    # Federation cases are skipped (out of scope), never counted as passes/xfails.
    assert fed.skipped == fed.total and fed.passed == 0 and fed.xfailed == 0
    # None of the remaining query-eval XFAILs mention ServiceGraphPattern.
    qe = by_category[QUERY_EVAL]
    assert not any("ServiceGraphPattern" in reason for reason in qe.xfail_reasons)


def test_result_format_suites_are_out_of_scope() -> None:
    # TSV/JSON result-serialization tests are lifted out of the query-eval
    # denominator (out of scope like the CSV result-format tests), so every
    # remaining in-scope query-evaluation case translates: 100%, 0 XFAIL.
    from tests.w3c.runner import QUERY_EVAL, w3c_corpus_root

    if w3c_corpus_root() is None:
        pytest.skip("W3C corpus not on disk; run scripts/fetch_w3c.sh first")

    by_category = analyze_coverage.analyze()
    rf = by_category.get(analyze_coverage.RESULT_FORMAT)
    assert rf is not None and rf.skipped == rf.total and rf.passed == 0
    qe = by_category[QUERY_EVAL]
    assert qe.xfailed == 0 and qe.failed == 0, "an in-scope query-eval case regressed"
    assert qe.coverage == 100.0


def test_analyze_live_counts_only_parameterized_w3c_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_command: list[str] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        seen_command.extend(command)
        junit_arg = next(arg for arg in command if arg.startswith("--junitxml="))
        junit_path = Path(junit_arg.split("=", 1)[1])
        junit_path.write_text(
            """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="2" skipped="2">
    <testcase classname="tests.w3c.test_w3c_live_execution" name="test_live_execution[bind/bind07]">
      <skipped type="pytest.xfail" message="binding divergence" />
    </testcase>
    <testcase classname="tests.w3c.test_w3c_live_execution" name="test_live_execution[functions/rand01]">
      <skipped type="pytest.xfail" message="binding divergence" />
    </testcase>
  </testsuite>
</testsuites>
""",
            encoding="utf-8",
        )
        return _completed(stdout="86 passed, 105 xfailed in 10.24s")

    monkeypatch.setenv("RUN_INTEGRATION", "1")
    monkeypatch.setattr(subprocess, "run", fake_run)

    stats = analyze_coverage.analyze_live()

    assert seen_command[-3] == ("tests/w3c/test_w3c_live_execution.py::test_live_execution")
    assert stats.total == 191
    assert stats.passed == 86
    assert stats.xfailed == 105
    assert stats.xfail_ids == {"bind/bind07", "functions/rand01"}
    assert stats.coverage == pytest.approx(86 / 191 * 100)


def test_analyze_live_rejects_counter_denominator_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUN_INTEGRATION", "1")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: _completed(stdout="90 passed, 105 xfailed in 10.24s"),
    )

    with pytest.raises(RuntimeError, match="195 outcomes for 191 W3C cases"):
        analyze_coverage.analyze_live()


def test_analyze_live_surfaces_hard_pytest_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUN_INTEGRATION", "1")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: _completed(returncode=1, stdout="1 failed, 190 passed"),
    )

    with pytest.raises(RuntimeError, match="live W3C pytest run failed"):
        analyze_coverage.analyze_live()


def test_live_failure_registry_matches_committed_report() -> None:
    registry_path = Path(__file__).with_name("LIVE_FAILURE_LABELS.json")
    report_path = Path(__file__).with_name("LIVE_FAILURES.md")
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    report = report_path.read_text(encoding="utf-8")

    failures = registry["failures"]
    report_ids = set(re.findall(r"^\| `([^`]+)` \|", report, re.MULTILINE))
    valid_labels = {
        "needs inference",
        "language tags lost",
        "text stored the wrong way",
        "genuine bug",
    }

    assert set(failures) == report_ids
    assert len(failures) == 60
    assert {entry["label"] for entry in failures.values()} <= valid_labels
    assert sum(entry["label"] in valid_labels for entry in failures.values()) == 60
    assert all(
        entry["diagnosis"].strip() and "\n" not in entry["diagnosis"]
        for entry in failures.values()
        if entry["label"] == "genuine bug"
    )


def test_live_failure_join_rejects_missing_and_stale_ids() -> None:
    entry = {"label": "genuine bug", "diagnosis": "A concrete one-line diagnosis."}

    with pytest.raises(ValueError, match="unlabelled.*suite/missing"):
        analyze_coverage.validate_live_failure_join({"suite/missing"}, {})

    with pytest.raises(ValueError, match="stale.*suite/stale"):
        analyze_coverage.validate_live_failure_join(set(), {"suite/stale": entry})


def test_live_failure_join_rejects_invalid_label() -> None:
    failures = {
        "suite/case": {
            "label": "not reviewed",
            "diagnosis": "A concrete one-line diagnosis.",
        }
    }

    with pytest.raises(ValueError, match="invalid label.*not reviewed"):
        analyze_coverage.validate_live_failure_join({"suite/case"}, failures)


def test_live_failure_baseline_rejects_count_mismatch() -> None:
    stats = analyze_coverage.CategoryStats(total=191, passed=130, xfailed=61)
    registry = {
        "baseline": {
            "profile": "document_edge",
            "total": 191,
            "passed": 131,
            "xfailed": 60,
        },
        "failures": {},
    }

    with pytest.raises(ValueError, match="live failure baseline mismatch.*passed.*xfailed"):
        analyze_coverage.validate_live_failure_baseline(stats, registry, "document_edge")


@pytest.mark.parametrize("contents", [None, "<testsuites>"])
def test_parse_live_xfail_ids_rejects_missing_or_malformed_junit(
    tmp_path: Path,
    contents: str | None,
) -> None:
    junit_path = tmp_path / "live-results.xml"
    if contents is not None:
        junit_path.write_text(contents, encoding="utf-8")

    with pytest.raises(RuntimeError, match="could not parse live W3C JUnit report"):
        analyze_coverage.parse_live_xfail_ids(junit_path)


def test_main_rejects_both_write_modes_before_analysis(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "analyze_coverage.py",
            "--write",
            "--live",
            "--write-live-failures",
        ],
    )
    monkeypatch.setattr(
        analyze_coverage,
        "analyze",
        lambda: pytest.fail("analysis must not run for conflicting write modes"),
    )

    with pytest.raises(SystemExit) as exc_info:
        analyze_coverage.main()

    assert exc_info.value.code != 0
    assert "--write cannot be used with --write-live-failures" in capsys.readouterr().err


def test_render_live_failures_reproduces_committed_report_byte_for_byte() -> None:
    registry_path = Path(__file__).with_name("LIVE_FAILURE_LABELS.json")
    report_path = Path(__file__).with_name("LIVE_FAILURES.md")
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    counts = Counter(entry["label"] for entry in registry["failures"].values())

    rendered = analyze_coverage.render_live_failures(registry, counts).encode()

    assert rendered == report_path.read_bytes()
