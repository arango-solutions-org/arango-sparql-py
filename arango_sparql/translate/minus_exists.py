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

from typing import TYPE_CHECKING, Any

from rdflib import Variable

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
    2. Collect every SPARQL variable referenced anywhere inside
       ``p2`` (walks the inner algebra tree). Intersect with the
       outer's ``var_to_expr`` keys — that's the *shared-variable
       set* the SPARQL spec uses to define compatibility.
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

    inner_vars = _collect_referenced_variables(node.p2)
    shared = {v for v in inner_vars if str(v) in visitor.state.var_to_expr}
    if not shared:
        # SPARQL spec §8.3.4 — disjoint domain ⇒ no-op. Important
        # divergence from NOT EXISTS, which excludes any outer row
        # whenever the inner matches even one row.
        return

    inner_aql = _translate_probe(
        visitor,
        node.p2,
        allow_optional_rebind=True,
        overlap_var_names={str(v) for v in shared},
    )

    probe_alias = visitor.builder.fresh_alias(prefix="minus_probe")
    visitor.builder.let(probe_alias, f"LENGTH((\n{_indent(inner_aql)}\n))")
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
    visitor.builder.let(probe_alias, f"LENGTH((\n{_indent(inner_aql)}\n))")

    return f"{probe_alias} == 0" if negated else f"{probe_alias} > 0"


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _translate_probe(
    visitor: AlgebraVisitor,
    inner_pattern: Any,
    *,
    allow_optional_rebind: bool = False,
    overlap_var_names: set[str] | None = None,
    substitute: bool = False,
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

    Also NOT carried over: ``var_to_rpt_class`` (RPT class
    bindings are query-scope; an inner pattern that references
    the same SPARQL variable should re-resolve it against the
    inner's own type-pattern, not adopt the outer's RPT class)
    and ``tenant_entity`` (the inner re-derives its own tenant
    scope from the resolver).
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
    )
    # Pre-seed the child's var_to_expr with the outer scope's
    # bindings so shared variables turn into equality FILTERs
    # against the outer's expressions (the SPARQL compatibility
    # check, expressed in AQL).
    child_visitor.state.var_to_expr = dict(visitor.state.var_to_expr)
    if substitute:
        # EXISTS substitutes the outer row into its pattern (§18.6): those
        # variables are constants throughout it, inside an OPTIONAL body too,
        # and an EXISTS inside an OPTIONAL body still reads the outer
        # variables the body hides (#125; pyoxigraph and rdflib agree).
        child_visitor.state.var_to_expr = {**visitor.state.hidden_outer, **child_visitor.state.var_to_expr}
        child_visitor.state.substituted = visitor.state.substituted | set(child_visitor.state.var_to_expr)
    # Shared, so names minted inside the probe never collide with the
    # outer scope's ``_path_<n>`` / ``_bn_…`` (see _BindingState.counters).
    child_visitor.state.counters = visitor.state.counters
    if allow_optional_rebind:
        # Switch the child out of "reject re-bind" mode (ADR-0002
        # Problem 2) into conditional-add mode; visit_LeftJoin appends
        # one entry per re-binding optional triple.
        child_visitor.state.optional_rebind_sink = []

    child_visitor.visit(inner_pattern)

    sink = child_visitor.state.optional_rebind_sink or []
    if overlap_var_names is not None and sink:
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


def _collect_referenced_variables(node: Any) -> set[Variable]:
    """Walk an algebra subtree and collect every ``Variable`` term
    referenced in triples or expressions.

    Used by :func:`emit_minus` to compute the shared-variable set
    against the outer scope. We deliberately walk the algebra
    tree rather than reading rdflib's ``_vars`` because the
    latter is a set-iteration artefact populated lazily and not
    always present on every node type we care about.

    Recurses into ``.p`` / ``.p1`` / ``.p2`` / ``.graph`` /
    ``.expr`` children and reads ``.triples`` lists on BGP-like
    nodes. Variables inside ``Path`` predicates are picked up
    when the path's args list contains them (rare but legal —
    a ``Path`` whose start/end is a variable shows up via the
    enclosing triple's subject/object slots, not the path
    expression itself).
    """
    result: set[Variable] = set()
    _walk_for_vars(node, result)
    return result


def _walk_for_vars(node: Any, out: set[Variable]) -> None:
    """Recursive helper for :func:`_collect_referenced_variables`."""
    if node is None:
        return
    if isinstance(node, Variable):
        out.add(node)
        return

    triples = getattr(node, "triples", None)
    if triples:
        for triple in triples:
            for term in triple:
                if isinstance(term, Variable):
                    out.add(term)

    for attr in ("p", "p1", "p2", "graph", "expr", "other"):
        child = getattr(node, attr, None)
        if child is not None and (isinstance(child, Variable) or hasattr(child, "name")):
            _walk_for_vars(child, out)


def _indent(text: str) -> str:
    """Two-space indent each line — purely cosmetic for the
    generated LET block. Keeps EXPLAIN-output readable when an
    inner probe is several lines long. Matches the indentation
    style :meth:`AqlQueryBuilder.for_subquery` uses for the
    ``ToMultiSet`` branch."""
    return "\n".join("  " + line for line in text.splitlines())
