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

from rdflib import Variable

from .builder import attribute_ref
from .union_paths import _spawn_child

if TYPE_CHECKING:
    from .visitor import AlgebraVisitor

#: Internal sigils (property-path intermediates, BGP blank nodes): never
#: projected out of the subquery.
_INTERNAL_PREFIXES = ("_path_", "_bn_")


def group_scoped_variables(visitor: AlgebraVisitor, scope_before: set[str], node: Any) -> set[str]:
    """Variables of ``LeftJoin(p1, p2)`` that its optional side must bind on its own.

    The left join is one group, evaluated on its own and then joined with
    the solutions around it (SPARQL 1.1 §18.5, §18.3). The translator
    visits the group in sequence after them, so ``p2`` sees every variable
    already in scope. That substitution equals the join only for a
    variable the group binds in every solution. A variable bound before the
    group (*scope_before*) that ``p1`` does not bind but ``p2`` does
    (``{ ?s :knows ?x OPTIONAL { ?s :name ?n OPTIONAL { ?x :email ?m } } }``)
    can be left unbound by the group, so the group's ``?x`` has to be
    found without the outer one and compared after (#194).

    A variable an EXISTS substitutes is a constant throughout its pattern
    (§18.6), so it stays shared. A MINUS's right side is evaluated on its
    own too (§18.5), so the rule holds inside its probe, except where the
    probe's inline conditional add already applies it: an optional side
    whose every subject is a document the required side opened reads one
    value per row and tests it against the outer one
    (``minus_exists.yml``, ``tests/cross/test_minus_optional_cross.py``).
    """
    if not scope_before:
        return set()
    if visitor.state.optional_rebind_sink is not None and _inline_conditional_add(visitor, node.p2):
        return set()
    loose = (scope_before & bound_variables(node.p2)) - bound_variables(node.p1) - visitor.state.substituted
    return {name for name in loose if not name.startswith(_INTERNAL_PREFIXES)}


def _inline_conditional_add(visitor: AlgebraVisitor, p2: Any) -> bool:
    """Whether a MINUS probe's inline emitter lowers *p2* itself (documents it opened).

    Not a triples-table subject: the inline reads are document attribute
    paths, which it refuses on a triples row (#189).
    """
    if getattr(p2, "name", None) != "BGP":
        return False
    triples = getattr(p2, "triples", None) or []
    return bool(triples) and all(
        isinstance(s, Variable)
        and str(s) in visitor.state.var_to_doc_alias
        and str(s) not in visitor.state.var_to_rpt_class
        for s, _p, _o in triples
    )


def emit_optional_subquery(visitor: AlgebraVisitor, node: Any, loose: set[str] | None = None) -> None:
    """Emit ``LeftJoin(p1, p2, expr)`` with ``p1`` already visited.

    *loose* names outer variables the body must bind on its own and join
    by compatibility (:func:`group_scoped_variables`).
    """
    loose = loose or set()
    outer = set(visitor.state.var_to_expr)
    child = _spawn_child(visitor)
    # The body is evaluated on its own, then left-joined (SPARQL 1.1 §18.5):
    # it shares with the outer pattern only the variables it binds itself.
    # Seeded with every outer binding, a BIND, a nested group's FILTER or a
    # sub-select in it read outer variables that are not in its scope (#125:
    # ``OPTIONAL { ?p :age ?a BIND(CONCAT(?n, "!") AS ?v) }`` bound ``?v``).
    # The OPTIONAL's own condition (``expr``, its top-level FILTER) is the
    # left join's, evaluated on the joined row, so it sees them all.
    hidden = _hide_unshared(child, ((outer - bound_variables(node.p2)) | loose) - visitor.state.substituted)
    child.state.hidden_outer = {**visitor.state.hidden_outer, **hidden}
    child.visit(node.p2)
    # A loose variable the body bound is the group's own (its condition
    # below reads that value too); it is compared with the outer one after
    # the padding, below.
    rejoined = [name for name in sorted(loose) if name in hidden and name in child.state.var_to_expr]
    expr = getattr(node, "expr", None)
    if expr is not None and getattr(expr, "name", "") != "TrueFilter":
        child.state.var_to_expr.update(
            {name: value for name, value in hidden.items() if name not in rejoined}
        )
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
    # An outer variable an earlier OPTIONAL can leave unbound joins any value
    # in the body (SPARQL 1.1 §18.3) and then takes the body's: the body's
    # expression for it changed (``NOT_NULL(outer, inner)``), so carry it out
    # too, or the solution keeps the variable unbound (#190).
    filled = [
        name
        for name in child.state.var_to_expr
        if name in outer
        and name not in loose
        and name in visitor.state.maybe_unbound
        and child.state.var_to_expr[name] != visitor.state.var_to_expr[name]
        and not name.startswith(_INTERNAL_PREFIXES)
    ]
    projected = new_vars + filled + rejoined
    if projected:
        child.builder.return_object([(name, child.state.var_to_expr[name]) for name in projected])
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
    for name in filled:
        # Unchanged where it was bound; the body's value where it was not;
        # still unbound on the padded row.
        visitor.state.var_to_expr[name] = (
            f"NOT_NULL({visitor.state.var_to_expr[name]}, {attribute_ref(row_alias, name)})"
        )
    for name in rejoined:
        # The group's solution (the padded row included) joins the
        # surrounding one by compatibility (SPARQL 1.1 §18.3): either side
        # unbound, or both equal. It is tested here, after the padding, as
        # the group was evaluated without the outer value (#194). A row that
        # fails it is not a solution of the enclosing join; inside an
        # enclosing OPTIONAL's body, that body then pads.
        outer_expr = visitor.state.var_to_expr[name]
        inner_expr = attribute_ref(row_alias, name)
        visitor.builder.filter_raw(
            f"({outer_expr} == null || {inner_expr} == null || {inner_expr} == {outer_expr})"
        )
        if visitor.state.optional_rebind_sink is not None:
            # Inside a MINUS probe the variable is shared only where both
            # sides bind it: the probe's disjoint-domain guard (§8.3.4)
            # reads it from the sink, as for an inline conditional add.
            visitor.state.optional_rebind_sink.append((name, inner_expr, outer_expr))
        if name in visitor.state.maybe_unbound:
            visitor.state.var_to_expr[name] = f"NOT_NULL({outer_expr}, {inner_expr})"


