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
from typing import TYPE_CHECKING, Any

from rdflib.paths import AlternativePath

from .builder import attribute_ref

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


def _emit_union_of_arms(
    visitor: AlgebraVisitor,
    arm_drivers: list[Callable[[AlgebraVisitor], None]],
    *,
    distinct: bool = False,
    existence_collapse: bool = False,
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
    """
    if len(arm_drivers) < 2:
        # Defensive — AlternativePath always has ≥ 2 arms (otherwise
        # rdflib would have collapsed to the single inner path),
        # Union always has p1 + p2. If a future caller hands us a
        # degenerate single-arm list, fail loudly rather than emit
        # an AQL ``UNION((x))`` that ArangoDB would reject.
        raise ValueError(f"UNION requires at least 2 arms, got {len(arm_drivers)}")

    # ---- Phase 1: probe each arm to collect its bound variables -----
    # We need the union of per-arm vars BEFORE we can decide each
    # arm's RETURN schema. The probe spawns + walks a child but
    # never absorbs it, so the parent's counters stay put.
    outer_vars = set(visitor.state.var_to_expr.keys())
    arm_var_sets: list[set[str]] = []
    for driver in arm_drivers:
        probe = _spawn_child(visitor)
        driver(probe)
        arm_var_sets.append(set(probe.state.var_to_expr.keys()) - outer_vars)

    raw_vars = sorted(set().union(*arm_var_sets))
    # Property-path intermediate variables (``_path_<n>``, minted by
    # :meth:`AlgebraVisitor._fresh_path_var`) are internal sigil
    # names the user can never reference — strip them from the
    # per-arm RETURN and the outer-scope binding so the AQL only
    # carries user-visible vars.
    user_vars = [v for v in raw_vars if not v.startswith("_path_") and not v.startswith("_bn_")]

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

    # ---- Phase 2: emit each arm with full-schema projection ---------
    arm_aqls: list[str] = []
    # Variables some arm does not bind (it projects ``null``) or binds to a
    # value that can itself be unbound: unbound in some rows of the union.
    unbound_in_some_arm: set[str] = set()
    for driver in arm_drivers:
        cv = _spawn_child(visitor)
        driver(cv)
        unbound_in_some_arm.update(
            name for name in all_vars if name not in cv.state.var_to_expr or name in cv.state.maybe_unbound
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
        arm_aqls.append(visitor.builder.absorb_child(cv.builder))

    # ---- Outer-scope FOR + variable binding -------------------------
    row_alias = visitor.builder.fresh_alias(prefix="row")
    visitor.builder.for_union(row_alias, arm_aqls, distinct=distinct)

    for var_name in all_vars:
        if var_name == "_path_hit":
            # Sentinel only — never surface in SELECT * / projection.
            continue
        new_expr = attribute_ref(row_alias, var_name)
        existing = visitor.state.var_to_expr.get(var_name)
        if existing is None:
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

    * ``tenant_entity`` / ``tenant_bind_placeholder`` — the
      per-query tenant commitment and its cached AQL bind variable
      (PRD §6.5.1). A UNION arm that opens a tenant-scoped class
      must apply the SAME tenant filter, not mint a fresh one, so
      EXPLAIN output stays clean and cross-tenant-join detection
      stays consistent. Propagating both the commitment and the
      cached placeholder is what keeps the per-query tenant bind
      a single AQL parameter no matter how many UNION arms touch
      tenant-scoped collections.

    The two counters on ``BindingState`` (``path_var_counter`` and
    ``bgp_counter``) are deliberately NOT propagated — they mint
    internal sigil names (``_path_<n>`` / ``_bn_<bgp>_<label>``)
    whose scope is intra-arm, and resetting them per child gives
    deterministic per-arm AQL even when arms re-enter the same
    visitor code path.

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
    )
    cv.state.var_to_expr = dict(visitor.state.var_to_expr)
    # An outer variable an arm re-projects keeps "can be unbound" (#59).
    cv.state.maybe_unbound = set(visitor.state.maybe_unbound)
    cv.state.graph_scope = list(visitor.state.graph_scope)
    cv.state.var_to_rpt_class = dict(visitor.state.var_to_rpt_class)
    cv.state.tenant_entity = visitor.state.tenant_entity
    cv.state.tenant_bind_placeholder = visitor.state.tenant_bind_placeholder
    return cv
