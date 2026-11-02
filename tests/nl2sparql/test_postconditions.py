"""SPARQL example postconditions — unit checks + end-to-end through the engine.

Two layers:

* Unit — call each shipped postcondition's ``check`` directly on hand-written
  SPARQL, asserting it flags the semantically-wrong shape and (critically) does
  NOT false-positive on valid BIND/aggregate bindings or non-SELECT queries.
* Integration — drive :func:`arango_sparql.nl2sparql.nl_to_sparql` with a
  scripted provider so a violation rides the real retry budget: it retries with
  the correction, succeeds when the next candidate satisfies the invariant, and
  fails closed (``method="llm_failed"``, reason+code surfaced) when it never does.
"""

from __future__ import annotations

import pytest

pytest.importorskip("arango_query_core", reason="nl extra (arango-query-core) required")

from arango_query_core.nl import PostconditionContext, PostconditionViolation  # noqa: E402

from arango_sparql.nl2sparql import nl_to_sparql  # noqa: E402
from arango_sparql.nl2sparql.postconditions import (  # noqa: E402
    ForbidUnboundProjection,
    RequireResultLimit,
)

ONTOLOGY_TTL = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
<http://example.org/Person> a owl:Class ; phys:collectionName "Person" .
<http://example.org/name> a owl:DatatypeProperty .
"""

_PREFIX = "PREFIX ex: <http://example.org/> "
_NO_LIMIT = _PREFIX + "SELECT ?name WHERE { ?p a ex:Person ; ex:name ?name }"
_WITH_LIMIT = _NO_LIMIT + " LIMIT 100"


def _ctx() -> PostconditionContext:
    return PostconditionContext(schema_summary="", question="q", attempt=0)


class ScriptedProvider:
    """LLMProvider double: returns queued responses, records calls."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        self.calls.append((system, user))
        content = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        return content, {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "cached_tokens": 0,
        }


# ---------------------------------------------------------------------------
# RequireResultLimit — unit
# ---------------------------------------------------------------------------


def test_require_limit_flags_unbounded_select() -> None:
    v = RequireResultLimit().check(_NO_LIMIT, context=_ctx())
    assert v is not None
    assert v.code == "require_result_limit"
    assert "unbounded" in v.reason.lower() and "LIMIT" in v.suggested_hint


def test_require_limit_accepts_bounded_select() -> None:
    assert RequireResultLimit().check(_WITH_LIMIT, context=_ctx()) is None


def test_require_limit_rejects_over_ceiling() -> None:
    v = RequireResultLimit(max_rows=10).check(_NO_LIMIT + " LIMIT 500", context=_ctx())
    assert v is not None and "exceeds" in v.reason and "10" in v.suggested_hint


def test_require_limit_accepts_within_ceiling() -> None:
    assert RequireResultLimit(max_rows=10).check(_NO_LIMIT + " LIMIT 5", context=_ctx()) is None


def test_require_limit_ignores_non_select() -> None:
    # ASK has no row projection to bound — must be accepted untouched.
    assert RequireResultLimit().check("ASK { ?s ?p ?o }", context=_ctx()) is None


def test_require_limit_prompt_section_states_the_rule() -> None:
    assert "LIMIT" in RequireResultLimit().prompt_section()
    assert "at most 25" in RequireResultLimit(max_rows=25).prompt_section()


# ---------------------------------------------------------------------------
# ForbidUnboundProjection — unit
# ---------------------------------------------------------------------------


def test_unbound_projection_flags_variable_never_bound() -> None:
    q = _PREFIX + "SELECT ?name ?bogus WHERE { ?p a ex:Person ; ex:name ?name }"
    v = ForbidUnboundProjection().check(q, context=_ctx())
    assert v is not None
    assert v.code == "forbid_unbound_projection"
    assert "?bogus" in v.reason and "?bogus" in v.suggested_hint


def test_unbound_projection_accepts_fully_bound() -> None:
    assert ForbidUnboundProjection().check(_NO_LIMIT, context=_ctx()) is None


def test_unbound_projection_accepts_bind_target() -> None:
    # ?label is bound by BIND, not a triple — must NOT be a false positive.
    q = _PREFIX + "SELECT ?name ?label WHERE { ?p ex:name ?name BIND(?name AS ?label) }"
    assert ForbidUnboundProjection().check(q, context=_ctx()) is None


def test_unbound_projection_accepts_aggregate_result() -> None:
    q = _PREFIX + "SELECT (COUNT(?p) AS ?n) WHERE { ?p a ex:Person }"
    assert ForbidUnboundProjection().check(q, context=_ctx()) is None


def test_unbound_projection_accepts_select_star() -> None:
    # SELECT * declares no projection list — nothing to verify.
    assert ForbidUnboundProjection().check(_PREFIX + "SELECT * WHERE { ?s ?p ?o }", context=_ctx()) is None


def test_unbound_projection_ignores_non_select() -> None:
    assert ForbidUnboundProjection().check("ASK { ?s ?p ?o }", context=_ctx()) is None


def test_unbound_projection_prompt_section_states_the_rule() -> None:
    section = ForbidUnboundProjection().prompt_section()
    assert "bound" in section and "SELECT" in section


# ---------------------------------------------------------------------------
# End-to-end through nl_to_sparql — the retry budget
# ---------------------------------------------------------------------------


def test_postcondition_retries_then_succeeds() -> None:
    provider = ScriptedProvider([f"```sparql\n{_NO_LIMIT}\n```", f"```sparql\n{_WITH_LIMIT}\n```"])
    result = nl_to_sparql(
        "people",
        ontology_ttl=ONTOLOGY_TTL,
        provider=provider,
        postconditions=[RequireResultLimit()],
    )
    assert result.method == "llm"
    assert result.sparql == _WITH_LIMIT
    assert result.retries == 1
    # The correction reached the model on the retry.
    assert "LIMIT" in provider.calls[1][1]


