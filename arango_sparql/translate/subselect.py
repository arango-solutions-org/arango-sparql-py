"""SPARQL 1.1 sub-SELECT + VALUES emission (`ToMultiSet` algebra node).

The visitor's main dispatcher hands every ``ToMultiSet`` node to
:func:`emit_to_multiset` below, which routes to one of two branches
based on the inner pattern:

* **Sub-SELECT** (``ToMultiSet → Project → ...``) — the inner
  query is translated as a self-contained AQL sub-query via a
  *child builder* spawned through
  :meth:`AqlQueryBuilder.create_child`; the outer scope then
  iterates the sub-query's rows with ``FOR <row> IN (<inner AQL>)``.
  Slice / Distinct / OrderBy wrappers around the inner Project are
  preserved in the child visitor's walk so the inner sub-query
  carries its own LIMIT / SORT / DISTINCT.

* **VALUES** (``ToMultiSet → values``) — inline binding data
  (SPARQL 1.1 §10.2). Each row is converted to a Python dict,
  bound as a single AQL list-of-objects value, and iterated with
  ``FOR <row> IN @<bind>``. UNDEF in any row becomes JSON ``null``.

Both branches use the same outer-scope plumbing: each
projected/declared variable becomes ``<row>.<var>`` in
``var_to_expr``; shared variables (already bound by an outer
pattern) get an equality FILTER to enforce the SPARQL join.

Lives next to :mod:`arango_sparql.translate.paths` and
:mod:`arango_sparql.translate.variable_predicates` for the same
reason those exist: keeping ``visitor.py`` under the 1500-line
cap from ``.cursor/rules/modularity-and-structure.mdc``. Helpers
take the visitor instance as their first argument and reach into
its public-ish surface (``builder``, ``state``, ``resolver``,
``tenant_id``) — same convention.

Legacy reference: ``references/arango-sparql/src/lib/
pgt-translator.js`` has no honest sub-SELECT path; its
``visit_GroupGraphPattern`` flattens nested groups into the
parent BGP, which conflates inner- and outer-scope variables.
VALUES is handled inline in the BGP emitter as a binding-set
injection rather than a separate algebra node. We emit both as
honest scoped sub-queries here — the rdflib algebra distinguishes
them via the ``ToMultiSet`` wrapper and we honour that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rdflib import Literal, URIRef

from ..errors import UnsupportedSparqlError
from .builder import attribute_ref

if TYPE_CHECKING:
    from .visitor import AlgebraVisitor


def emit_to_multiset(visitor: AlgebraVisitor, node: Any) -> None:
    """Top-level dispatcher for ``ToMultiSet`` algebra nodes.

    ``ToMultiSet`` is the wrapper rdflib uses around both
    inline-data (``values``) and sub-SELECTs (``Project`` —
    possibly wrapped in Slice / Distinct / OrderBy). Both
    branches produce a row-stream the outer scope joins against.
    """
    inner = node.p
    inner_name = getattr(inner, "name", None)

    if inner_name == "values":
        _emit_values(visitor, inner)
        return

    _emit_subselect(visitor, node, inner)


def _emit_subselect(
    visitor: AlgebraVisitor,
    multiset_node: Any,
    inner: Any,
) -> None:
    """Translate a sub-SELECT (``ToMultiSet → [Slice/Distinct/OrderBy*] → Project``).

    Variable scoping follows SPARQL semantics:

    * The inner pattern has its own ``_BindingState`` — inner
      variables that *aren't* in the projection list don't
      escape. A nested ``?p`` inside the sub-SELECT can't be
      referenced from the outer query, exactly as SPARQL §18.2.2
      mandates. We get this for free by spawning a fresh
      :class:`AlgebraVisitor` whose ``state`` is a default
      ``_BindingState``.
    * The inner projection variables (``inner.PV``) become bound
      to ``<row>.<var>`` in the OUTER ``var_to_expr``. If the
      outer scope already binds any of those variables (a shared
      variable forces the SPARQL join), we emit an equality
      FILTER — the same recipe ``_emit_triple`` / ``_bind_subject``
      use for BGP-level shared vars.

    Carry-overs to the child visitor:

    * ``resolver`` — same instance.
    * ``tenant_id`` — the inner sub-query participates in the
      same tenant scope as the outer (PRD §6.5.1); refusing to
      propagate would silently drop tenant FILTERs from
      sub-queries.
    * ``explicit_projection`` — set from ``inner.PV`` so the
      inner ``_emit_projection`` knows which keys to put in its
      ``RETURN { ... }`` and in which order. We use ``inner.PV``
      directly even though it's a set under the hood, because
      the W3C corpus's sub-SELECT tests don't depend on inner-
      projection order (the outer query references them by name
      through ``<row>.<var>``, not by positional access).
    """
    # The Project node holds the projection list (PV), but
    # sub-SELECTs that use LIMIT / OFFSET / ORDER BY / DISTINCT
    # wrap Project in a chain of Slice / OrderBy / Distinct
    # nodes that rdflib hangs OUTSIDE the Project (so the
    # algebra reads ``ToMultiSet → Slice → Project → BGP``,
    # not ``ToMultiSet → Project → Slice → BGP``). We unwrap
    # those single-child operators to find the Project for PV
    # extraction — the child visitor still walks the FULL
    # ``inner`` subtree so the wrappers' own AQL (LIMIT, SORT,
    # DISTINCT) lands in the inner sub-query block.
    project = inner
    while project is not None and getattr(project, "name", None) in ("Slice", "OrderBy", "Distinct"):
        project = getattr(project, "p", None)
    if project is None or getattr(project, "name", None) != "Project":
        inner_name = getattr(inner, "name", None)
        raise UnsupportedSparqlError(
            f"ToMultiSet inner pattern resolves to "
            f"{getattr(project, 'name', None)!r} under "
            f"{inner_name!r}; expected Project (sub-SELECT must "
            f"carry an explicit projection list)"
        )

    inner_pv = list(project.PV)
    if not inner_pv:
        # ``SELECT * WHERE { … }`` inside a sub-query would be
        # legal SPARQL but produces no exposed variables — we'd
        # have no way to join the inner result back to the
        # outer scope. The W3C corpus doesn't exercise this
        # shape; defer rather than ship empty-row semantics.
        raise UnsupportedSparqlError("sub-SELECT with empty projection list is not supported")

    # Local import to avoid circular dependency at module import
    # time — :mod:`visitor` already imports this module.
    from .visitor import AlgebraVisitor

    child_builder = visitor.builder.create_child()
    child_visitor = AlgebraVisitor(
        builder=child_builder,
        resolver=visitor.resolver,
        tenant_id=visitor.tenant_id,
        base_iri=visitor.base_iri,
        explicit_projection=list(inner_pv),
    )
    child_visitor.visit(inner)

    inner_aql = visitor.builder.absorb_child(child_builder)

    row_alias = visitor.builder.fresh_alias(prefix="row")
    visitor.builder.for_subquery(row_alias, inner_aql)

    _bind_outer_scope(visitor, row_alias, [str(v) for v in inner_pv])
    # A column the sub-select can leave unbound (an OPTIONAL inside it, or a
    # MIN / MAX / SUM … that can be unbound) stays so outside (#59).
    visitor.state.maybe_unbound.update(
        str(v) for v in inner_pv if str(v) in child_visitor.state.maybe_unbound
    )


def _emit_values(visitor: AlgebraVisitor, values_node: Any) -> None:
    """SPARQL VALUES (§10.2) — inline binding data emission.

    ``rdflib.plugins.sparql.algebra.values`` carries a ``res``
    attribute (list of ``{Variable → Term}`` dicts). Each dict is
    one VALUES row; an UNDEF in any cell is represented by rdflib
    as the literal Python string ``'UNDEF'`` (NOT a missing key
    and NOT ``None``) — that's the rdflib internal contract,
    which we convert to JSON ``null`` so AQL evaluates equality
    joins correctly per the W3C spec ("UNDEF binds to no value").

    AQL shape::

        FOR <row> IN @<values_bind>
        -- @<values_bind> binds to a Python list of dicts:
        -- [{"x": "...", "y": 1}, {"x": "...", "y": null}, ...]

    Why bind-as-list rather than inline ``FOR row IN [...]``:

    * **Plan stability** — a bind variable keeps the AQL string
      length constant regardless of how many VALUES rows the
      query carries, which means the ArangoDB query cache keys
      off the same plan instead of one plan per row count.
    * **Bind-safety** — every URI / literal in the VALUES rows
      goes through the bind dict; the AQL string never contains
      a user-supplied value, matching the rest of the visitor's
      parameter-only-AQL rule.

    Variable iteration order: sorted by name. The W3C corpus's
    VALUES tests don't depend on output column order, but the
    sort gives us deterministic AQL across PYTHONHASHSEED runs —
    the same property ``visit_Project`` defends through
    ``explicit_projection``.
    """
    res = getattr(values_node, "res", None) or []

    var_names = sorted({str(v) for row in res for v in row.keys()})
    if not var_names:
        # ``VALUES ?x { }`` — declared variables but zero rows.
        # rdflib carries the declared-var list separately; if
        # ``res`` is empty AND no vars surface we have nothing
        # to bind. The W3C spec treats this as the empty
        # binding-set ⇒ outer query yields no rows. Emit an
        # AQL FOR over an empty list so the join collapses
        # correctly.
        empty_bind = visitor.builder.bind([], hint="values")
        row_alias = visitor.builder.fresh_alias(prefix="row")
        visitor.builder.for_values(row_alias, empty_bind)
        return

    # Local import to break the visitor ↔ subselect cycle at
    # module-import time.
    from .visitor import _term_to_python

    rows_python: list[dict[str, Any]] = []
    for row in res:
        # rdflib's row dict keys are ``Variable`` objects; we
        # index by string name for stable JSON output.
        term_by_name = {str(k): v for k, v in row.items()}
        rows_python.append(
            {
                name: (
                    None
                    if not isinstance(term_by_name.get(name), (URIRef, Literal))
                    else _term_to_python(term_by_name[name])
                )
                for name in var_names
            }
        )

    values_bind = visitor.builder.bind(rows_python, hint="values")
    row_alias = visitor.builder.fresh_alias(prefix="row")
    visitor.builder.for_values(row_alias, values_bind)

    _bind_outer_scope(visitor, row_alias, var_names)
    # A variable with UNDEF in some row is unbound in that solution.
    visitor.state.maybe_unbound.update(
        name for name in var_names if any(row[name] is None for row in rows_python)
    )


def _bind_outer_scope(
    visitor: AlgebraVisitor,
    row_alias: str,
    var_names: list[str],
) -> None:
    """Bind ``<row>.<var>`` into the outer ``var_to_expr`` for each var.

    Shared variables (already bound by a prior outer pattern)
    receive an equality FILTER instead of being rebound — that's
    the SPARQL join recipe used uniformly across the visitor (see
    ``_emit_triple``'s right-side Variable branch). Two callers
    share this helper because sub-SELECT and VALUES need the same
    behaviour: both contribute a binding-set the outer query joins
    against.
    """
    for var_name in var_names:
        new_expr = attribute_ref(row_alias, var_name)
        existing = visitor.state.var_to_expr.get(var_name)
        if existing is None:
            visitor.state.var_to_expr[var_name] = new_expr
            continue
        if existing != new_expr:
            visitor.builder.filter_raw(f"{new_expr} == {existing}")
