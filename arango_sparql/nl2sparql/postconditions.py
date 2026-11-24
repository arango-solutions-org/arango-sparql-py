"""Example SPARQL-specific postconditions for the NL→SPARQL retry loop.

The postcondition *mechanism* is language-agnostic and lives in the shared
engine (:mod:`arango_query_core.nl.postconditions`): a caller-supplied semantic
invariant that rides the generate→validate→retry budget, is announced to the
model up front, feeds its correction back on violation, and fails closed when
the budget is spent. See that module for the ``Postcondition`` protocol and the
runner.

This module ships two *illustrative* SPARQL invariants a caller can pass into
:func:`arango_sparql.nl2sparql.nl_to_sparql` (or model their own domain checks
on). They catch the class of bug neither parse nor translate can: a query that
is well-formed and maps to valid AQL yet is **semantically wrong**.

Both inspect the rdflib algebra (hard rule #1 — never a hand-rolled parser) via
:func:`arango_sparql.translate.parser.parse_sparql`, and both are scoped to
``SELECT`` queries; ``ASK`` / ``CONSTRUCT`` / ``DESCRIBE`` have no row
projection to reason about, so the checks accept them untouched. A parse failure
inside a check returns ``None`` (accept) rather than raising: the engine only
runs postconditions on queries the ``validate`` seam already parsed and
translated, so a parse error here would mean disagreement with the authority,
and failing the query closed on it would be a false outage.
"""

from __future__ import annotations

# The shared engine's violation type — the only thing a check must return.
from arango_query_core.nl import PostconditionContext, PostconditionViolation
from rdflib.term import Variable

from ..translate.parser import parse_sparql

__all__ = [
    "ForbidUnboundProjection",
    "RequireResultLimit",
]


def _select_algebra(query: str) -> object | None:
    """Return the top ``SelectQuery`` algebra node, or ``None`` when the query
    is not a SELECT (or cannot be parsed — deferring to the ``validate`` seam)."""
    try:
        parsed = parse_sparql(query)
    except Exception:  # noqa: BLE001 - validate() is the authority on parseability
        return None
    algebra = parsed.algebra
    if getattr(algebra, "name", None) != "SelectQuery":
        return None
    return algebra


class RequireResultLimit:
    """Every ``SELECT`` must bound its result set with ``LIMIT``.

    An unbounded ``SELECT`` parses, translates, and returns *plausible* results
    in a demo — then streams the whole graph in production. This invariant makes
    the cap a hard requirement, and (when ``max_rows`` is set) also rejects a
    ``LIMIT`` larger than the ceiling. rdflib represents ``LIMIT``/``OFFSET`` as
    a ``Slice`` node carrying ``length``; its absence means no limit.
    """

    code = "require_result_limit"

    def __init__(self, max_rows: int | None = None) -> None:
        self.max_rows = max_rows

    def prompt_section(self) -> str:
        ceiling = f" of at most {self.max_rows}" if self.max_rows is not None else ""
        return (
            "INVARIANT: every SELECT query MUST end with an explicit "
            f"LIMIT clause{ceiling} so the result set is bounded."
        )

    def check(self, query: str, *, context: PostconditionContext) -> PostconditionViolation | None:
        algebra = _select_algebra(query)
        if algebra is None:
            return None
        length = _find_slice_length(algebra)
        if length is None:
            hint = "Add a LIMIT clause, e.g. `LIMIT 100`."
            if self.max_rows is not None:
                hint = f"Add a LIMIT clause of at most {self.max_rows}, e.g. `LIMIT {self.max_rows}`."
            return PostconditionViolation(
                code=self.code,
                reason="The SELECT query has no LIMIT, so its result set is unbounded.",
                suggested_hint=hint,
            )
        if self.max_rows is not None and length > self.max_rows:
            return PostconditionViolation(
                code=self.code,
                reason=f"The LIMIT of {length} exceeds the allowed maximum of {self.max_rows}.",
                suggested_hint=f"Lower the LIMIT to {self.max_rows} or fewer rows.",
            )
        return None