def bound_variables(node: Any) -> set[str]:
    """The variables a graph pattern binds (SPARQL 1.1 §18.2.1, in-scope).

    Not rdflib's ``_vars``: it leaves out VALUES columns (the rows are dicts
    its traversal does not walk), is unset inside EXISTS patterns, and counts
    a variable a nested FILTER only reads (review of #140). Pattern terms,
    BIND targets, VALUES columns, sub-select projections, a GRAPH variable and
    a MINUS's left side bind; a FILTER or EXISTS does not.
    """
    if node is None:
        return set()
    name = getattr(node, "name", None)
    if name == "BGP":
        return {
            str(t) for triple in node.triples for t in (triple[0], triple[2]) if isinstance(t, Variable)
        } | {str(triple[1]) for triple in node.triples if isinstance(triple[1], Variable)}
    if name == "values":
        return {str(var) for row in node.get("res") or [] for var in row}
    if name in ("Project", "SelectQuery"):
        return {str(v) for v in node.get("PV") or []}
    if name == "Extend":
        return bound_variables(node.p) | {str(node.var)}
    if name == "Graph":
        term = node.term
        return bound_variables(node.p) | ({str(term)} if isinstance(term, Variable) else set())
    if name == "Minus":
        return bound_variables(node.p1)
    if name == "AggregateJoin":
        return bound_variables(node.p) | {str(a.res) for a in node.A}
    out: set[str] = set()
    for key in ("p", "p1", "p2"):
        child = node.get(key) if hasattr(node, "get") else None
        if child is not None and hasattr(child, "name"):
            out |= bound_variables(child)
    return out


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
        if str(s) in visitor.state.var_to_rpt_class:
            # A subject read from a triples table (RPT) is a row, not a
            # document: its properties are other rows, which the inline
            # emitter's attribute paths (``doc.<local name>``) cannot read; it
            # answered every such OPTIONAL with nothing. The subquery runs the
            # body through the triples-table reader instead.
            return True
        if (
            visitor.state.var_to_doc_alias[str(s)] in visitor.state.copy_members
            and isinstance(o, Variable)
            and str(o) not in visitor.state.var_to_expr
        ):
            # A union row standing for every copy of its IRI: its attributes
            # are read from all of them, a FOR of values the inline emitter's
            # attribute paths cannot express (#147). A variable the required
            # side binds stays with the inline emitter, which refuses the
            # re-bind: the subquery path drops its value (review of #171).
            return True
        # A property path (``:replyOf*``) is not a URIRef either: the inline
        # emitter reads one attribute or one edge, so paths go to the subquery.
        if not isinstance(p, URIRef) or not isinstance(o, Variable):
            return True
    return False
