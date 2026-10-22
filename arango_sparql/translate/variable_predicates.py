"""SPARQL 1.1 variable-predicate emission (``?s ?p ?o``).

The visitor's main triple dispatcher (:meth:`AlgebraVisitor._emit_triple`)
hands every triple whose predicate is a :class:`rdflib.term.Variable`
to :func:`emit_variable_predicate_triple` below. Two emission shapes
are selected here from the subject's binding state at the time the
triple visits — see the function docstring for the spec-correctness
disclosure on each.

This module lives next to :mod:`arango_sparql.translate.paths` for
exactly the same reason that one does: keeping ``visitor.py`` under
the 1500-line cap from ``.cursor/rules/modularity-and-structure.mdc``.
Every helper here takes the visitor instance as its first argument
and reaches into its public-ish surface
(``builder``, ``state``, ``_open_collection``, ``_ensure_subject_alias``,
``_record_var_expr``) — the same convention :mod:`paths` uses.

Legacy reference: ``references/arango-sparql/src/lib/pgt-translator.js``
lines 244-261, which hard-coded a four-collection UNION
(``Person | Organization | Property | Class``) rather than driving
the fan-out off the resolver. We replace that with RPT and
document-edge shapes. The latter merges ATTRIBUTES rows with every
ontology-declared edge collection so object properties are not lost.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rdflib import Literal, URIRef, Variable

from ..errors import AqlEmitError, UnsupportedSparqlError
from .builder import attribute_ref, subject_ref

if TYPE_CHECKING:
    from .resolver import ResolvedProperty
    from .visitor import AlgebraVisitor

# System-attribute names we strip out of an ATTRIBUTES() fan-out
# because they are not RDF predicates — ArangoDB metadata
# (``_id``, ``_key``, ``_rev``, ``_from``, ``_to``) plus our own
# synthetic ``_uri`` column (which is the subject IRI, never a
# predicate). ATTRIBUTES(doc, true) already drops the first five;
# ``_uri`` is ours, so we filter it explicitly. Stored as a frozen
# tuple so two visitors share the same bind value via the
# builder's value-keyed dedup.
#
# The resolver's :attr:`SchemaResolver.graph_field` (default
# ``"_graph"``) is appended at call time in
# :func:`_emit_attributes_fan_out` so that wildcard-predicate
# queries (``?s ?p ?o``) don't leak the named-graph metadata
# attribute as a triple predicate. The constant stays minimal —
# the dynamic part is the per-resolver graph field name, which
# may be overridden per deployment.
SYSTEM_ATTRIBUTES_TO_SKIP: tuple[str, ...] = ("_uri",)


def emit_variable_predicate_triple(
    visitor: AlgebraVisitor,
    subject: Any,
    predicate: Variable,
    obj: Any,
    triple: tuple[Any, Any, Any],
) -> None:
    """Emit AQL for ``?s ?p ?o`` (variable-predicate triple pattern).

    Two emission shapes, selected by the subject's binding state.

    **RPT-bound subject** — the triples table already has
    ``predicate`` / ``object_uri`` / ``object_value`` columns,
    so the fan-out collapses to a plain FOR + per-column
    projection. ``?p`` binds to the predicate column directly
    (no FILTER) and ``?o`` to the standard
    ``NOT_NULL(object_uri, object_value)`` expression. Mirrors
    :meth:`AlgebraVisitor._emit_rpt_property_triple` minus the
    predicate equality FILTER. This emission is W3C-spec-correct.

    **PG / LPG / default-collection subject** — an ``ATTRIBUTES()``
    fan-out over the subject document, unioned with edge traversals when
    the ontology declares edge-backed object properties::

        FOR k1 IN ATTRIBUTES(<subject_alias>, true)
        FILTER k1 NOT IN [<sys attrs>]
        LET p1 = @attr_uris[k1]      -- when the ontology declares
        FILTER p1 != null            -- datatype properties
        -- ?p bound to p1 (predicate IRI)
        -- ?o bound to <subject_alias>[k1]

    which iterates every attribute on the subject document and
    produces one binding row per non-system attribute. Each object
    property contributes a correlated OUTBOUND arm that returns the
    property's IRI and target vertex IRI. When the
    resolver's :meth:`SchemaResolver.attribute_uri_map` is non-empty
    (the ontology declares ``owl:DatatypeProperty`` terms), ``?p``
    binds to the **predicate IRI** via the bound reverse map, which
    is the W3C-spec-correct shape; attributes with no declared
    property are filtered out. CARVE-OUT (empty-ontology fallback
    only): with no declared datatype properties there is nothing to
    map through, so ``?p`` binds to the attribute **name** (a string
    like ``"name"``) — live-execution cross-validation against a
    W3C-conformant triplestore diverges for queries that rely on
    the IRI shape (PRD §6.6 Variable-predicates row).

    Object binding still goes through the standard
    ``var_to_expr`` machinery, so a ``?o`` that already appears
    in another triple gets the equality-FILTER join automatically
    (mirroring the visitor's right-side Variable branch).
    """

    if not isinstance(predicate, Variable):  # pragma: no cover - defensive
        raise UnsupportedSparqlError("variable-predicate emitter called with non-Variable predicate")

    # ---- RPT branch ---------------------------------------------------
    if isinstance(subject, Variable) and str(subject) in visitor.state.var_to_rpt_class:
        _emit_rpt_branch(visitor, subject, predicate, obj, triple)
        return

    # ---- PG / LPG / default-collection branch -------------------------
    _emit_attributes_branch(visitor, subject, predicate, obj, triple)


def _emit_rpt_branch(
    visitor: AlgebraVisitor,
    subject: Variable,
    predicate: Variable,
    obj: Any,
    triple: tuple[Any, Any, Any],
) -> None:
    """RPT subject + variable predicate.

    Opens a fresh FOR over the triples table, joins on the subject
    URI captured in ``var_to_expr``, and projects the predicate
    column directly. Object follows the standard RPT object
    dispatch (Variable → NOT_NULL, URI → OR-filter across both
    columns, Literal → object_value).

    This branch is the W3C-spec-correct half of the slice — the
    triples table genuinely has a predicate column, so ``?p``
    binds to an IRI as the spec requires. Live-execution cross-
    validation against pyoxigraph would pass for these (the v0.3
    coverage report doesn't include any RPT-flavoured variable-
    predicate W3C tests, but the goldens prove the shape).
    """
    rpt_class = visitor.state.var_to_rpt_class[str(subject)]
    triples_alias = visitor._open_collection(rpt_class.collection, resolved=rpt_class)
    subj_expr = visitor.state.var_to_expr.get(str(subject))
    if subj_expr is None:  # pragma: no cover - defensive
        raise AqlEmitError(f"RPT variable-predicate triple references unbound subject ?{subject}")
    new_subj_expr = attribute_ref(triples_alias, rpt_class.subject_column)
    if subj_expr != new_subj_expr:
        visitor.builder.filter_raw(f"{new_subj_expr} == {subj_expr}")
    # ``?p`` binds to the predicate column — no FILTER; we want
    # EVERY predicate on this subject.
    visitor._record_var_expr(predicate, attribute_ref(triples_alias, rpt_class.predicate_column))
    # ``?o`` follows the same NOT_NULL shape as
    # _emit_rpt_property_triple's Variable branch.
    coalesce_expr = (
        f"NOT_NULL({attribute_ref(triples_alias, rpt_class.object_uri_column)}, "
        f"{attribute_ref(triples_alias, rpt_class.object_value_column)})"
    )
    if isinstance(obj, Variable):
        o_name = str(obj)
        existing = visitor.state.var_to_expr.get(o_name)
        if existing is None:
            visitor.state.var_to_expr[o_name] = coalesce_expr
        elif existing != coalesce_expr:
            visitor.builder.filter_raw(f"{coalesce_expr} == {existing}")
        return
    if isinstance(obj, URIRef):
        uri_bind = visitor.builder.bind(str(obj), hint="obj")
        visitor.builder.filter_raw(
            f"({attribute_ref(triples_alias, rpt_class.object_uri_column)} == {uri_bind} "
            f"|| {attribute_ref(triples_alias, rpt_class.object_value_column)} == {uri_bind})"
        )
        return
    if isinstance(obj, Literal):
        # Local import to avoid circular dependency at module
        # import time — :mod:`visitor` already imports this module.
        from .visitor import _term_to_python

        val_bind = visitor.builder.bind(_term_to_python(obj), hint="obj")
        visitor.builder.filter_eq(attribute_ref(triples_alias, rpt_class.object_value_column), val_bind)
        return
    raise UnsupportedSparqlError(
        f"variable-predicate RPT triple object term type "
        f"{type(obj).__name__!r} is not supported (triple {triple!r})"
    )


def _emit_attributes_branch(
    visitor: AlgebraVisitor,
    subject: Any,
    predicate: Variable,
    obj: Any,
    triple: tuple[Any, Any, Any],
) -> None:
    """PG / LPG / default-collection subject + variable predicate.

    Opens (or reuses) a FOR for the subject, then iterates the
    document's attributes via ``ATTRIBUTES(doc, true)`` so each
    non-system attribute produces one binding row. ``?p`` binds
    to the attribute name (string — see PRD §6.6 carve-out);
    ``?o`` binds to the dictionary lookup ``doc[k]``.

    Composes cleanly with the rest of the BGP because both
    ``?p`` and ``?o`` are recorded in ``var_to_expr`` — a
    downstream triple that re-binds either variable gets the
    standard equality-FILTER join.
    """
    subject_alias = visitor._ensure_subject_alias(subject)
    edge_properties = visitor.resolver.edge_properties()
    if edge_properties:
        _emit_document_edge_union(
            visitor,
            subject_alias,
            predicate,
            obj,
            triple,
            edge_properties,
        )
        return

    # Mint a fresh alias for the ATTRIBUTES() loop variable.
    # ``k`` prefix is descriptive — operators reading EXPLAIN
    # output can immediately tell which FOR is an attribute
    # iteration vs. a document scan.
    key_alias = visitor.builder.fresh_alias(prefix="k")
    visitor.builder.for_attributes(key_alias, subject_alias)
    # Skip our synthetic ``_uri`` column AND the named-graph
    # metadata attribute (``resolver.graph_field``, default
    # ``"_graph"``) — ATTRIBUTES(_, true) already drops the
    # ArangoDB system attrs. Without the ``graph_field`` entry a
    # wildcard ``?s ?p ?o`` would surface the named-graph IRI as
    # if it were a triple predicate, which is the kind of silent
    # semantic leak ADR-0001 specifically calls out. The skip
    # list is bound through the builder so the AQL never sees
    # inlined string literals.
    skip_list = sorted({*SYSTEM_ATTRIBUTES_TO_SKIP, visitor.resolver.graph_field})
    sys_bind = visitor.builder.bind(skip_list, hint="sys_attrs")
    visitor.builder.filter_raw(f"{key_alias} NOT IN {sys_bind}")
    attr_uri_map = visitor.resolver.attribute_uri_map()
    if attr_uri_map:
        # Ontology-declared datatype properties give us the reverse
        # attribute→IRI index, so ``?p`` binds to the predicate IRI
        # the spec requires. Attributes with no declared property are
        # filtered out — SPARQL's open-world answer for a value the
        # dataset cannot express as a triple with an IRI predicate.
        map_bind = visitor.builder.bind(attr_uri_map, hint="attr_uris")
        pred_alias = visitor.builder.fresh_alias(prefix="p")
        visitor.builder.let(pred_alias, f"{map_bind}[{key_alias}]")
        visitor.builder.filter_raw(f"{pred_alias} != null")
        visitor._record_var_expr(predicate, pred_alias)
    else:
        # ``?p`` binds to the iteration key. CARVE-OUT: this is the
        # attribute NAME (a string), not the predicate IRI — reached
        # only when the ontology declares no datatype properties at
        # all. See the module docstring and PRD §6.6 row.
        visitor._record_var_expr(predicate, key_alias)
    # ``?o`` binds to the attribute value at this key. Same
    # var_to_expr-aware logic the visitor's Case 2 object
    # branch uses, so a ``?o`` that's also bound by another
    # triple gets the equality-FILTER join. Under
    # ``fan_out_list_values`` a list-valued attribute is N triples:
    # unbound ``?o`` fans out per element, and comparisons become
    # membership tests (mirrors Case 2 exactly).
    value_expr = f"{subject_alias}[{key_alias}]"
    if isinstance(obj, Variable):
        o_name = str(obj)
        existing = visitor.state.var_to_expr.get(o_name)
        if existing is None:
            if visitor.resolver.fan_out_list_values:
                value_alias = visitor.builder.fresh_alias(prefix="lv")
                visitor.builder.for_inline(value_alias, visitor._fan_out_source(value_expr))
                visitor._record_var_expr(obj, value_alias)
            else:
                visitor._record_var_expr(obj, value_expr)
        elif existing != value_expr:
            visitor.builder.filter_raw(visitor._value_match_expr(value_expr, existing))
        return
    if isinstance(obj, (Literal, URIRef)):
        from .visitor import _term_to_python

        obj_bind = visitor.builder.bind(_term_to_python(obj), hint="obj")
        if visitor.resolver.fan_out_list_values:
            visitor.builder.filter_raw(visitor._value_match_expr(value_expr, obj_bind))
        else:
            visitor.builder.filter_eq(value_expr, obj_bind)
        return
    raise UnsupportedSparqlError(
        f"variable-predicate triple object term type "
        f"{type(obj).__name__!r} is not supported (triple {triple!r})"
    )


def _emit_document_edge_union(
    visitor: AlgebraVisitor,
    subject_alias: str,
    predicate: Variable,
    obj: Any,
    triple: tuple[Any, Any, Any],
    edge_properties: tuple[ResolvedProperty, ...],
) -> None:
    """Merge datatype-attribute and object-edge triples for one subject."""
    arm_aqls = [_build_attribute_arm(visitor, subject_alias)]
    for prop in edge_properties:
        edge_collection = prop.edge_collection
        if edge_collection is None:  # pragma: no cover - resolver filters these
            raise AqlEmitError(f"object property {prop.iri!r} has no executable edge collection")
        child = visitor.builder.create_child()
        vertex_alias = child.fresh_alias(prefix="v")
        edge_alias = child.fresh_alias(prefix="e")
        child.for_traversal(
            vertex_alias,
            edge_alias,
            subject_alias,
            edge_collection,
        )
        if prop.mapping_style == "GENERIC_WITH_TYPE":
            if not prop.type_field or prop.type_value is None:
                raise AqlEmitError(
                    f"object property {prop.iri!r} uses GENERIC_WITH_TYPE "
                    "without both typeField and typeValue"
                )
            type_bind = child.bind(prop.type_value, hint=prop.type_field)
            child.filter_eq(attribute_ref(edge_alias, prop.type_field), type_bind)
        predicate_bind = child.bind(prop.iri, hint="predicate")
        child.return_object(
            [
                ("predicate", predicate_bind),
                ("object", subject_ref(vertex_alias)),
            ]
        )
        arm_aqls.append(visitor.builder.absorb_child(child))

    triple_alias = visitor.builder.fresh_alias(prefix="triple")
    visitor.builder.for_union(triple_alias, arm_aqls)
    _bind_union_term(visitor, predicate, f"{triple_alias}.predicate")
    _bind_union_object(visitor, obj, f"{triple_alias}.object", triple)


def _build_attribute_arm(visitor: AlgebraVisitor, subject_alias: str) -> str:
    """Build the correlated document-attribute arm of a triple UNION."""
    child = visitor.builder.create_child()
    key_alias = child.fresh_alias(prefix="k")
    child.for_attributes(key_alias, subject_alias)
    skip_list = sorted({*SYSTEM_ATTRIBUTES_TO_SKIP, visitor.resolver.graph_field})
    sys_bind = child.bind(skip_list, hint="sys_attrs")
    child.filter_raw(f"{key_alias} NOT IN {sys_bind}")

    attr_uri_map = visitor.resolver.attribute_uri_map()
    if attr_uri_map:
        map_bind = child.bind(attr_uri_map, hint="attr_uris")
        predicate_expr = child.fresh_alias(prefix="p")
        child.let(predicate_expr, f"{map_bind}[{key_alias}]")
        child.filter_raw(f"{predicate_expr} != null")
    else:
        predicate_expr = key_alias

    value_expr = f"{subject_alias}[{key_alias}]"
    if visitor.resolver.fan_out_list_values:
        value_alias = child.fresh_alias(prefix="lv")
        child.for_inline(value_alias, visitor._fan_out_source(value_expr))
        value_expr = value_alias
    child.return_object(
        [
            ("predicate", predicate_expr),
            ("object", value_expr),
        ]
    )
    return visitor.builder.absorb_child(child)


def _bind_union_term(
    visitor: AlgebraVisitor,
    variable: Variable,
    expression: str,
) -> None:
    existing = visitor.state.var_to_expr.get(str(variable))
    if existing is None:
        visitor._record_var_expr(variable, expression)
    elif existing != expression:
        visitor.builder.filter_raw(f"{expression} == {existing}")


def _bind_union_object(
    visitor: AlgebraVisitor,
    obj: Any,
    expression: str,
    triple: tuple[Any, Any, Any],
) -> None:
    if isinstance(obj, Variable):
        _bind_union_term(visitor, obj, expression)
        return
    if isinstance(obj, (Literal, URIRef)):
        from .visitor import _term_to_python

        obj_bind = visitor.builder.bind(_term_to_python(obj), hint="obj")
        visitor.builder.filter_eq(expression, obj_bind)
        return
    raise UnsupportedSparqlError(
        f"variable-predicate triple object term type "
        f"{type(obj).__name__!r} is not supported (triple {triple!r})"
    )