class ForbidUnboundProjection:
    """Every explicitly projected variable must be bound in the query body.

    ``SELECT ?person ?salary WHERE { ?person a :Employee }`` parses and
    translates, yet ``?salary`` is never bound — every row comes back with it
    unset. No syntactic check catches this; a reviewer skimming plausible output
    might not either. This invariant requires each projected variable to appear
    in the ``WHERE`` body — as a triple term, a ``BIND`` target, or an aggregate
    result. ``SELECT *`` has no explicit projection to check and is accepted.
    """

    code = "forbid_unbound_projection"

    def prompt_section(self) -> str:
        return (
            "INVARIANT: every variable named in the SELECT projection MUST be "
            "bound in the WHERE clause (by a triple pattern, BIND, or aggregate) "
            "— never project a variable the body never binds."
        )

    def check(self, query: str, *, context: PostconditionContext) -> PostconditionViolation | None:
        algebra = _select_algebra(query)
        if algebra is None:
            return None
        projection = getattr(algebra, "explicit_projection", None)
        if not projection:  # SELECT * — nothing declared to verify
            projection = _extract_projection(query)
        if not projection:
            return None
        body: set[Variable] = set()
        _collect_body_vars(getattr(algebra, "p", None), body)
        unbound = [str(v) for v in projection if v not in body]
        if not unbound:
            return None
        listed = ", ".join(f"?{name}" for name in unbound)
        return PostconditionViolation(
            code=self.code,
            reason=f"Projected variable(s) {listed} are never bound in the WHERE clause.",
            suggested_hint=(
                f"Either bind {listed} with a triple pattern / BIND / aggregate, "
                "or drop them from the SELECT projection."
            ),
        )


def _extract_projection(query: str) -> list[Variable] | None:
    """Recover the projection list directly from the parser result.

    ``parse_sparql`` already exposes ``explicit_projection``; this re-parses only
    on the ``SELECT *`` fallthrough, where it returns ``None`` (nothing declared).
    """
    try:
        return parse_sparql(query).explicit_projection
    except Exception:  # noqa: BLE001 - deferring to the validate seam, as above
        return None


def _find_slice_length(node: object, depth: int = 0) -> int | None:
    """Return the ``LIMIT`` length from the first ``Slice`` node, or ``None``."""
    if node is None or depth > 40:
        return None
    if getattr(node, "name", None) == "Slice":
        length = node.get("length") if hasattr(node, "get") else None
        return int(length) if length is not None else None
    for attr in ("p", "p1", "p2", "graph"):
        child = getattr(node, attr, None)
        if child is not None and hasattr(child, "name"):
            found = _find_slice_length(child, depth + 1)
            if found is not None:
                return found
    return None


def _collect_body_vars(node: object, out: set[Variable], depth: int = 0) -> None:
    """Collect every variable the query *body* can bind.

    Distinct in purpose from ``translate.optional_subquery.bound_variables``
    (exactly the variables a pattern binds, for MINUS and OPTIONAL scope):
    this over-collects, taking every variable the body mentions plus the
    assignment slots — ``.var`` (BIND / Extend), ``.res`` and ``.A``
    (aggregates) — so a projected variable bound by BIND or an aggregate is
    not falsely reported as unbound.
    """
    if node is None or depth > 60:
        return
    if isinstance(node, Variable):
        out.add(node)
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            _collect_body_vars(item, out, depth + 1)
        return
    for attr in ("p", "p1", "p2", "graph", "expr", "other", "triples", "var", "res", "A", "vars"):
        child = getattr(node, attr, None)
        if child is not None:
            _collect_body_vars(child, out, depth + 1)


def soft_postcondition_note(violation: PostconditionViolation) -> str:
    """The one sentence both NL entry points report when the engine returned
    a candidate after a *soft* postcondition used up the retry budget
    (``PostconditionViolation.soft``, arango-query-core >= 0.2.3, PRD §7.3.1)."""
    return f"Returned after the retry budget ran out on a soft check: {violation.reason} ({violation.code})."