def test_postcondition_exhaustion_fails_closed() -> None:
    provider = ScriptedProvider([f"```sparql\n{_NO_LIMIT}\n```"])
    result = nl_to_sparql(
        "people",
        ontology_ttl=ONTOLOGY_TTL,
        provider=provider,
        postconditions=[RequireResultLimit()],
        max_retries=2,
    )
    assert result.method == "llm_failed"
    assert result.sparql == ""  # a valid-but-rejected query is never returned
    assert result.retries == 2
    assert "require_result_limit" in result.explanation
    assert "unbounded" in result.explanation.lower()


def test_postcondition_announced_in_system_prompt() -> None:
    provider = ScriptedProvider([f"```sparql\n{_WITH_LIMIT}\n```"])
    nl_to_sparql(
        "people",
        ontology_ttl=ONTOLOGY_TTL,
        provider=provider,
        postconditions=[RequireResultLimit()],
    )
    system = provider.calls[0][0]
    assert "INVARIANT" in system and "LIMIT" in system


def test_no_postconditions_leaves_behaviour_unchanged() -> None:
    # The same unbounded query is accepted when no postcondition is supplied.
    provider = ScriptedProvider([f"```sparql\n{_NO_LIMIT}\n```"])
    result = nl_to_sparql("people", ontology_ttl=ONTOLOGY_TTL, provider=provider)
    assert result.method == "llm"
    assert result.sparql == _NO_LIMIT
    assert result.retries == 0


# ---------------------------------------------------------------------------
# Soft postconditions (arango-query-core >= 0.2.3, PRD §7.3.1)
# ---------------------------------------------------------------------------


class SoftRequireLimit:
    """RequireResultLimit, but soft: the same objection, marked ``soft``, so the
    engine returns the candidate once the budget is spent instead of failing
    closed. Shaped like the real ``Postcondition`` protocol (``code``,
    ``check(query, *, context)``, ``prompt_section()``)."""

    code = "soft_require_limit"

    def check(self, query: str, *, context: PostconditionContext) -> PostconditionViolation | None:
        hard = RequireResultLimit().check(query, context=context)
        if hard is None:
            return None
        return PostconditionViolation(
            reason=hard.reason, suggested_hint=hard.suggested_hint, code=self.code, soft=True
        )

    def prompt_section(self) -> str:
        return RequireResultLimit().prompt_section()


def test_a_soft_postcondition_returns_the_candidate_and_says_so() -> None:
    provider = ScriptedProvider([f"```sparql\n{_NO_LIMIT}\n```"])
    result = nl_to_sparql(
        "people",
        ontology_ttl=ONTOLOGY_TTL,
        provider=provider,
        postconditions=[SoftRequireLimit()],
        max_retries=2,
    )
    assert result.method == "llm"
    assert result.sparql == _NO_LIMIT  # the candidate is returned, not refused
    assert result.retries == 2  # the whole budget was spent asking for a fix
    assert result.explanation.startswith("Returned after the retry budget ran out on a soft check")
    assert "(soft_require_limit)" in result.explanation


def test_the_declared_query_core_floor_ships_soft_postconditions() -> None:
    """``PostconditionViolation.soft`` arrived in arango-query-core 0.2.3. A
    floor below it lets pip resolve 0.2.0-0.2.2, where a soft check silently
    fails closed (the two soft tests above fail; they did locally on 0.2.0)."""
    import tomllib
    from pathlib import Path

    from packaging.requirements import Requirement
    from packaging.version import Version

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    requirements = [Requirement(r) for r in project.get("dependencies", [])]
    for extra in project.get("optional-dependencies", {}).values():
        requirements.extend(Requirement(r) for r in extra)
    core = [r for r in requirements if r.name == "arango-query-core"]
    assert core, "arango-query-core is not declared in pyproject.toml"
    for req in core:
        floors = [Version(s.version) for s in req.specifier if s.operator == ">="]
        assert floors and min(floors) >= Version("0.2.3"), (
            f"{req} allows an arango-query-core without PostconditionViolation.soft (needs >=0.2.3)"
        )


def test_a_hard_postcondition_still_fails_closed_beside_the_soft_path() -> None:
    provider = ScriptedProvider([f"```sparql\n{_NO_LIMIT}\n```"])
    result = nl_to_sparql(
        "people", ontology_ttl=ONTOLOGY_TTL, provider=provider, postconditions=[RequireResultLimit()]
    )
    assert result.method == "llm_failed" and result.sparql == ""


def test_the_pipeline_warns_when_a_soft_postcondition_was_exhausted() -> None:
    from arango_sparql.nl2sparql import LLMResponse, NlPipeline, ScriptedLLMClient
    from arango_sparql.translate.resolver import SchemaResolver

    response = LLMResponse(
        content=f"```sparql\n{_NO_LIMIT}\n```",
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
    )
    client = ScriptedLLMClient([response, response, response], latency_ms=0)
    pipeline = NlPipeline(
        client=client,
        resolver=SchemaResolver.from_turtle(ONTOLOGY_TTL),
        ontology_ttl=ONTOLOGY_TTL,
        postconditions=[SoftRequireLimit()],
    )
    outcome = pipeline.run("people")
    assert outcome.sparql == _NO_LIMIT and outcome.aql
    soft = [w for w in outcome.warnings if w["code"] == "W_NL_SOFT_POSTCONDITION"]
    assert len(soft) == 1 and "(soft_require_limit)" in soft[0]["message"]
