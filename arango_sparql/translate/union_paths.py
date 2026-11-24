"""SPARQL 1.1 ``UNION`` and ``AlternativePath`` (``:p|:q``) emission.

Both constructs lower to the same AQL recipe: translate each arm in
its own child builder, project every arm's result to the common
union-schema (vars not bound in an arm get ``null``), then iterate
the combined arms with ``FOR <row> IN UNION((arm1), (arm2), …)``.
Each row of the UNION carries a uniform ``{var: value, ...}`` shape
so the outer scope can reference any variable from the union schema
via ``row.<var>`` regardless of which arm produced the row.

Why share the recipe:

* SPARQL ``AlternativePath`` is defined in §18.4 as the union of
  per-arm single-triple BGP solutions — the rewrite is literal:
  ``?s (:p|:q) ?o ≡ UNION({?s :p ?o}, {?s :q ?o})``. Emitting it
  through the same helper as ``Union`` keeps the two cases
  semantically and operationally aligned.
* AQL's ``UNION`` takes N array expressions and returns their
  concatenation. Each arm's child-builder block, wrapped in
  parentheses, is exactly such an expression.

Two-phase translation:

1. **Probe** every arm in a throwaway child to discover which
   variables it binds. Throwaway because we need the *union* of
   per-arm vars BEFORE we can decide each arm's RETURN schema. The
   probe uses :meth:`AqlQueryBuilder.create_child` but never calls
   :meth:`absorb_child`, so the parent's counters don't advance —
   the second-phase children re-spawn with the same seeded
   counters and mint the same aliases (which is harmless because
   the first-phase counterparts were discarded).
2. **Emit** every arm a second time, this time projecting the
   full union schema (each arm RETURNs ``{v1: expr_or_null,
   v2: expr_or_null, …}``). Absorb each into the parent in order;
   the parent's counters advance per absorption so the second
   arm's aliases are disjoint from the first's. Finally emit
   ``FOR <row> IN UNION((arm1_aql), (arm2_aql), …)`` in the
   outer scope and bind ``row.<var>`` into ``var_to_expr`` (with
   equality FILTERs for shared variables, matching the
   ``ToMultiSet`` / ``MINUS`` recipe).

Outer scope variable pre-seeding: each child receives a copy of
the outer's ``var_to_expr`` so a UNION nested inside a BGP that
has outer-bound ``?s`` joins each arm's local ``?s`` to the
outer's expression (via the BGP emitter's existing shared-variable
FILTER recipe). This mirrors how :mod:`minus_exists` propagates
the outer scope into MINUS / EXISTS probes.

Legacy reference: ``references/arango-sparql/src/lib/
pgt-translator.js`` has no ``visit_Union`` and no
``AlternativePath`` handler — both return ``UnsupportedSparqlError``
in the legacy Foxx service. This module is a green-field
implementation against the SPARQL 1.1 spec (§18.5 ``Union``,
§18.4 property paths), cross-validated against pyoxigraph via
``tests/cross/`` once the W3C harness lands the live-execution
runs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rdflib.paths import AlternativePath

from ..errors import AqlEmitError
from .builder import attribute_ref, indent_aql

if TYPE_CHECKING:
    from .visitor import AlgebraVisitor


# ---------------------------------------------------------------------------
# Public entrypoints
# ---------------------------------------------------------------------------


def emit_union(visitor: AlgebraVisitor, node: Any) -> None:
    """``Union(p1, p2)`` — bag-union of two patterns' binding sets.

    Each arm becomes a child-builder sub-query whose RETURN
    projects every variable in the combined union schema (vars not
    bound in this arm get ``null``). The outer scope iterates the
    AQL ``UNION(…)`` and binds each union variable to ``row.<var>``.
    """
    arms = [node.p1, node.p2]
    _emit_union_of_arms(visitor, [_visit_arm_driver(arm) for arm in arms])


def emit_alternative_path(
    visitor: AlgebraVisitor,
    subject: Any,
    predicate: AlternativePath,
    obj: Any,
) -> None:
    """``?s (:p|:q|…) ?o`` — desugar to a UNION of single-triple BGPs.

    Each arm is a single triple ``?s :p_i ?o`` re-dispatched
    through :meth:`AlgebraVisitor._emit_triple` so PG / LPG /
    default-collection / RPT branches all compose without
    duplicating per-arm logic here. The same
    :func:`_emit_union_of_arms` helper that ``visit_Union`` uses
    drives the two-phase translation. A path relation is a **set** of
    connected node pairs (SPARQL 1.1 §18.4), so two predicates linking
    the *same* pair (``:a :p :b . :a :q :b``) yield ``:a (:p|:q) :b``
    once — hence ``distinct=True`` (``UNION_DISTINCT``). Crucially the
    distinct key is the whole projected binding, including a
    SequencePath intermediate (``_path_1``): distinct *paths* through
    different intermediates to the same endpoint stay separate rows
    (W3C pp31: ``(:p1|:p2)/(:p3|:p4)`` → two solutions). That is why the
    intermediate must be projected + joined, never collapsed away.
    """
    arms = list(predicate.args)
    _emit_union_of_arms(
        visitor,
        [_triple_arm_driver(subject, arm, obj) for arm in arms],
        distinct=True,
    )


# Named driver factories rather than lambdas-with-defaults: mypy cannot
# infer a lambda whose extra parameters exist only to bind loop state,
# and the factory spells out the closure explicitly (same convention as
# :mod:`paths`).


def _visit_arm_driver(arm: Any) -> Callable[[AlgebraVisitor], None]:
    def drive(v: AlgebraVisitor) -> None:
        v.visit(arm)

    return drive


def _triple_arm_driver(subject: Any, arm: Any, obj: Any) -> Callable[[AlgebraVisitor], None]:
    def drive(v: AlgebraVisitor) -> None:
        v._emit_triple((subject, arm, obj))  # noqa: SLF001 - intentional

    return drive


# ---------------------------------------------------------------------------
# Shared two-phase implementation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DepthGuard:
    """Refuse a bounded ``:p*`` / ``:p+`` expansion that would answer short.

    The arms of :func:`_emit_union_of_arms` are the path repeated ``0..N`` (or
    ``1..N``) times; ``beyond`` walks it ``N + 1`` times, projected like the
    arms. When ``beyond`` yields a row the arms do not, some node is first
    reached after ``N + 1`` repetitions, so the bounded union is missing pairs,
    and the query fails with ``FAIL(<message>)`` instead (issue #183; the
    argument is in :func:`arango_sparql.translate.paths._emit_mul_path`).

    ``only`` emits the check without iterating the arms: the caller emits the
    path's own arms afterwards, because they are constrained in a way the
    check must not be (an object the query fixes).
    """

    beyond: Callable[[AlgebraVisitor], None]
    message_bind: str
    only: bool = False


def _emit_union_of_arms(
    visitor: AlgebraVisitor,
    arm_drivers: list[Callable[[AlgebraVisitor], None]],
    *,
    distinct: bool = False,
    existence_collapse: bool = False,
    keep_vars: frozenset[str] = frozenset(),
    depth_guard: DepthGuard | None = None,
) -> None:
    """Two-phase UNION emitter — probe arms for vars, then emit
    each arm with the full union-schema projection and combine.

    ``arm_drivers`` is a list of single-argument callables that
    take a child :class:`AlgebraVisitor` and walk one arm into
    it. Decoupling the *what to translate* (driver) from the *how
    to wrap and combine* (this function) keeps the same code path
    serving both ``Union`` (driver = ``cv.visit(arm)``) and
    ``AlternativePath`` (driver = ``cv._emit_triple((s, p_i, o))``)
    without per-caller duplication.

    ``distinct`` selects AQL ``UNION_DISTINCT`` (property-path set
    semantics) vs ``UNION`` (SPARQL pattern bag semantics).

    ``keep_vars`` are internal sigils (``_path_<n>`` / ``_bn_…``) that are
    still projected and bound outside: the end points of a repeated path. A
    ``:p+`` whose object is a sequence's intermediate (``:p+/:q``) must hand
    that intermediate to the next step, or the step joins nothing.

    ``depth_guard`` binds the arms with ``LET`` and checks them against one
    more repetition before iterating them (:class:`DepthGuard`).
    """
    if len(arm_drivers) < (1 if depth_guard is not None else 2):
        # Defensive — AlternativePath always has ≥ 2 arms (otherwise
        # rdflib would have collapsed to the single inner path),
        # Union always has p1 + p2. If a future caller hands us a
        # degenerate single-arm list, fail loudly rather than emit
        # an AQL ``UNION((x))`` that ArangoDB would reject. A guarded
        # repetition (``:p+`` at depth 1) may have one arm: its LET pairs
        # it with ``[]``.
        raise ValueError(f"UNION requires at least 2 arms, got {len(arm_drivers)}")

    # ---- Phase 1: probe each arm to collect its bound variables -----
    # We need the union of per-arm vars BEFORE we can decide each
    # arm's RETURN schema. The probe spawns + walks a child but
    # never absorbs it, so the parent's counters stay put.
    outer_vars = set(visitor.state.var_to_expr.keys())
    arm_var_sets: list[set[str]] = []
    # Outer variables an earlier OPTIONAL can leave unbound, which an arm
    # joins with any value (SPARQL 1.1 §18.3) and so fills: its expression
    # for them changed (``NOT_NULL(outer, inner)``). They are projected like
    # the arm's own variables and read back below, or the solution keeps them
    # unbound (review of #192), as ``optional_subquery`` does for its body.
    filled: set[str] = set()
    for driver in arm_drivers:
        probe = _spawn_child(visitor)
        driver(probe)
        arm_var_sets.append(set(probe.state.var_to_expr.keys()) - outer_vars)
        filled.update(
            name
            for name in outer_vars & visitor.state.maybe_unbound
            if probe.state.var_to_expr.get(name, visitor.state.var_to_expr[name])
            != visitor.state.var_to_expr[name]
            and not name.startswith(("_path_", "_bn_"))
        )

    raw_vars = sorted(set().union(*arm_var_sets))
    # Property-path intermediate variables (``_path_<n>``, minted by
    # :meth:`AlgebraVisitor._fresh_path_var`) are internal sigil
    # names the user can never reference — strip them from the
    # per-arm RETURN and the outer-scope binding so the AQL only
    # carries user-visible vars.
    user_vars = [v for v in raw_vars if v in keep_vars or not v.startswith(("_path_", "_bn_"))]

    if not raw_vars:
        # Arms bind no variables at all — a constant-only existence
        # probe (e.g. ``:a (:p)* :b`` / ``:a (:p|:q) :b`` with both ends
        # IRI). Sentinel schema so ``UNION_DISTINCT`` yields one row.
        all_vars = ["_path_hit"]
    elif not user_vars and existence_collapse:
        # Bounded/arbitrary-length repetition (``*``/``+``/``{n,m}``,
        # from :mod:`paths`) whose per-length arms bound only internal
        # ``_path_*`` / ``_bn_*`` sigils: an existence-only walk —
        # collapse to the sentinel (re-projecting the sigils produced
        # one empty SELECT * row per walk).
        all_vars = ["_path_hit"]
    elif not user_vars:
        # SequencePath intermediate (e.g. the ``_path_1`` in
        # ``:a (:p1|:p2)/(:p3|:p4) ?t``). It MUST be projected and bound
        # in the outer scope so the next step can join on it
        # (``FILTER doc._uri == row._path_1``) — collapsing it to the
        # sentinel drops the join and merges distinct paths.
        all_vars = list(raw_vars)
    else:
        # Strip internal sigils from the user-visible projection.
        all_vars = user_vars
    all_vars = [*all_vars, *sorted(filled - set(all_vars))]

    # ---- Phase 2: emit each arm with full-schema projection ---------
    # Variables some arm does not bind (it projects ``null``) or binds to a
    # value that can itself be unbound: unbound in some rows of the union.
    unbound_in_some_arm: set[str] = set()
    # A variable every binding arm bound to a reified relationship's edge
    # stays a relationship node outside (plan D8); one an arm bound to
    # anything else does not.
    arm_relationships: dict[str, set[str]] = {}
    not_relationship: set[str] = set()

    def emit_arm(driver: Callable[[AlgebraVisitor], None], *, track: bool) -> str:
        cv = _spawn_child(visitor)
        driver(cv)
        if track:
            for name in all_vars:
                if name not in cv.state.var_to_expr or name in visitor.state.var_to_expr:
                    continue
                rels = cv.state.relationship_nodes.get(name)
                if rels:
                    arm_relationships.setdefault(name, set()).update(rels)
                else:
                    not_relationship.add(name)
        # An arm that only reads an outer document opens no FOR of its own.
        if not cv.builder.has_for_clause():
            cv.builder.open_one_row_scope_first(cv.builder.fresh_alias(prefix="empty"))
        if track:
            unbound_in_some_arm.update(
                name
                for name in all_vars
                if name not in cv.state.var_to_expr or name in cv.state.maybe_unbound
            )
        # Build the per-arm RETURN: vars bound in this arm use
        # their expression; vars NOT bound in this arm fall back
        # to ``null`` so every row of UNION carries the same
        # schema (matches SPARQL semantics where a variable that
        # only appears in one UNION branch is UNDEF in the other
        # branch's rows). Existence-only arms project
        # ``_path_hit: 1`` so UNION_DISTINCT collapses to one row.
        mapping: list[tuple[str, str]] = []
        for var_name in all_vars:
            if var_name == "_path_hit":
                mapping.append((var_name, "1"))
                continue
            expr = cv.state.var_to_expr.get(var_name, "null")
            mapping.append((var_name, expr))
        cv.builder.return_object(mapping)
        return visitor.builder.absorb_child(cv.builder)

    arm_aqls = [emit_arm(driver, track=True) for driver in arm_drivers]

    # ---- Outer-scope FOR + variable binding -------------------------
    if depth_guard is None:
        row_alias = visitor.builder.fresh_alias(prefix="row")
        visitor.builder.for_union(row_alias, arm_aqls, distinct=distinct)
    else:
        if all_vars == ["_path_hit"]:
            # The check compares end points; an existence-only row has none,
            # so it could not tell a new node from a known one.
            raise AqlEmitError("property-path depth guard needs the end point projected, got none")
        # The beyond arm is projected like the others but leaves no trace on
        # the outer scope: it is only compared, never iterated.
        beyond_aql = emit_arm(depth_guard.beyond, track=False)
        reach_alias = visitor.builder.fresh_alias(prefix="reach")
        beyond_alias = visitor.builder.fresh_alias(prefix="beyond")
        visitor.builder.let_union(reach_alias, arm_aqls, distinct=True)
        visitor.builder.let(beyond_alias, f"(\n{indent_aql(beyond_aql)}\n)")
        # ``||`` short-circuits, so FAIL runs only when a node is new.
        visitor.builder.filter_raw(
            f"LENGTH(MINUS({beyond_alias}, {reach_alias})) == 0 || FAIL({depth_guard.message_bind})"
        )
        if depth_guard.only:
            return
        row_alias = visitor.builder.fresh_alias(prefix="row")
        visitor.builder.for_inline(row_alias, reach_alias)

    for var_name in all_vars:
        if var_name == "_path_hit":
            # Sentinel only — never surface in SELECT * / projection.
            continue
        new_expr = attribute_ref(row_alias, var_name)
        existing = visitor.state.var_to_expr.get(var_name)
        if var_name in filled and existing is not None:
            # Every arm already held it compatible with the outer value; an
            # arm that filled it carries the value, the others the outer one.
            visitor.state.var_to_expr[var_name] = f"NOT_NULL({existing}, {new_expr})"
            if var_name not in unbound_in_some_arm:
                visitor.state.maybe_unbound.discard(var_name)
            continue
        if existing is None:
            if var_name in arm_relationships and var_name not in not_relationship:
                visitor.state.relationship_nodes[var_name] = arm_relationships[var_name]
            visitor.state.var_to_expr[var_name] = new_expr
            if var_name in unbound_in_some_arm:
                visitor.state.maybe_unbound.add(var_name)
            continue
        if existing != new_expr:
            visitor.builder.filter_raw(f"{new_expr} == {existing}")


def _spawn_child(visitor: AlgebraVisitor) -> AlgebraVisitor:
    """Spawn a child visitor with outer-scope binding & scope state
    pre-seeded.

    Pre-seeding plays the same role as in
    :mod:`minus_exists`: a UNION arm that references an
    outer-bound variable should turn it into an equality FILTER
    against the outer's expression, not anchor a new FOR. The
    BGP emitter's ``_bind_subject`` does the right thing as long
    as ``var_to_expr`` has the outer's binding when the inner
    triple is visited.

    **Scope-stack propagation** — beyond ``var_to_expr`` we also
    carry forward four pieces of ambient state so the child
    behaves identically to the outer scope it was spawned from:

    * ``graph_scope`` — the active SPARQL named-graph stack. When
      a ``GRAPH ?g { … }`` wrapper opens a UNION (e.g. the path
      pattern in W3C ``property-path/pp35``), each child arm's
      ``_open_collection`` calls must see the ``?g`` term on the
      stack so ``_apply_graph_scope`` can both attach the
      ``alias.<graph_field>`` filter AND bind the graph variable
      into the child's ``var_to_expr``. The standard UNION-emitter
      re-projection then promotes that binding to the outer's
      ``row.<g>`` expression for the surrounding FILTER. Forgetting
      to propagate this stack used to fail pp35 with the misleading
      ``FILTER references unbound variable ?g`` diagnostic — the
      variable was perfectly well-scoped at the SPARQL level; the
      visitor just dropped it on UNION descent.

    * ``var_to_rpt_class`` — an outer-scope RPT type pattern (``?s
      a :Person``) binds ``?s`` to a ``ResolvedClass`` in this map;
      a UNION arm that references the same ``?s`` must continue to
      route through the RPT reader, not the PG/LPG emitter. Same
      copy-on-spawn discipline as ``var_to_expr``.

    * the tenant commitment and its cached AQL bind variable (PRD
      §6.5.1). The commitment is SHARED with the parent, so a class
      under another tenant root is refused wherever it sits, in the
      arm or after it (#211). A tenant bind the parent already has is
      reused, so the arm's tenant filter is the same AQL parameter;
      one the arm mints stays the arm's.

    The sigil counters (``state.counters``: ``_path_<n>`` /
    ``_bn_<bgp>_<label>``) are SHARED with the parent, not reset: the
    child is seeded with the outer bindings, so a child restarting at
    ``_path_1`` would re-use an outer ``_path_1`` and join unrelated
    intermediates (LDBC IC6). Sharing keeps every minted name unique in
    the query and the AQL deterministic. Both come from
    :meth:`_BindingState.share_query_state`, which every child visitor
    calls.

    Counter sharing follows the standard
    :meth:`AqlQueryBuilder.create_child` /
    :meth:`absorb_child` protocol — caller is responsible for
    either absorbing the child or discarding it (probe phase).
    """
    # Local import to avoid the visitor ↔ union_paths cycle at
    # module-import time.
    from .visitor import AlgebraVisitor

    child_builder = visitor.builder.create_child()
    cv = AlgebraVisitor(
        builder=child_builder,
        resolver=visitor.resolver,
        tenant_id=visitor.tenant_id,
        base_iri=visitor.base_iri,
        # Whether a union in the arm or body may keep one row per IRI (#147).
        query_triples=visitor.query_triples,
        describe_resources=visitor.describe_resources,
    )
    cv.state.var_to_expr = dict(visitor.state.var_to_expr)
    visitor.state.share_query_state(cv.state)
    # An outer variable an arm re-projects keeps "can be unbound" (#59).
    cv.state.maybe_unbound = set(visitor.state.maybe_unbound)
    cv.state.graph_scope = list(visitor.state.graph_scope)
    cv.state.graph_vars_unbound_on_entry = set(visitor.state.graph_vars_unbound_on_entry)
    cv.state.graph_vars_open_on_entry = set(visitor.state.graph_vars_open_on_entry)
    # OPTIONAL-body scope (#125): what an enclosing body hid, what an
    # enclosing EXISTS substitutes.
    cv.state.hidden_outer = dict(visitor.state.hidden_outer)
    cv.state.hidden_outer_unbound = set(visitor.state.hidden_outer_unbound)
    cv.state.substituted = set(visitor.state.substituted)
    cv.state.outer_subject_homes = dict(visitor.state.outer_subject_homes)
    # Where the outer documents are, so a GRAPH inside the child can re-read
    # one there (``AlgebraVisitor._reopen_at_home``).
    cv.state.doc_to_class = dict(visitor.state.doc_to_class)
    cv.state.var_to_rpt_class = dict(visitor.state.var_to_rpt_class)
    # An outer PG/LPG document stays a document in the arm: AQL subqueries
    # see outer FOR variables, so a triple from an outer-bound subject
    # traverses from that document instead of re-reading the subject (a scan
    # routed by the property's domain, joined back on its IRI). In IS6 the
    # re-read ran once per path-length arm and tripped an ArangoDB 3.12.9
    # planner error (ERR 4, "missing variable … for node … TraversalNode").
    # RPT aliases are triples rows, not documents, and stay out.
    cv.state.var_to_doc_alias = {
        name: alias
        for name, alias in visitor.state.var_to_doc_alias.items()
        if name not in visitor.state.var_to_rpt_class
    }
    # A reified relationship's edge bound outside stays one inside, so an
    # exit predicate in an arm (LDBC ``?lk :hasPost|:hasComment ?m``)
    # reaches its far vertex (plan D8).
    cv.state.reified_edges = dict(visitor.state.reified_edges)
    # An outer union row that stands for every copy of its IRI is read from
    # all of them in the arm or OPTIONAL body too (#147).
    cv.state.copy_members = dict(visitor.state.copy_members)
    cv.state.relationship_nodes = {name: set(rels) for name, rels in visitor.state.relationship_nodes.items()}
    return cv
