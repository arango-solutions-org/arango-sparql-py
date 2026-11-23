"""Which store holds a subject's triples, when the mapping has more than one (#191).

A mapping can keep some classes as RDF triples (RPT: rows of a triples
table) and others as documents, in one database. A subject a type pattern
or an earlier triple opened is already placed: its document, or its triples
table. One the query never placed — a variable bound only as a value (the
object of an earlier triple, a ``VALUES`` row, an outer row inside a probe)
or a constant IRI — can be in either store, so a triple on it must be read
from the store that holds it, not from one guessed for it.

The emitters guessed. The cross-subject OPTIONAL read the triples table
whenever a triples-table class was in scope, and answered ``OPTIONAL { ?c
:cname ?n }`` with nothing when ``?c`` was a document. The BGP and the
OPTIONAL sub-query read the default ``Document`` collection, which a mixed
database need not have (ERR 1203), and strict subject resolution refused.

:func:`route_subject` classifies instead:

1. **By the predicate's ``rdfs:domain``**, when it declares one. The
   mapping stores a property on its domain's classes: ``:cname`` (domain
   ``:Company``, a document class) is a document attribute, so its
   triples are read from the ``Company`` documents, looked up by ``_uri``
   as the visitor's domain routing reads them (but without opening the
   subject as a ``Company`` document: its next triple is routed on its
   own); a property whose domain is a triples-table class is read from
   that table.
2. **At run time, from every store**, when the mapping cannot say: an
   undeclared predicate, a declared one with no domain, a variable
   predicate, or a domain spanning both stores. The subject's IRI is
   looked up in each triples table (on its subject column) and in each
   document collection (on ``_uri``, the index every reader uses), and
   the triples found are unioned. A resource in one store contributes
   its triples there; one with facts in both contributes both. The union
   is a set (``UNION_DISTINCT``), as an RDF graph is: a triple stored in
   two places is one triple.

The ``rdfs:range`` of the predicate that bound the subject is not used:
it says what class the value is, not where the value's other triples are.
A ``:worksFor`` ranging over documents does not stop the triples table from
holding a triple about the same company, and routing by it would drop that
triple without an error.

The stores are the classes' collections and tables
(:meth:`SchemaResolver.store_sources`) and, on the library's non-strict
resolver, the default ``Document`` collection, where the non-strict
contract keeps an IRI no class places (as ``DESCRIBE`` reads it). A mapping
without that collection then fails loudly (ERR 1203) rather than answer
without it. Under strict subject resolution (the service) it is not a
store. A domain naming a class the ontology does not declare
(``rdfs:domain :Company, :Any``) reads ``Document`` for that class on the
non-strict resolver and is refused under strict resolution, as the
visitor's domain routing does. An edge-backed object property is not
routed here: its triples are its edges.

Each arm is held to the active graph scope on its graph field
(``GRAPH <iri>``; ``GRAPH ?g``: a named graph, ``?g``'s once bound) and
returns that field: inside ``GRAPH ?g`` the row's graph binds ``?g``
(:func:`_bind_graph_variable`, #203), and the set union keeps a triple
stored in two named graphs twice, as the default graph holds two quads
(lax default-graph mode) and the single-store readers count it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rdflib import OWL, RDF, Literal, URIRef, Variable

from ..errors import UnsupportedSparqlError
from .builder import (
    attribute_path_ref,
    attribute_ref,
    subject_matches,
    subject_matches_value,
    subject_ref,
    triple_object_is_iri,
    triple_object_ref,
)
from .resolver import ResolvedClass
from .union_paths import _spawn_child
from .variable_predicates import attribute_triple_loop, document_triple_arms, refuse_nested_attribute_scan

if TYPE_CHECKING:
    from .visitor import AlgebraVisitor


@dataclass(frozen=True)
class StoreRoute:
    """Where a triple on an unplaced subject is read.

    ``tables`` are the triples tables (one RPT class per table and column
    layout); ``documents`` the document collections, each with the classes
    whose test applies there (empty: any document in it).
    """

    tables: tuple[ResolvedClass, ...]
    documents: tuple[tuple[str, tuple[ResolvedClass, ...]], ...]

    @property
    def one_table(self) -> ResolvedClass | None:
        """The table, when the triple can only be in one triples table."""
        return self.tables[0] if len(self.tables) == 1 and not self.documents else None


def route_subject(visitor: AlgebraVisitor, subject: Any, predicate: Any) -> StoreRoute | None:
    """Where ``subject predicate ?o`` is read, or ``None`` when this module
    does not decide it (the visitor's own routing applies).

    ``None`` for: a mapping with no triples table, or one that is a single
    triple store (:meth:`SchemaResolver.triple_store_class`, whose reader
    already takes every unplaced subject); a subject already placed (opened
    as a document or a triples row, a relationship node, a subject re-read
    at its home inside a GRAPH); and a predicate whose triples are edges.

    A route to documents only (``:cname``, domain ``:Company``) is still
    this module's: the visitor's domain routing would open the subject as a
    ``Company`` document, and its next triple (``?c :email ?m``, no domain)
    would then read that document alone, missing a triple the table holds.
    Each triple on an unplaced subject is routed on its own.

    A variable seen for the first time is read from the same stores, every
    row of them (``?x :email ?m`` asks for every subject with an email, in
    whichever store holds it), and bound to its IRI, not opened as a
    document, for the same reason.
    """
    resolver = visitor.resolver
    collections, tables = resolver.store_sources()
    if not tables or resolver.triple_store_class() is not None:
        return None
    state = visitor.state
    if isinstance(subject, Variable):
        name = str(subject)
        if (
            name in state.var_to_doc_alias
            or name in state.var_to_rpt_class
            or name in state.relationship_nodes
            or name in state.outer_subject_homes
        ):
            return None
    elif isinstance(subject, URIRef):
        key = str(subject)
        if key in state.const_subject_alias or key in state.const_subject_rpt_class:
            return None
        if key in state.outer_subject_homes:
            return None
    else:
        return None
    if isinstance(predicate, URIRef) and _is_edge_predicate(visitor, predicate):
        return None

    domain = resolver.domain_classes(str(predicate)) if isinstance(predicate, URIRef) else ()
    if domain:
        classes = visitor._concrete_classes(domain, domain_of=str(predicate))
        route = _route_of(classes)
    else:
        route = StoreRoute(tables=tables, documents=tuple((c, ()) for c in collections))
    return route


def _is_edge_predicate(visitor: AlgebraVisitor, predicate: URIRef) -> bool:
    """Whether *predicate*'s triples are edges (or relationship nodes): the
    edge emitters read those from their edge collections."""
    resolver = visitor.resolver
    if resolver.is_reified_entry(predicate) or resolver.is_edge_exit(predicate):
        return True
    if (predicate, RDF.type, OWL.ObjectProperty) not in resolver.ontology:
        return False
    prop = resolver.resolve_property(predicate)
    return prop.mapping_style != "RPT_EDGE" and prop.edge_collection is not None


def _route_of(classes: list[ResolvedClass]) -> StoreRoute:
    """The stores *classes* (a predicate's domain) keep their triples in."""
    tables: dict[tuple[str, ...], ResolvedClass] = {}
    documents: dict[str, list[ResolvedClass]] = {}
    for c in classes:
        if c.style == "RPT":
            tables.setdefault(c.table_layout, c)
            continue
        for collection in c.shard_family or (c.collection,):
            documents.setdefault(collection, []).append(c)
    return StoreRoute(
        tables=tuple(tables[k] for k in sorted(tables)),
        documents=tuple((coll, tuple(documents[coll])) for coll in sorted(documents)),
    )


def emit_routed_triple(
    visitor: AlgebraVisitor,
    route: StoreRoute,
    subject: Any,
    predicate: Any,
    obj: Any,
    triple: tuple[Any, Any, Any],
) -> None:
    """Read ``subject predicate obj`` from every store in *route*, as one set
    of triples.

    One store's read is emitted in place, as the visitor's own readers read
    it: an index lookup on the subject's IRI (a scan of the domain's
    documents or rows for a subject seen first here), with no sub-query to
    hold. Several stores are one arm each, matched on the
    subject's IRI, unioned as a set; each arm returns ``{s, o}`` (and ``p``
    for a variable predicate). A constant object is tested inside the arm,
    the way its store compares it. The subject keeps its value; one an
    earlier OPTIONAL can leave unbound joins any triple and takes its
    subject (SPARQL 1.1 §18.3, as the triples-table reader does since
    #190); one seen first here binds to it.
    """
    variable_predicate = isinstance(predicate, Variable)
    if variable_predicate and route.documents and route.tables and not visitor.resolver.attribute_uri_map():
        # Without declared datatype properties a document's ``?p`` is its
        # attribute *name* (the PRD §6.6 carve-out), which unioned with the
        # tables' predicate IRIs would mix names and IRIs in one variable.
        raise UnsupportedSparqlError(
            f"variable predicate on {subject.n3()} needs its triples from triples tables and "
            f"documents, but the ontology declares no datatype properties, so a document's "
            f"predicates are attribute names, not IRIs; declare the properties, or give the "
            f"subject a type pattern"
        )
    if isinstance(subject, URIRef):
        subject_expr: str | None = visitor.builder.bind(str(subject), hint="uri")
        open_subject = False
    else:
        subject_expr = visitor.state.var_to_expr.get(str(subject))
        open_subject = subject_expr is not None and str(subject) in visitor.state.maybe_unbound

    if len(route.tables) + len(route.documents) == 1:
        # One store: read in place, as the visitor's own readers do.
        if route.tables:
            fields = dict(
                _table_arm(visitor, route.tables[0], subject_expr, open_subject, predicate, obj, triple)
            )
        else:
            collection, classes = route.documents[0]
            fields = dict(
                _document_arm(
                    visitor, collection, classes, subject_expr, open_subject, predicate, obj, triple
                )
            )
    else:
        arms: list[str] = []
        for table in route.tables:
            arm = _spawn_child(visitor)
            arm.builder.return_object(
                _table_arm(arm, table, subject_expr, open_subject, predicate, obj, triple)
            )
            arms.append(visitor.builder.absorb_child(arm.builder))
        for collection, classes in route.documents:
            arm = _spawn_child(visitor)
            arm.builder.return_object(
                _document_arm(arm, collection, classes, subject_expr, open_subject, predicate, obj, triple)
            )
            arms.append(visitor.builder.absorb_child(arm.builder))
        row = visitor.builder.fresh_alias(prefix="row")
        visitor.builder.for_union(row, arms, distinct=True)
        fields = {key: f"{row}.{key}" for key in ("s", "p", "o", "g")}

    state = visitor.state
    _bind_graph_variable(visitor, fields["g"])
    if isinstance(subject, Variable):
        name = str(subject)
        if subject_expr is None:
            state.var_to_expr[name] = fields["s"]
        elif open_subject:
            state.var_to_expr[name] = f"NOT_NULL({subject_expr}, {fields['s']})"
            state.maybe_unbound.discard(name)
    if variable_predicate:
        _bind_value(visitor, str(predicate), fields["p"])
    if isinstance(obj, Variable):
        _bind_value(visitor, str(obj), fields["o"])


def _bind_graph_variable(visitor: AlgebraVisitor, graph_expr: str) -> None:
    """Inside ``GRAPH ?g``, bind ``?g`` to the graph the triple was read in.

    Each arm already holds its store to the scope
    (:meth:`AlgebraVisitor._graph_scope_test`): a named graph while ``?g`` is
    unbound, ``?g``'s graph once bound, either where it can be unbound. The
    arms did not bind ``?g``, so a read before any other in the group joined
    across graphs and took in the default graph (review of #199; #203). As
    :meth:`AlgebraVisitor._apply_graph_scope` binds it for a document: the
    row's graph when ``?g`` has no value, the row's graph where an outer
    OPTIONAL left it unbound (SPARQL 1.1 §18.3), and a refusal when an
    OPTIONAL of the group itself bound it first (#198).
    """
    scope = visitor.state.graph_scope
    if not scope or not isinstance(scope[-1], Variable):
        return
    state = visitor.state
    name = str(scope[-1])
    existing = state.var_to_expr.get(name)
    if existing is None:
        state.var_to_expr[name] = graph_expr
    elif name in state.maybe_unbound:
        if name in state.graph_vars_unbound_on_entry:
            visitor._refuse_graph_var_without_documents(name)
        state.var_to_expr[name] = f"NOT_NULL({existing}, {graph_expr})"
        state.maybe_unbound.discard(name)


def _bind_value(visitor: AlgebraVisitor, name: str, expr: str) -> None:
    """Bind *name* to *expr*, or join it to the value it already has. One an
    earlier OPTIONAL can leave unbound joins any value and takes this one
    (SPARQL 1.1 §18.3), through the visitor's one compatible-join helper."""
    existing = visitor.state.var_to_expr.get(name)
    if existing is None:
        visitor.state.var_to_expr[name] = expr
        return
    if existing == expr:
        return
    if not visitor._join_maybe_unbound(
        name, existing, f"NOT_NULL({existing}, {expr})", f"{expr} == {existing}"
    ):
        visitor.builder.filter_raw(f"{expr} == {existing}")


def _table_arm(
    arm: AlgebraVisitor,
    table: ResolvedClass,
    subject_expr: str | None,
    open_subject: bool,
    predicate: Any,
    obj: Any,
    triple: tuple[Any, Any, Any],
) -> list[tuple[str, str]]:
    """Read one triples table into *arm*: its rows on the subject (and the
    predicate). Returns the ``{s, [p,] o}`` fields."""
    builder = arm.builder
    arm._refuse_tenant_scoped_triples(table.collection)
    row = builder.fresh_alias(prefix="doc")
    builder.for_(row, table.collection)
    subject_column = attribute_ref(row, table.subject_column)
    if subject_expr is not None and open_subject:
        # A subject an earlier OPTIONAL can leave unbound joins every row
        # (SPARQL 1.1 §18.3): the scan is split so a bound one keeps its
        # subject-column index (#196).
        arm._emit_unbound_join(
            subject_expr,
            lambda alias: f"{attribute_ref(alias, table.subject_column)} == {subject_expr}",
            alias=row,
            builder=builder,
        )
    elif subject_expr is not None:
        builder.filter_raw(f"{subject_column} == {subject_expr}")
    predicate_column = attribute_ref(row, table.predicate_column)
    if isinstance(predicate, URIRef):
        arm._refuse_generated_iri_on_triples(predicate, table.collection, role="predicate")
        builder.filter_eq(predicate_column, builder.bind(str(predicate), hint="pred"))
    scope = arm._graph_scope_test(row)
    if scope is not None:
        builder.filter_raw(scope)
    if isinstance(obj, URIRef):
        # Either column, as the triples-table reader matches an IRI object.
        builder.filter_raw(triple_object_is_iri(row, table, builder.bind(str(obj), hint="obj")))
    elif isinstance(obj, Literal):
        from .visitor import _term_to_python

        builder.filter_eq(
            attribute_ref(row, table.object_value_column), builder.bind(_term_to_python(obj), hint="obj")
        )
    elif not isinstance(obj, Variable):
        raise UnsupportedSparqlError(
            f"object term type {type(obj).__name__!r} is not supported in {triple!r}"
        )
    fields = [("s", subject_column)]
    if isinstance(predicate, Variable):
        fields.append(("p", predicate_column))
    fields.append(("o", triple_object_ref(row, table)))
    fields.append(_graph_field(arm, row))
    return fields


def _document_arm(
    arm: AlgebraVisitor,
    collection: str,
    classes: tuple[ResolvedClass, ...],
    subject_expr: str | None,
    open_subject: bool,
    predicate: Any,
    obj: Any,
    triple: tuple[Any, Any, Any],
) -> list[tuple[str, str]]:
    """Read into *arm* the documents of one collection with the subject's
    IRI (every document of *classes* for a subject seen first here), and
    the triple's values on them. Returns the ``{s, [p,] o}`` fields.

    The document is opened as the domain routing opens one (its class test,
    its tenant scope, the named-graph scope) but not recorded as the
    subject's: the subject's next triple is routed on its own."""
    builder = arm.builder
    doc = builder.fresh_alias(prefix="doc")
    builder.for_(doc, collection)
    if subject_expr is not None and open_subject:
        arm._emit_unbound_join(
            subject_expr, lambda alias: subject_matches(alias, subject_expr), alias=doc, builder=builder
        )
    elif subject_expr is not None:
        builder.filter_raw(subject_matches_value(doc, subject_expr))
    for test in arm._class_tests(builder, doc, list(classes), f"the documents of {collection!r}"):
        builder.filter_raw(test)
    scope = arm._graph_scope_test(doc)
    if scope is not None:
        builder.filter_raw(scope)
    scoped = next((c for c in classes if c.tenant_field), None)
    if scoped is not None:
        arm._enforce_tenant_scope(doc, scoped)
    else:
        tenant = arm._tenant_test(doc, builder=builder)
        if tenant is not None:
            builder.filter_raw(tenant)
    indexes = arm.resolver.uri_indexes
    if subject_expr is not None and indexes is not None and indexes.scanned(collection, [collection]):
        arm.resolver.warn_unindexed_uri(collection)

    if isinstance(predicate, Variable):
        refuse_nested_attribute_scan(arm, "a variable predicate (?s ?p ?o)")
        edge_properties = arm.resolver.edge_properties()
        if edge_properties:
            triple_row = builder.fresh_alias(prefix="row")
            builder.for_union(triple_row, document_triple_arms(arm, doc, edge_properties))
            predicate_value, value = f"{triple_row}.predicate", f"{triple_row}.object"
        else:
            predicate_value, value = attribute_triple_loop(arm, builder, doc)
        _document_object_test(arm, value, obj, fan_out=False, triple=triple)
        return [("s", subject_ref(doc)), ("p", predicate_value), ("o", value), _graph_field(arm, doc)]

    prop = arm.resolver.resolve_property(predicate)
    path = attribute_path_ref(doc, prop.field_path)
    builder.filter_raw(builder.attribute_exists(doc, prop.field_path))
    fan_out = arm.resolver.fans_out(prop)
    value = path
    if fan_out and isinstance(obj, Variable):
        value = builder.fresh_alias(prefix="lv")
        builder.for_inline(value, arm._fan_out_source(path))
    _document_object_test(arm, path, obj, fan_out=fan_out and not isinstance(obj, Variable), triple=triple)
    return [("s", subject_ref(doc)), ("o", value), _graph_field(arm, doc)]


def _graph_field(arm: AlgebraVisitor, alias: str) -> tuple[str, str]:
    """The ``g`` field: the named graph the triple is in, part of the set
    union's key (null in the default graph)."""
    return ("g", attribute_ref(alias, arm.resolver.graph_field))


def _document_object_test(arm: AlgebraVisitor, value: str, obj: Any, *, fan_out: bool, triple: Any) -> None:
    """Test a constant object against a document value, as the document
    reader does (membership for an attribute that fans out)."""
    if isinstance(obj, Variable):
        return
    if not isinstance(obj, (Literal, URIRef)):
        raise UnsupportedSparqlError(
            f"object term type {type(obj).__name__!r} is not supported in {triple!r}"
        )
    from .visitor import _term_to_python

    bind = arm.builder.bind(_term_to_python(obj), hint="obj")
    arm.builder.filter_raw(arm._value_match_expr(value, bind, fan_out=fan_out))
