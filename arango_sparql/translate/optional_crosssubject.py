"""RPT-native cross-subject ``OPTIONAL`` (ADR-0002 Problem 1, Option A).

A *cross-subject* OPTIONAL is one whose subject is bound by the required
side only as a **value** — the object of a prior triple — never as a
document the translator opened a ``FOR`` over (so it has a
``var_to_expr`` entry but no ``var_to_doc_alias`` entry):

    ?s a :Person ; :knows ?o .
    OPTIONAL { ?o ?p2 ?o2 }          # ?o is the *object* of :knows

Per ADR-0002 the difficulty of this construct is **storage-model
dependent**, and it is *trivial and spec-correct* only on RPT: the
triples table already holds every ``(subject, predicate, object)`` row,
so the OPTIONAL is a plain left-join scan of that table filtered on
``subject == <o>``. The variable predicate ``?p2`` is just the predicate
column — no ``ATTRIBUTES`` fan-out, no ``_uri → collection`` ambiguity.

This module implements **only** that RPT path; PG / LPG / default-
``Document`` cross-subject OPTIONALs still raise the structured
``UnsupportedSparqlError`` in ``visit_LeftJoin`` (ADR-0002 Options B/C,
deferred). Detection therefore gates on ``state.var_to_rpt_class`` being
populated — i.e. some subject in the query resolved to an RPT class, so
we know the triples collection (and its column overrides) to scan.

Emitted shape (the ADR's Option A idiom)::

    LET <opt> = (
      FOR <t> IN @@<triples>
      FILTER <t>.<subject_col> == <o_expr>
      [FILTER <t>.<predicate_col> == @<pred>]   -- fixed-predicate only
      RETURN { f0: <t>.<predicate_col>, f1: NOT_NULL(<obj_uri>, <obj_val>) }
    )
    FOR <row> IN (LENGTH(<opt>) > 0 ? <opt> : [null])
      -- ?p2 = <row>.f0, ?o2 = <row>.f1  (both null when no triple matched)

The ``LENGTH(...) > 0 ? ... : [null]`` pad is what makes this a LEFT
join rather than an INNER join: with zero matches the FOR still yields
exactly one row whose fields are ``null`` (SPARQL "unbound"), preserving
the outer solution. Multiple matches fan out to multiple rows, which is
the correct multiset OPTIONAL semantics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rdflib import URIRef, Variable

from ..errors import UnsupportedSparqlError
from .builder import attribute_ref

if TYPE_CHECKING:
    from .visitor import AlgebraVisitor


def is_rpt_cross_subject_optional(visitor: AlgebraVisitor, p2: Any, node: Any) -> bool:
    """``True`` iff *node* is an RPT-backed cross-subject OPTIONAL this
    module can emit.

    Gates (all required — anything else falls through to
    ``visit_LeftJoin``'s existing same-subject handling or its
    structured rejection):

    * RPT mode — ``state.var_to_rpt_class`` is non-empty, so a triples
      collection + column overrides are known.
    * The OPTIONAL body is a single BGP triple (multi-triple / filtered
      cross-subject OPTIONAL is deferred — it needs an inner join inside
      the subquery).
    * No inner ``FILTER`` on the LeftJoin.
    * The triple's subject is a variable bound as a *value*
      (in ``var_to_expr``) but never opened as a document
      (not in ``var_to_doc_alias``) — the defining trait of
      cross-subject.
    """
    if not visitor.state.var_to_rpt_class:
        return False
    triples = getattr(p2, "triples", []) or []
    if len(triples) != 1:
        return False
    expr = getattr(node, "expr", None)
    if expr is not None and getattr(expr, "name", "") != "TrueFilter":
        return False
    subject = triples[0][0]
    if not isinstance(subject, Variable):
        return False
    name = str(subject)
    return name in visitor.state.var_to_expr and name not in visitor.state.var_to_doc_alias


def emit_rpt_cross_subject_optional(visitor: AlgebraVisitor, triple: tuple[Any, Any, Any], node: Any) -> None:
    """Emit the RPT left-join scan for a cross-subject OPTIONAL triple.

    See the module docstring for the emitted AQL shape. ``node`` is
    accepted for signature symmetry with the other OPTIONAL emitters and
    to assert the no-inner-FILTER precondition the detector guarantees.
    """
    subject, predicate, obj = triple
    if not isinstance(obj, Variable):
        # ``OPTIONAL { ?o :p <const> }`` is an existence test, not a
        # binding; AQL can express it but the semantics differ enough to
        # warrant its own slice. Refuse rather than guess.
        raise UnsupportedSparqlError("cross-subject OPTIONAL with a non-variable object is not yet supported")

    # Any RPT class gives us the triples collection + column overrides;
    # a pure-RPT dataset keeps every triple in one collection, so the
    # subject's class (or any in scope) names the right table.
    rpt_class = next(iter(visitor.state.var_to_rpt_class.values()))
    subject_value_expr = visitor.state.var_to_expr[str(subject)]

    child = visitor.builder.create_child()
    # ``doc``-prefixed alias keeps the triples scan consistent with every
    # other collection FOR the visitor emits (the RPT property-triple
    # path opens ``doc<N>`` too), so EXPLAIN output and the cross-
    # validation interpreter both read it uniformly.
    triples_alias = child.fresh_alias(prefix="doc")
    child.for_(triples_alias, rpt_class.collection)
    # Join the OPTIONAL subject to the outer-bound value. The outer
    # alias referenced by ``subject_value_expr`` is in lexical scope
    # inside this subquery (same as the MINUS/EXISTS probe).
    child.filter_raw(f"{attribute_ref(triples_alias, rpt_class.subject_column)} == {subject_value_expr}")

    # One projection field per *new* variable (predicate if it's a
    # variable, plus the object). Fields are positional (``f0``, ``f1``)
    # so the downstream binding doesn't depend on SPARQL var names.
    new_bindings: list[tuple[str, str]] = []  # (sparql_var, source_expr)
    if isinstance(predicate, Variable):
        new_bindings.append((str(predicate), attribute_ref(triples_alias, rpt_class.predicate_column)))
    elif isinstance(predicate, URIRef):
        pred_bind = child.bind(str(predicate), hint="pred")
        child.filter_eq(attribute_ref(triples_alias, rpt_class.predicate_column), pred_bind)
    else:
        raise UnsupportedSparqlError("cross-subject OPTIONAL predicate must be a variable or an IRI")
    object_expr = (
        f"NOT_NULL({attribute_ref(triples_alias, rpt_class.object_uri_column)}, "
        f"{attribute_ref(triples_alias, rpt_class.object_value_column)})"
    )
    new_bindings.append((str(obj), object_expr))

    field_names = [f"f{i}" for i in range(len(new_bindings))]
    projection = ", ".join(
        f"{field}: {source}" for field, (_, source) in zip(field_names, new_bindings, strict=False)
    )
    child.return_scalar("{" + projection + "}")

    inner_aql = visitor.builder.absorb_child(child)
    indented = "\n".join("  " + line for line in inner_aql.splitlines())
    opt_alias = visitor.builder.fresh_alias(prefix="optsub")
    visitor.builder.let(opt_alias, f"(\n{indented}\n)")

    row_alias = visitor.builder.fresh_alias(prefix="optrow")
    # ``[null]`` pad ⇒ LEFT join: zero matches still yield one row whose
    # field reads are ``null`` (SPARQL unbound), so the outer solution
    # survives. Multiple matches fan out — correct multiset OPTIONAL.
    visitor.builder.for_inline(row_alias, f"(LENGTH({opt_alias}) > 0 ? {opt_alias} : [null])")
    for field, (var_name, _source) in zip(field_names, new_bindings, strict=False):
        visitor.state.var_to_expr[var_name] = f"{row_alias}.{field}"
        visitor.state.maybe_unbound.add(var_name)
