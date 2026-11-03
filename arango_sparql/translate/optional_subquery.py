"""General OPTIONAL (LeftJoin) emitter: the optional pattern as a correlated subquery.

``visit_LeftJoin`` lowers the common OPTIONAL shapes inline — attribute
reads and single-step edges from a document the required side already
opened, and ADR-0002's RPT-native single-triple scan. Anything else used
to raise: an OPTIONAL whose subject the required side bound only as a
value (``OPTIONAL { ?study :hasOrganisation ?org . ?org :name ?n }``), or
whose body is not a plain BGP (``OPTIONAL { … BIND(…) }``). LDBC SNB's
reference reads use both (IS6, IC1, IC5, IC10, IC14; PRD §13.6).

This emitter is ADR-0002 Option A generalised from one triple to any
pattern. SPARQL 1.1 §18.5 defines ``LeftJoin(Ω1, Ω2, F)`` as every
compatible pair of solutions that satisfies F, plus every Ω1 solution with
no such partner, unextended. The AQL is exactly that::

    LET optsub = (
      <the optional pattern, translated by a child visitor that sees the
       required side's bindings, so shared variables become equality
       FILTERs; then FILTER F>
      RETURN { <each variable the pattern binds>: <its expression> }
    )
    FOR optrow IN (LENGTH(optsub) > 0 ? optsub : [null])
      // ?v reads optrow.v — null (unbound) for the padded row

so a pattern matching k times yields k rows, and none yields one row with
the optional variables unbound. Variables it binds are recorded as possibly
unbound, which aggregates rely on (SPARQL §18.5.1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .builder import attribute_ref
from .union_paths import _spawn_child

if TYPE_CHECKING:
    from .visitor import AlgebraVisitor

#: Internal sigils (property-path intermediates, BGP blank nodes): never
#: projected out of the subquery.
_INTERNAL_PREFIXES = ("_path_", "_bn_")


def emit_optional_subquery(visitor: AlgebraVisitor, node: Any) -> None:
    """Emit ``LeftJoin(p1, p2, expr)`` with ``p1`` already visited."""
    outer = set(visitor.state.var_to_expr)
    child = _spawn_child(visitor)
    # The body is evaluated on its own, then left-joined (SPARQL 1.1 §18.5):
    # it shares with the outer pattern only the variables it binds itself.
    # Seeded with every outer binding, a BIND, a nested group's FILTER or a
    # sub-select in it read outer variables that are not in its scope (#125:
    # ``OPTIONAL { ?p :age ?a BIND(CONCAT(?n, "!") AS ?v) }`` bound ``?v``).
    # The OPTIONAL's own condition (``expr``, its top-level FILTER) is the
    # left join's, evaluated on the joined row, so it sees them all.
    hidden = _hide_unshared(child, outer - {str(v) for v in getattr(node.p2, "_vars", ())})
    child.visit(node.p2)
    expr = getattr(node, "expr", None)
    if expr is not None and getattr(expr, "name", "") != "TrueFilter":
        child.state.var_to_expr.update(hidden)
        child.builder.filter_raw(child._translate_expr(expr))
    if not child.builder.has_for_clause():
        # A body that binds nothing from a collection (``OPTIONAL { BIND(…) }``):
        # give it a one-row scope, as an empty BGP does.
        child.builder.for_inline(child.builder.fresh_alias(prefix="empty"), "[1]")

    new_vars = [
        name
        for name in child.state.var_to_expr
        if name not in outer and not name.startswith(_INTERNAL_PREFIXES)
    ]
    if new_vars:
        child.builder.return_object([(name, child.state.var_to_expr[name]) for name in new_vars])
    else:
        # Binds nothing new: the OPTIONAL still repeats each required row once
        # per match (SPARQL multiset semantics), so count matches.
        child.builder.return_scalar("1")

    inner_aql = visitor.builder.absorb_child(child.builder)
    indented = "\n".join("  " + line for line in inner_aql.splitlines())
    sub_alias = visitor.builder.fresh_alias(prefix="optsub")
    visitor.builder.let(sub_alias, f"(\n{indented}\n)")
    row_alias = visitor.builder.fresh_alias(prefix="optrow")
    visitor.builder.for_inline(row_alias, f"(LENGTH({sub_alias}) > 0 ? {sub_alias} : [null])")
    for name in new_vars:
        visitor.state.var_to_expr[name] = attribute_ref(row_alias, name)
        visitor.state.maybe_unbound.add(name)


def _hide_unshared(child: AlgebraVisitor, names: set[str]) -> dict[str, str]:
    """Take *names* out of *child*'s scope; return their expressions."""
    state = child.state
    hidden = {name: state.var_to_expr.pop(name) for name in sorted(names) if name in state.var_to_expr}
    for name in hidden:
        state.var_to_doc_alias.pop(name, None)
        state.var_to_rpt_class.pop(name, None)
        state.maybe_unbound.discard(name)
    return hidden


def needs_subquery(visitor: AlgebraVisitor, p2: Any) -> bool:
    """Whether ``visit_LeftJoin`` should hand *p2* to :func:`emit_optional_subquery`.

    True for exactly the shapes the inline emitter refuses: a body that is
    not a plain BGP, or a BGP triple whose subject is not a document the
    required side opened, or whose object is not a variable. A variable
    predicate stays refused (ADR-0002 Option B is deferred). Inside a MINUS probe (``optional_rebind_sink``) the inline
    emitter's conditional-add semantics apply, so it keeps those.
    """
    from rdflib import URIRef, Variable

    if visitor.state.optional_rebind_sink is not None:
        return False
    if getattr(p2, "name", None) != "BGP":
        return True
    triples = getattr(p2, "triples", []) or []
    if any(isinstance(p, Variable) for _s, p, _o in triples):
        # A variable predicate in an OPTIONAL is ADR-0002 Option B, which was
        # deferred: without declared properties ``?p`` binds attribute names,
        # not IRIs. Keep refusing it rather than change that decision here.
        return False
    for s, p, o in triples:
        if not (isinstance(s, Variable) and str(s) in visitor.state.var_to_doc_alias):
            return True
        # A property path (``:replyOf*``) is not a URIRef either: the inline
        # emitter reads one attribute or one edge, so paths go to the subquery.
        if not isinstance(p, URIRef) or not isinstance(o, Variable):
            return True
    return False
