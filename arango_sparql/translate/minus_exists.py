"""SPARQL 1.1 `MINUS`, `FILTER EXISTS`, `FILTER NOT EXISTS` emission.

All three constructs share one AQL recipe: spawn a child
:class:`AqlQueryBuilder`, translate the inner pattern in it with the
outer scope's variable bindings pre-seeded (so the inner's BGP
emitter turns shared variables into equality FILTERs against the
outer's expressions), short-circuit the child with ``LIMIT 1`` + a
constant ``RETURN 1``, then probe its row count in the outer scope::

    LET <probe> = LENGTH((
      <child AQL — with FILTERs joining inner aliases to outer exprs>
    ))
    FILTER <probe> {== 0 | > 0 | == 0}   -- Minus / EXISTS / NOT EXISTS

Why share the recipe:

* All three are *boolean compatibility tests* over a graph pattern —
  the only differences are (a) MINUS is a binary operator
  (``visit_Minus``) while EXISTS / NOT EXISTS are FILTER builtins
  (``_translate_expr`` dispatch) and (b) the comparator on the
  final FILTER.
* The child-builder primitives (:meth:`AqlQueryBuilder.create_child`,
  :meth:`absorb_child`) were introduced for the ``ToMultiSet`` slice
  (sub-SELECT / VALUES); reusing them here keeps the alias /
  bind-name disjointness invariant intact across nested probe
  blocks without duplicating the counter-seeding logic.

SPARQL semantic differences (and how we honour them):

* **MINUS vs NOT EXISTS** — SPARQL 1.1 §8.3.4: ``MINUS`` is a no-op
  if the two patterns share no variables (compatibility is vacuous),
  while ``NOT EXISTS`` still excludes outer rows whenever the inner
  pattern matches anything at all. We detect the empty-shared-var
  case in ``emit_minus`` and skip emission entirely; ``NOT EXISTS``
  always emits.
* **EXISTS** — the SPARQL "compatible mapping" semantic reduces to
  "does the inner pattern produce ≥ 1 row when the outer's current
  binding is substituted in?" The pre-seeded ``var_to_expr`` realises
  the substitution; the AQL ``LENGTH(...) > 0`` realises the
  "≥ 1 row" probe.

Lives next to :mod:`arango_sparql.translate.subselect` (the home
of the same child-builder pattern for ``ToMultiSet``) for the
same modularity reason: ``visitor.py`` stays under the 1500-line
cap from ``.cursor/rules/modularity-and-structure.mdc``.

Legacy reference: ``references/arango-sparql/src/lib/
pgt-translator.js`` lacks both ``MINUS`` and ``EXISTS`` — they
return ``UnsupportedSparqlError`` in the legacy Foxx service. This
module is a from-scratch port that follows the SPARQL spec
algebra; the legacy code can't be ported because it has no
analogous construct to fall back on.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..errors import UnsupportedSparqlError
from .builder import attribute_ref, indent_aql
from .optional_subquery import bound_variables, certain_variables

if TYPE_CHECKING:
    from .visitor import AlgebraVisitor


def emit_minus(visitor: AlgebraVisitor, node: Any) -> None:
    """``Minus(p1, p2)`` — remove outer solutions compatible with
    the inner pattern.

    Steps:

    1. Visit the outer pattern (``p1``) on the *parent* visitor so
       its FOR loops + FILTERs land in the outer AQL body and
       populate ``state.var_to_expr`` with the outer scope's
       bindings.
    2. Take the variables ``p2`` binds (:func:`bound_variables`: its
       triples, BIND targets, VALUES columns, sub-select projections)
       and intersect them with the outer's ``var_to_expr`` keys —
       that's the *shared-variable set* the SPARQL spec uses to
       define compatibility. A variable ``p2`` only reads, in a FILTER
       or in a sub-select it does not project, is outside every inner
       solution's domain, so it shares nothing (#209); a variable bound
       only by a VALUES block shares as any other.
    3. **If the shared set is empty: do nothing.** SPARQL §8.3.4
       defines MINUS over disjoint domains as a no-op (every outer
       solution is incompatible with every inner solution
       *vacuously*, so no outer row is removed). Emitting a probe
       would conservatively prune to zero rows, which is wrong.
       NOT EXISTS does not get this exemption (see
       :func:`emit_exists_filter`).
    4. Otherwise: spawn a child builder, pre-seed its
       ``var_to_expr`` with the outer's bindings (so the inner's
       BGP emitter turns ``?s`` into a FILTER against the outer's
       ``doc1._uri`` rather than opening a new ``?s`` document
       anchor), translate ``p2`` in the child, then short-circuit
       the child with ``LIMIT 1`` + ``RETURN 1``.
    5. Absorb the child, emit ``LET <probe> = LENGTH((<inner>))``
       in the outer scope, then ``FILTER <probe> == 0`` to remove
       outer rows that DID match the inner.

    Why ``LIMIT 1`` + ``RETURN 1``:

    * ``LIMIT 1`` short-circuits the child cursor — we only need
      to know whether ≥ 1 row matches, not how many.
    * ``RETURN 1`` is the minimal projection that satisfies the
      builder's "every query needs a RETURN" invariant without
      paying for any document materialisation.
    """
    visitor.visit(node.p1)

    # Inside FILTER EXISTS / NOT EXISTS the outer row's variables are bound
    # throughout the pattern, on both sides of this MINUS, as pyoxigraph and
    # rdflib evaluate it (the repo's ground truth; §18.6 substitutes them as
    # constants, which would put them in neither side's domain): every one
    # is shared and bound in every solution of the right side (third review
    # of #227, where the apart probe read them as unbound).
    preset = visitor.state.substituted & visitor.state.var_to_expr.keys()
    shared_names = (bound_variables(node.p2) & visitor.state.var_to_expr.keys()) | preset
    if not shared_names:
        # SPARQL spec §8.3.4 — disjoint domain ⇒ no-op. Important
        # divergence from NOT EXISTS, which excludes any outer row
        # whenever the inner matches even one row.
        return

    # An outer variable an earlier OPTIONAL can leave unbound is outside the
    # row's domain where it is unbound: it constrains nothing, and the row is
    # removed only through another shared variable bound on both sides
    # (§8.3.4), which must hold per inner solution. It is exact in two cases:
    # a shared variable bound on both sides in every solution (an anchor)
    # makes the overlap certain, and a body that binds every shared variable
    # in every solution overlaps exactly where the outer row binds one
    # (the guard in ``_translate_probe``). Otherwise — the overlap would
    # depend on which UNION arm, VALUES row, OPTIONAL or sub-select column
    # bound what — the probe keeps joining the variable as a value, as
    # before #193 (review of #196).
    open_outer = shared_names & visitor.state.maybe_unbound
    certain = certain_variables(node.p2) | preset
    anchors = (shared_names & certain) - visitor.state.maybe_unbound
    free_outer = open_outer if open_outer and (anchors or shared_names <= certain) else None
    # No anchor and not every shared variable bound in every solution of the
    # body (a VALUES UNDEF, an aggregate over no rows, a UNION arm, an
    # OPTIONAL): whether a solution overlaps the row depends on which shared
    # variables that solution binds. Joining the outer values in made one
    # that binds none of them a wildcard that removed every row, and one
    # that binds a shared variable the outer row leaves unbound matched
    # nothing (reviews of #227). The body is evaluated on its own and §8.3.4
    # applied per solution (:func:`_emit_apart_probe`). An OPTIONAL that
    # re-binds every shared variable is the exception: the conditional-add
    # sink already tests the overlap per solution (ADR-0002 Problem 2, W3C
    # ``negation/full-minuend``) and joins on the outer values, so that pass
    # is kept when it covers them.
    inner_aql = None
    if not anchors and not shared_names <= certain:
        try:
            inner_aql = _translate_probe(
                visitor, node.p2, allow_optional_rebind=True, overlap_var_names=shared_names, sink_only=True
            )
        except (_SinkDoesNotCover, UnsupportedSparqlError):
            _emit_apart_probe(visitor, node.p2, shared_names)
            return
    if inner_aql is None:
        inner_aql = _translate_probe(
            visitor,
            node.p2,
            allow_optional_rebind=True,
            overlap_var_names=shared_names,
            free_outer=free_outer,
        )

    probe_alias = visitor.builder.fresh_alias(prefix="minus_probe")
    visitor.builder.let(probe_alias, f"LENGTH((\n{indent_aql(inner_aql)}\n))")
    visitor.builder.filter_raw(f"{probe_alias} == 0")


def emit_exists_filter(
    visitor: AlgebraVisitor,
    exists_node: Any,
    *,
    negated: bool,
) -> str:
    """``FILTER EXISTS { … }`` / ``FILTER NOT EXISTS { … }``.

    Called from :meth:`AlgebraVisitor._translate_expr` when the
    expression node is ``Builtin_EXISTS`` (``negated=False``) or
    ``Builtin_NOTEXISTS`` (``negated=True``).

    Unlike :func:`emit_minus`, this helper EMITS the ``LET`` clause
    as a side-effect AND returns the boolean AQL expression to
    splice into the surrounding FILTER. Two reasons:

    1. ``_translate_expr`` is called bottom-up from within a
       ``Filter`` visit, so the FILTER clause is constructed at
       the call site. We can't emit the FILTER ourselves — only
       the upstream ``visit_Filter`` knows the *complete* boolean
       expression (EXISTS may be one conjunct in a larger ``&&``).
    2. The ``LET`` clause must precede the FILTER it feeds; that
       ordering is guaranteed because ``_translate_expr`` runs
       AFTER ``visit_Filter`` has already visited its inner
       pattern (so all outer FORs are open) but BEFORE the FILTER
       clause is appended.

    Empty-shared-var divergence from MINUS: ``NOT EXISTS`` still
    fires when shared vars are empty (it tests "does the inner
    pattern match ANYTHING in the dataset", not "does it match a
    row compatible with the current outer binding"). We skip the
    pre-seeding overhead in that case but still emit the probe
    + comparator.
    """
    inner_pattern = exists_node.graph
    inner_aql = _translate_probe(visitor, inner_pattern, substitute=True)

    probe_alias = visitor.builder.fresh_alias(prefix=("not_exists_probe" if negated else "exists_probe"))
    visitor.builder.let(probe_alias, f"LENGTH((\n{indent_aql(inner_aql)}\n))")

    return f"{probe_alias} == 0" if negated else f"{probe_alias} > 0"


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _emit_apart_probe(visitor: AlgebraVisitor, pattern: Any, shared: set[str]) -> None:
    """MINUS over a body evaluated on its own, as §8.3.4 defines it.

    The body is translated without the outer row and returns, per solution,
    its value for each shared variable (null where it leaves one unbound).
    An outer row is removed when some solution is compatible with it on
    every shared variable (unbound on either side, or equal) and binds one
    the row binds too. A body that reads nothing of the outer scope (the
    usual case) is computed once per evaluation of its scope, before that
    scope's loops (once per query at the top level; once per outer row
    inside an EXISTS, OPTIONAL subquery or UNION arm, #234); evaluated per
    row it took 14 s on 12,000 PG documents where main took 0.02 s (review
    of #227).
    """
    from .visitor import AlgebraVisitor

    child_builder = visitor.builder.create_child()
    child = AlgebraVisitor(
        builder=child_builder,
        resolver=visitor.resolver,
        tenant_id=visitor.tenant_id,
        base_iri=visitor.base_iri,
        query_triples=visitor.query_triples,
        describe_resources=visitor.describe_resources,
    )
    child.state.const_subject_rpt_class = dict(visitor.state.const_subject_rpt_class)
    # Inside EXISTS the outer row's variables are bound in the body too
    # (see ``emit_minus``); such a body reads the outer row and stays per row.
    child.state.var_to_expr = {
        name: visitor.state.var_to_expr[name]
        for name in visitor.state.substituted
        if name in visitor.state.var_to_expr
    }
    child.state.substituted = set(child.state.var_to_expr)
    # Matched in the scope's graph, as any probe is (§18.6, #195).
    child.state.graph_scope = list(visitor.state.graph_scope)
    visitor.state.share_query_state(child.state)
    child.visit(pattern)
    names = sorted(name for name in shared if name in child.state.var_to_expr)
    if not names:
        return  # no solution binds a shared variable: nothing is removed
    child_builder.open_one_row_scope_first(child_builder.fresh_alias(prefix="one"))
    child_builder.return_object([(name, child.state.var_to_expr[name]) for name in names])
    rows_aql = visitor.builder.absorb_child(child_builder)

    outer_aliases = {
        alias
        for expr in visitor.state.var_to_expr.values()
        for alias in re.findall(r"(?<![@.\w])([A-Za-z_]+\d+)\b", expr)
    }
    independent = not visitor.state.graph_scope and not any(
        re.search(rf"(?<![@.\w]){re.escape(alias)}\b", rows_aql) for alias in outer_aliases
    )
    if independent and len(names) == 1:
        # One shared variable: a solution overlaps the row exactly when it
        # binds that variable to the row's value, so the probe is a lookup
        # in its sorted distinct non-null values. A row the variable is
        # unbound in is never in them, so it is kept. ``NOOPT`` keeps the
        # list a value computed once: without it ArangoDB pushed the
        # ``NOT IN`` into an edge walk and re-evaluated it per vertex (over
        # 60 s at 12,000 people), as the VALUES semi-join found
        # (``subselect._emit_semi_join``). A scan per row of the hoisted rows
        # took 28 s (third review of #227).
        (name,) = names
        values = visitor.builder.fresh_alias(prefix="minus_values")
        row = visitor.builder.fresh_alias(prefix="row")
        visitor.builder.let_first(
            values,
            f"NOOPT(SORTED_UNIQUE((\n  FOR {row} IN (\n{indent_aql(indent_aql(rows_aql))}\n  )\n"
            f"  FILTER {attribute_ref(row, name)} != null\n"
            f"  RETURN {attribute_ref(row, name)}\n)))",
        )
        visitor.builder.filter_raw(f"{visitor.state.var_to_expr[name]} NOT IN {values}")
        return
    if independent:
        rows = visitor.builder.fresh_alias(prefix="minus_rows")
        visitor.builder.let_first(rows, f"(\n{indent_aql(rows_aql)}\n)")
        source = rows
    else:
        source = f"(\n{indent_aql(rows_aql)}\n)"
    row = visitor.builder.fresh_alias(prefix="row")
    pairs = [(attribute_ref(row, name), visitor.state.var_to_expr[name]) for name in names]
    compatible = [f"({inner} == null || {outer} == null || {inner} == {outer})" for inner, outer in pairs]
    overlap = [f"({inner} != null && {outer} != null)" for inner, outer in pairs]
    tests = (
        " && ".join(compatible)
        + " && "
        + (overlap[0] if len(overlap) == 1 else "(" + " || ".join(overlap) + ")")
    )
    probe = f"FOR {row} IN {source}\nFILTER {tests}\nLIMIT 1\nRETURN 1"
    probe_alias = visitor.builder.fresh_alias(prefix="minus_probe")
    visitor.builder.let(probe_alias, f"LENGTH((\n{indent_aql(probe)}\n))")
    visitor.builder.filter_raw(f"{probe_alias} == 0")


class _SinkDoesNotCover(Exception):
    """A ``sink_only`` probe whose OPTIONALs do not re-bind every shared variable."""


def _translate_probe(
    visitor: AlgebraVisitor,
    inner_pattern: Any,
    *,
    allow_optional_rebind: bool = False,
    overlap_var_names: set[str] | None = None,
    substitute: bool = False,
    free_outer: set[str] | None = None,
    sink_only: bool = False,
) -> str:
    """Spawn a child visitor for *inner_pattern*, emit a row-count
    probe (``LIMIT 1`` + ``RETURN 1``), absorb its binds, and
    return the inner AQL.

    ``allow_optional_rebind`` (MINUS only) lets the child accept an
    ``OPTIONAL`` that re-binds an outer-scoped variable as a SPARQL
    §18.2.5.2 conditional-add instead of rejecting it; the child's
    ``visit_LeftJoin`` emits the per-optional compatibility FILTER and
    records each ``(var, inner_value, outer_bound)`` in
    ``state.optional_rebind_sink``.

    ``overlap_var_names`` is the set of shared-variable names (the MINUS
    compatibility domain). When the *only* shared variables are bound by
    such optionals — i.e. every shared var appears in the sink, with no
    required inner triple binding any of them — the probe must also pass
    SPARQL §8.3.4's *disjoint-domain* test: an inner row removes an outer
    row only if it shares **at least one bound** variable with it. We
    encode that as an extra ``overlap`` FILTER (the OR of "this shared
    var is bound on both sides and equal"). If any shared var is bound by
    a required triple, the child already FILTERs equality on it, so
    overlap is guaranteed and no extra guard is emitted.

    ``sink_only`` raises
    :class:`_SinkDoesNotCover` unless the conditional-add sink binds every
    shared variable; the probe is then discarded.

    Shared with both ``emit_minus`` and ``emit_exists_filter`` —
    the only behavioural difference between those two is the
    no-op shortcut in ``emit_minus`` when shared vars are empty,
    and the comparator on the outer FILTER.

    Why pre-seed ``var_to_expr`` but NOT ``var_to_doc_alias``:

    * ``var_to_expr`` carries the outer's *expressions* (e.g.
      ``"doc1._uri"``). The inner's BGP emitter reads this map to
      decide whether to anchor a variable on a new FOR alias or
      to add an equality FILTER. Pre-seeding lets the inner
      reuse the outer's expressions as the join target. AQL's
      lexical scoping makes the outer alias (``doc1``) visible
      inside the child's ``(...)`` block, so the FILTER is valid.
    * ``var_to_doc_alias`` is the inner's record of which inner
      FOR alias owns each subject variable. Pre-seeding from the
      outer would mistakenly tell the inner "you already have a
      FOR for ?s" — which would skip opening the inner's own
      FOR + FILTER and leave the inner unable to constrain on
      the inner triples. We deliberately leave this empty.

    Carried over: ``var_to_rpt_class`` and ``const_subject_rpt_class``,
    so a probe triple on an outer triples-table (RPT) subject reads the
    table rather than a document collection (#190). Shared with the outer
    query (:meth:`_BindingState.share_query_state`): the name counters and
    the tenant commitment, so a class under another tenant root is refused
    inside the probe too (#211; it used to start uncommitted).
    """
    # Local import to avoid the visitor ↔ minus_exists module
    # cycle at import time.
    from .visitor import AlgebraVisitor

    child_builder = visitor.builder.create_child()
    child_visitor = AlgebraVisitor(
        builder=child_builder,
        resolver=visitor.resolver,
        tenant_id=visitor.tenant_id,
        base_iri=visitor.base_iri,
        # Whether a union in the probe may keep one row per IRI (#147).
        query_triples=visitor.query_triples,
        describe_resources=visitor.describe_resources,
    )
    # Pre-seed the child's var_to_expr with the outer scope's
    # bindings so shared variables turn into equality FILTERs
    # against the outer's expressions (the SPARQL compatibility
    # check, expressed in AQL).
    child_visitor.state.var_to_expr = dict(visitor.state.var_to_expr)
    # An outer subject read from a triples table (RPT) is still one inside
    # the probe: without its class the probe's triples on it were routed as
    # documents, which on a mapping that is not one triple store opened the
    # default collection (ERR 1203; #190). ``_spawn_child`` (UNION, OPTIONAL
    # subquery) carries the same state.
    child_visitor.state.var_to_rpt_class = dict(visitor.state.var_to_rpt_class)
    child_visitor.state.const_subject_rpt_class = dict(visitor.state.const_subject_rpt_class)
    # Inside ``GRAPH <g>`` / ``GRAPH ?g`` the probe's pattern is matched in
    # the same graph (SPARQL 1.1 §18.6: the active graph is the scope's).
    # Without the scope the probe read every graph, so ``GRAPH ?g { ?s :p ?o
    # FILTER EXISTS { ?s :q ?v } }`` passed on ``:q`` in the default graph
    # (#195). ``_spawn_child`` (UNION, OPTIONAL subquery) carries it too.
    child_visitor.state.graph_scope = list(visitor.state.graph_scope)
    if substitute:
        # EXISTS substitutes the outer row into its pattern (§18.6): those
        # variables are constants throughout it, inside an OPTIONAL body too,
        # and an EXISTS inside an OPTIONAL body still reads the outer
        # variables the body hides (#125; pyoxigraph and rdflib agree).
        child_visitor.state.var_to_expr = {**visitor.state.hidden_outer, **child_visitor.state.var_to_expr}
        child_visitor.state.substituted = visitor.state.substituted | set(child_visitor.state.var_to_expr)
        # Substitution replaces only the variables the row binds; one an
        # OPTIONAL left unbound stays free in the pattern and matches any
        # value (#190).
        child_visitor.state.maybe_unbound = set(visitor.state.maybe_unbound) | {
            # A hidden outer variable the body does not bind again keeps
            # "can be unbound" too (an EXISTS in a nested group of an
            # OPTIONAL body; review of #196).
            name
            for name in visitor.state.hidden_outer_unbound
            if name not in visitor.state.var_to_expr
        }
    elif free_outer is not None:
        # MINUS whose body binds every shared variable (``emit_minus``): one
        # the outer row leaves unbound constrains nothing, so the body matches
        # any value for it; the guard below removes the row only when a
        # shared variable is bound on both sides (§8.3.4). It used to be
        # joined as a value, matched nothing, and kept the row (review of #196).
        child_visitor.state.maybe_unbound = set(free_outer)
    # Per-query state: names minted inside the probe never collide with the
    # outer scope's ``_path_<n>`` / ``_bn_…``, and a class under another
    # tenant root is refused in the probe as outside it (#211).
    visitor.state.share_query_state(child_visitor.state)
    if allow_optional_rebind:
        # Switch the child out of "reject re-bind" mode (ADR-0002
        # Problem 2) into conditional-add mode; visit_LeftJoin appends
        # one entry per re-binding optional triple.
        child_visitor.state.optional_rebind_sink = []

    child_visitor.visit(inner_pattern)

    sink = child_visitor.state.optional_rebind_sink or []
    if sink_only and (overlap_var_names is None or not sink or overlap_var_names - {v for v, _, _ in sink}):
        raise _SinkDoesNotCover
    elif overlap_var_names is not None and free_outer is not None:
        # The body binds every shared variable in every solution, so the
        # domains overlap exactly where the outer row binds one of them. A
        # shared variable the outer row certainly binds makes that always.
        if not (overlap_var_names - free_outer):
            terms = [f"{visitor.state.var_to_expr[name]} != null" for name in sorted(free_outer)]
            child_builder.filter_raw(terms[0] if len(terms) == 1 else "(" + " || ".join(terms) + ")")
    elif overlap_var_names is not None and sink:
        soft_vars = {var for var, _, _ in sink}
        # A shared var bound by a *required* inner triple (not in the
        # sink) already carries a hard equality FILTER ⇒ overlap is
        # guaranteed and no guard is needed. Only when every shared var
        # is optional-bound must we add the §8.3.4 disjoint-domain guard.
        if not (overlap_var_names - soft_vars):
            terms = [
                f"({value} != null && {bound} != null && {value} == {bound})" for _, value, bound in sink
            ]
            overlap = terms[0] if len(terms) == 1 else "(" + " || ".join(terms) + ")"
            child_builder.filter_raw(overlap)

    # Short-circuit the cursor: we only need to know whether ≥ 1
    # row matches, not how many, so LIMIT 1 + RETURN 1 is the
    # minimal probe shape.
    child_builder.limit(1)
    child_builder.return_scalar("1")

    return visitor.builder.absorb_child(child_builder)
