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

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from rdflib import Variable

from .builder import attribute_ref
from .mixed_store import route_subject
from .union_paths import _spawn_child

if TYPE_CHECKING:
    from .visitor import AlgebraVisitor

#: Internal sigils (property-path intermediates, BGP blank nodes): never
#: projected out of the subquery.
_INTERNAL_PREFIXES = ("_path_", "_bn_")


def group_scoped_variables(visitor: AlgebraVisitor, scope_before: set[str], node: Any) -> set[str]:
    """Variables of ``LeftJoin(p1, p2)`` its optional side binds on its own.

    A group is evaluated on its own and then joined with the solutions
    around it (SPARQL 1.1 §18.5, §18.3). The translator visits it in
    sequence after them, so ``p2`` sees every variable already in scope.
    That substitution equals the join only for a variable the group binds
    in every solution. A variable bound before the group (*scope_before*)
    that ``p1`` does not bind in every solution but ``p2`` binds
    (``?s :knows ?x OPTIONAL { ?s :name ?n OPTIONAL { ?x :email ?m } } }``)
    can be left unbound by the group, so whether ``p2`` matches is decided
    without the outer value, and its matches are compared with it (#194;
    :func:`emit_optional_subquery`).

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
    loose = (scope_before & bound_variables(node.p2)) - certain_variables(node.p1) - visitor.state.substituted
    return {name for name in loose if not name.startswith(_INTERNAL_PREFIXES)}


def certain_variables(node: Any) -> set[str]:
    """The variables a graph pattern binds in every solution.

    An OPTIONAL's side, a MINUS's right side, a variable only some UNION
    arms bind, a VALUES column with an ``UNDEF`` cell and a sub-select
    column its own pattern can leave unbound can each be unbound. A BIND
    target counts as bound (a BIND onto an outer variable is refused), as
    does every variable of a pattern this does not look inside: those keep
    the sequential translation they had before #194.
    """
    if node is None:
        return set()
    name = getattr(node, "name", None)
    if name == "BGP":
        return bound_variables(node)
    if name == "Join":
        return certain_variables(node.p1) | certain_variables(node.p2)
    if name in ("LeftJoin", "Minus"):
        return certain_variables(node.p1)
    if name in ("Filter", "ToMultiSet", "Distinct", "Reduced", "OrderBy", "Slice", "Group", "AggregateJoin"):
        # An aggregate's result can be unbound (SAMPLE, MIN over nothing):
        # only the grouped pattern's own variables count (review of #197).
        return certain_variables(node.p)
    if name in ("Project", "SelectQuery"):
        # A sub-select's column is bound in every row only if its own
        # pattern binds it in every solution (review of #197).
        return {str(v) for v in node.get("PV") or []} & certain_variables(node.p)
    if name == "values":
        # A column with an ``UNDEF`` cell (or a row without it) can be unbound.
        rows = node.get("res") or []
        columns = {str(var) for row in rows for var in row}
        return {
            column
            for column in columns
            if all(row.get(Variable(column)) not in (None, "UNDEF") for row in rows)
        }
    if name == "Union":
        return certain_variables(node.p1) & certain_variables(node.p2)
    if name == "Extend":
        return certain_variables(node.p) | {str(node.var)}
    if name == "Graph":
        term = node.term
        return certain_variables(node.p) | ({str(term)} if isinstance(term, Variable) else set())
    return bound_variables(node)


def group_own_value(visitor: AlgebraVisitor, node: Any, name: str) -> str | None:
    """The value the visited pattern *node* gave *name* itself, without the outer one.

    ``"null"`` when it does not bind the variable; ``None`` when it binds it
    only in some solutions in a way this does not track (a UNION arm, a
    VALUES column with ``UNDEF``, a sub-select): the caller refuses then. A group-scoped OPTIONAL records its own value
    (:func:`emit_optional_subquery`); a join combines its sides'.
    """
    if name not in bound_variables(node):
        return "null"
    kind = getattr(node, "name", None)
    if kind == "LeftJoin":
        return visitor.state.group_own.get(id(node), {}).get(name)
    if kind == "Join":
        left = group_own_value(visitor, node.p1, name)
        right = group_own_value(visitor, node.p2, name)
        if left is None or right is None:
            return None
        return _coalesce(left, right)
    if kind == "Filter":
        return group_own_value(visitor, node.p, name)
    return None


def _coalesce(first: str, second: str) -> str:
    if first == "null":
        return second
    if second == "null":
        return first
    return f"NOT_NULL({first}, {second})"


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


class _Body:
    """The optional side translated once by a child visitor (:func:`_translate_body`)."""

    def __init__(self, aql: str, values: dict[str, str]) -> None:
        self.aql = aql
        #: Variable → the body's AQL for it, for each variable it returns.
        self.values = values


def _translate_body(
    visitor: AlgebraVisitor,
    node: Any,
    *,
    hide: set[str],
    compare: dict[str, str],
    own: dict[str, str],
    returns: Callable[[dict[str, str]], list[str]] | None,
) -> _Body:
    """Translate ``node.p2`` (and the left join's condition) in a child visitor.

    *hide* are outer variables the body binds on its own; *compare* maps
    some of them to the value each match must be compatible with (§18.3).
    The condition reads a group-scoped variable as the group would bind it:
    *own*'s value where set, the body's where not. *returns* picks the
    variables to return from the child's bindings, in order (``None``: the
    probe shape, ``LIMIT 1`` and ``1``).
    """
    outer = set(visitor.state.var_to_expr)
    child = _spawn_child(visitor)
    # The body is evaluated on its own, then left-joined (SPARQL 1.1 §18.5):
    # it shares with the outer pattern only the variables it binds itself.
    # Seeded with every outer binding, a BIND, a nested group's FILTER or a
    # sub-select in it read outer variables that are not in its scope (#125:
    # ``OPTIONAL { ?p :age ?a BIND(CONCAT(?n, "!") AS ?v) }`` bound ``?v``).
    # The OPTIONAL's own condition (``expr``, its top-level FILTER) is the
    # left join's, evaluated on the joined row, so it sees them all.
    # The active GRAPH variables are not hidden: the body is matched in the
    # scope's graph (SPARQL 1.1 §18.6), the outer row's. Hidden, ``?g`` left
    # the body free to read any named graph (review of #198).
    graph_vars = {str(t) for t in visitor.state.graph_scope if isinstance(t, Variable)}
    hidden = _hide_unshared(
        child, ((outer - bound_variables(node.p2)) | hide) - visitor.state.substituted - graph_vars
    )
    child.state.hidden_outer = {**visitor.state.hidden_outer, **hidden}
    child.state.hidden_outer_unbound = visitor.state.hidden_outer_unbound | (
        set(hidden) & visitor.state.maybe_unbound
    )
    child.visit(node.p2)
    for name, target in compare.items():
        if name in child.state.var_to_expr:
            value = child.state.var_to_expr[name]
            child.builder.filter_raw(f"({value} == null || {target} == null || {value} == {target})")
    values = {
        name: child.state.var_to_expr[name] for name in (returns(child.state.var_to_expr) if returns else [])
    }
    expr = getattr(node, "expr", None)
    if expr is not None and getattr(expr, "name", "") != "TrueFilter":
        child.state.var_to_expr.update({name: value for name, value in hidden.items() if name not in own})
        for name, own_value in own.items():
            child.state.var_to_expr[name] = _coalesce(own_value, child.state.var_to_expr.get(name, "null"))
        # ...and knows which of them an earlier OPTIONAL can leave unbound, so
        # an EXISTS in it leaves such a variable free (§18.3; review of #196).
        # A group-scoped variable's value there can be unbound too (#194).
        child.state.maybe_unbound |= (set(hidden) & visitor.state.maybe_unbound) | set(own)
        child.builder.filter_raw(child._translate_expr(expr))
    if not child.builder.has_for_clause():
        # A body that binds nothing from a collection (``OPTIONAL { BIND(…) }``):
        # give it a one-row scope, as an empty BGP does.
        child.builder.for_inline(child.builder.fresh_alias(prefix="empty"), "[1]")
    if returns is None:
        child.builder.limit(1)
        child.builder.return_scalar("1")
    elif values:
        child.builder.return_object(list(values.items()))
    else:
        # Binds nothing new: the OPTIONAL still repeats each required row once
        # per match (SPARQL multiset semantics), so count matches.
        child.builder.return_scalar("1")
    inner_aql = visitor.builder.absorb_child(child.builder)
    return _Body("\n".join("  " + line for line in inner_aql.splitlines()), values)


def emit_optional_subquery(visitor: AlgebraVisitor, node: Any, loose: dict[str, str] | None = None) -> None:
    """Emit ``LeftJoin(p1, p2, expr)`` with ``p1`` already visited.

    *loose* maps each group-scoped variable (:func:`group_scoped_variables`)
    to the value ``p1`` gave it itself (:func:`group_own_value`). A row
    of ``p1`` pads when ``p2`` has no match compatible with that value; the
    matches it keeps are those compatible with the variable's value in the
    whole row (the outer one too). Two subqueries say so: the matches, read
    with that value (an index lookup where the body binds the variable in
    every solution), and, when there are none, whether the body matches at
    all, which drops the row instead of padding it (#194).
    """
    loose = loose or {}
    state = visitor.state
    outer = set(state.var_to_expr)

    def returned(bindings: dict[str, str]) -> list[str]:
        # Variables the body binds that the outer row does not; outer
        # variables an earlier OPTIONAL can leave unbound that the body
        # filled (SPARQL 1.1 §18.3: its expression for them changed to
        # ``NOT_NULL(outer, inner)``, #190); and the group-scoped ones.
        fresh = [n for n in bindings if n not in outer and not n.startswith(_INTERNAL_PREFIXES)]
        filled = [
            n
            for n in bindings
            if n in outer
            and n not in loose
            and n in state.maybe_unbound
            and bindings[n] != state.var_to_expr[n]
            and not n.startswith(_INTERNAL_PREFIXES)
        ]
        return fresh + filled + [n for n in sorted(loose) if n in bindings]

    # A group-scoped variable the body binds in every solution, whose value
    # in the row cannot be null, is substituted (an index lookup); otherwise
    # the body binds it and each match is compared with the row's value.
    certain_body = certain_variables(node.p2)
    free = {n for n in loose if n not in certain_body or n in state.maybe_unbound}
    matches = _translate_body(
        visitor,
        node,
        hide=free,
        compare={name: state.var_to_expr[name] for name in sorted(free)},
        own=loose,
        returns=returned,
    )
    rejoined = [name for name in sorted(loose) if name in matches.values]
    new_vars = [name for name in matches.values if name not in outer]
    filled = [name for name in matches.values if name in outer and name not in loose]
    sub_alias = visitor.builder.fresh_alias(prefix="optsub")
    visitor.builder.let(sub_alias, f"(\n{matches.aql}\n)")
    rows = f"(LENGTH({sub_alias}) > 0 ? {sub_alias} : [null])"
    if loose:
        # No match compatible with the row: the body evaluated on its own may
        # still match, with other values, and then the group has no solution
        # compatible with the row, which is dropped rather than padded. Where
        # the group's own value is set it equals the row's (the matches are
        # compared with it), so the padding test is "no match" there; it
        # needs the body only where the group left the variable unbound, and
        # then with the variable free, which does not depend on the row's
        # value (no per-row scan of the whole body).
        settled = [own for own in loose.values() if own != "null"]
        free_probe = not settled or len(loose) == 1
        probe = _translate_body(
            visitor,
            node,
            hide=set(loose),
            compare={} if free_probe else {n: own for n, own in sorted(loose.items()) if own != "null"},
            own={n: "null" for n in loose} if free_probe else loose,
            returns=None,
        )
        probe_alias = visitor.builder.fresh_alias(prefix="optany")
        visitor.builder.let(probe_alias, f"(\n{probe.aql}\n)")
        if settled and free_probe:
            pad = f"({settled[0]} != null || LENGTH({probe_alias}) == 0 ? [null] : [])"
        else:
            pad = f"(LENGTH({probe_alias}) > 0 ? [] : [null])"
        rows = f"(LENGTH({sub_alias}) > 0 ? {sub_alias} : {pad})"
    row_alias = visitor.builder.fresh_alias(prefix="optrow")
    visitor.builder.for_inline(row_alias, rows)
    for name in new_vars:
        state.var_to_expr[name] = attribute_ref(row_alias, name)
        state.maybe_unbound.add(name)
    for name in filled:
        # Unchanged where it was bound; the body's value where it was not;
        # still unbound on the padded row.
        state.var_to_expr[name] = f"NOT_NULL({state.var_to_expr[name]}, {attribute_ref(row_alias, name)})"
    own_after: dict[str, str] = {}
    for name, own in loose.items():
        if name not in rejoined:
            own_after[name] = own
            continue
        value = attribute_ref(row_alias, name)
        own_after[name] = _coalesce(own, value)
        if state.optional_rebind_sink is not None:
            # Inside a MINUS probe the variable is shared only where both
            # sides bind it: the probe's disjoint-domain guard (§8.3.4)
            # reads it from the sink, as for an inline conditional add.
            state.optional_rebind_sink.append((name, value, state.var_to_expr[name]))
        if name in state.maybe_unbound:
            state.var_to_expr[name] = f"NOT_NULL({state.var_to_expr[name]}, {value})"
    if loose:
        # A later OPTIONAL in the same group decides its padding against
        # this group's own value (:func:`group_own_value`).
        state.group_own[id(node)] = own_after


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
        # not IRIs. Keep refusing it rather than change that decision here —
        # except on a subject the query never placed, in a mapping with
        # several stores (#191): the sub-query reads its triples from each
        # store, and the reader refuses when a document's ``?p`` would be a
        # name (:func:`.mixed_store.emit_routed_triple`).
        return any(
            isinstance(p, Variable) and route_subject(visitor, s, p) is not None for s, p, _o in triples
        )
    for s, p, o in triples:
        if not (isinstance(s, Variable) and str(s) in visitor.state.var_to_doc_alias):
            return True
        if isinstance(o, Variable) and str(o) in visitor.state.maybe_unbound:
            # An earlier OPTIONAL can leave the object unbound: the body joins
            # it (SPARQL 1.1 §18.3) rather than binding it afresh, which the
            # inline emitter would refuse as a re-bind (review of #196).
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
