"""One IRI stored in several member collections is one subject (#124, #147–#149).

A PG entity of two classes is a document in each class's collection, with
the same ``_uri``. A union of a class's member collections (a supertype, a
domain spanning collections) returned it once per document, so a query gave
a solution per copy, and a read of one copy missed a value stored on another.

When no triple on the subject depends on which copy the row holds
(:func:`copies_interchangeable`), the union drops a later copy of an IRI an
earlier member holds in the same graph, once the BGP that opened it has
filtered the rows (:func:`drop_repeated_copies`). Its attribute reads
(:func:`emit_attribute_across_copies`, :class:`CopyRow`):

* in that BGP, the first attribute read whose declared domain names only
  members (or none) is read from the row's own document, as before, and
  compared by the probe: every copy that can hold it is a row of its own, so
  a copy with another value, or none, is another solution and stays (#124).
  One triple only: a second read from the row's own document (another
  attribute, or the same one again) would have to be on the same copy as
  the first;
* any other read — an attribute whose domain names a collection outside the
  members, or any read after the probe (an OPTIONAL body, a later group), a
  compared attribute's too — is read from every copy in the members and the
  domain: its distinct values, so a value on two copies is one triple, as in
  RDF, and the rows of one IRI read the same (#147). An indexed ``_uri``
  lookup per collection per row, guarded so a row with no ``_uri`` starts
  none; so only where the row's own document is not enough: LDBC's messages
  read ``content`` from their own document.

An edge walked back into such a row starts from every copy in the range and
the members (``AlgebraVisitor._every_copy``).

Known limit: under ``fan_out_list_values`` a list is one triple per element,
and comparing whole values keeps two copies whose lists differ but share an
element, which then comes back once per copy. Reading every copy's elements
instead costs those lookups on every row (LDBC's PG layout fans out): #170.

Otherwise every copy is kept, as before.

Module functions taking the visitor, as :mod:`.paths` and
:mod:`.optional_subquery` do, so ``visitor.py`` does not grow
(``.cursor/rules/modularity-and-structure.mdc``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rdflib import RDF, Literal, URIRef, Variable
from rdflib.paths import InvPath

from ..errors import UnsupportedSparqlError
from .builder import (
    AqlQueryBuilder,
    attribute_path_ref,
    attribute_ref,
    is_aql_identifier,
)
from .resolver import ResolvedClass, ResolvedProperty

if TYPE_CHECKING:
    from .visitor import AlgebraVisitor


@dataclass
class CopyRow:
    """A union row that stands for every copy of its IRI (``copy_members``)."""

    members: dict[str, list[ResolvedClass]]
    #: The attribute path read from the row's own document before the probe,
    #: which compares it (one at most), and the triple that read it.
    compared: list[tuple[str, ...]] = field(default_factory=list)
    compared_by: Any = None
    #: Whether the probe is emitted: a read from the own document now would
    #: not be compared, so every later read is from every copy.
    probed: bool = False


def copies_interchangeable(visitor: AlgebraVisitor, subject: Any) -> bool:
    """Whether a union of *subject*'s class's collections may drop a
    repeated copy: no triple on it depends on which copy the row holds.

    Every triple pattern of the query (``query_triples``: every group,
    OPTIONAL, UNION arm and sub-select) that names the subject must be a type
    pattern, an attribute read (compared, or read from every copy), or an
    edge into it whose property declares its range: the walk back starts
    from every copy in the range (``AlgebraVisitor._every_copy``), and a walk
    forward joins on the IRI. A walk out of the
    subject, a path, a variable predicate, an edge into it with no usable
    range, a symmetric edge (walked out of it too), a DESCRIBE of it, or an
    unbound ``GRAPH ?g`` (bound per copy) depends on which copy the row
    holds: then every copy is kept.
    """
    if visitor.query_triples is None:
        return False
    if any(str(r) == str(subject) for r in visitor.describe_resources or ()):
        # DESCRIBE reads every attribute of every copy.
        return False
    graph = visitor.state.graph_scope[-1] if visitor.state.graph_scope else None
    if isinstance(graph, Variable) and str(graph) not in visitor.state.var_to_expr:
        return False
    for s, p, o in visitor.query_triples:
        if isinstance(p, InvPath) and isinstance(p.arg, URIRef):
            # ``?s ^:p ?x`` is the edge ``?x :p ?s``.
            s, p, o = o, p.arg, s
        if s == subject:
            if not isinstance(p, URIRef):
                return False
            if p == RDF.type:
                continue
            prop = visitor.resolver.resolve_property(p)
            if prop.is_object_property or prop.edge_target_of or prop.edge_collection:
                return False
            continue
        if o != subject or p == RDF.type:
            continue
        if not isinstance(p, URIRef):
            return False
        prop = visitor.resolver.resolve_property(p)
        if prop.edge_target_of or prop.symmetric:
            return False
        if prop.is_object_property and not visitor._usable_range(prop):
            return False
    return True


def read_from_own_copy(
    visitor: AlgebraVisitor, prop: ResolvedProperty, members: dict[str, list[ResolvedClass]]
) -> bool:
    """Whether every collection of *prop*'s domain is a member (or it declares
    none), so every copy that can hold the attribute is a union row of its
    own, read from its own document: comparing them keeps each value
    (review of #171: a domain collection outside the members, ``Employee``,
    held a value no row read)."""
    domain = visitor._range_classes(visitor.resolver.domain_classes(prop.iri))
    return not domain or set(domain) <= set(members)


def drop_repeated_copies(visitor: AlgebraVisitor) -> None:
    """Drop a union row whose IRI an earlier member already holds (#124).

    For a row of member collection *k* (in name order), each earlier member
    *j* is probed for a document with the same IRI, of its classes in the
    active graph scope and tenant (so the copy kept is one the union keeps),
    in the same graph (a copy in another named graph is another quad, as
    pyoxigraph's union default graph reads it), with the same value or the
    same absence for every attribute read from the row's own document
    (:attr:`CopyRow.compared`).
    Emitted once the BGP's triples have filtered the rows: every other read
    is the same from any copy, so the rows of one IRI that agree on the
    compared attributes pass or fail together. The row's collection and
    ``_uri`` guard the probe in a ternary, so a row no probe concerns starts
    no subquery.
    """
    checks, visitor.state.pending_copy_checks = visitor.state.pending_copy_checks, []
    builder = visitor.builder
    for alias, by_collection, what in checks:
        row = visitor.state.copy_members[alias]
        row.probed = True
        fields = row.compared
        order = sorted(by_collection)
        for j, earlier in enumerate(order[:-1]):
            indexes = visitor.resolver.uri_indexes
            if indexes is not None and indexes.scanned(earlier, order[j + 1 :]):
                visitor.resolver.warn_unindexed_uri(earlier)
            probe = builder.create_child()
            other = probe.fresh_alias()
            probe.for_(other, earlier)
            later = " || ".join(
                f"IS_SAME_COLLECTION({probe.bind(c, hint='coll')}, {alias})" for c in order[j + 1 :]
            )
            # Only an IRI names a copy in another collection (an ``_id``
            # includes the collection), so a row with no ``_uri`` is never
            # probed. The test guards the subquery itself, not a FILTER
            # inside it: ArangoDB skips a subquery in the branch a ternary
            # does not take, so such a row starts no lookup.
            guard = f"({later}) && {alias}._uri != null"
            # Implied by the guard, but the optimizer cannot see it inside
            # the subquery: without it a sparse ``_uri`` index is not used.
            probe.filter_raw(f"{other}._uri != null")
            probe.filter_raw(f"{other}._uri == {alias}._uri")
            graph = visitor.resolver.graph_field
            probe.filter_raw(f"{attribute_ref(other, graph)} == {attribute_ref(alias, graph)}")
            for test in visitor._class_tests(probe, other, by_collection[earlier], what):
                probe.filter_raw(test)
            scope = visitor._graph_scope_test(other)
            if scope is not None:
                probe.filter_raw(scope)
            for tenant in _distinct_tenants(by_collection[earlier]):
                visitor._enforce_tenant_scope(other, tenant, builder=probe)
            for path in fields:
                probe.filter_raw(
                    f"{probe.attribute_exists(other, path)} == {probe.attribute_exists(alias, path)}"
                    f" && {attribute_path_ref(other, path)} == {attribute_path_ref(alias, path)}"
                )
            probe.limit(1)
            probe.return_scalar("1")
            text = builder.absorb_child(probe)
            repeated = builder.fresh_alias(prefix="repeated")
            builder.let(repeated, f"({guard}) ? LENGTH((\n{_indent(text)}\n)) : 0")
            builder.filter_raw(f"{repeated} == 0")


def emit_attribute_across_copies(
    visitor: AlgebraVisitor, alias: str, prop: ResolvedProperty, obj: Any, triple: Any
) -> bool:
    """Emit the attribute triple ``(alias, prop, obj)`` from every copy of the
    subject's IRI, when *alias* is a union row that stands for all of them
    (``copy_members``); ``False`` (nothing emitted) when the row's own
    document is read: before the probe, *prop* is compared, or it is the
    first attribute read and every copy that can hold it is a union row
    (then it is compared from now on). After
    the probe every read is from every copy, a compared attribute's too: the
    rows kept differ on it, and each pairs with every copy's value (review
    of #171).

    The copies are the documents with the row's IRI, in the same graph, in
    the union's members and the collections of *prop*'s ``rdfs:domain``
    (where the attribute is declared; a member outside it can still hold a
    value, which a read of the bound document finds, review of #171), each
    read as a document of its class would be: its tenant FILTER and the
    active graph scope. The triple's values are their distinct values of the
    attribute, so a value two copies hold is one triple (``UNIQUE``). Only an
    IRI names a copy in another collection, so a row with no ``_uri`` reads
    its own document, and the lookup is guarded as the probe is: ArangoDB
    starts no subquery in the branch a ternary does not take, and the
    lookup's ``_uri != null`` lets a sparse index serve it (review of #171:
    an unguarded ``_uri``-or-``_id`` match scanned every collection per row)::

        LET vals = (row._uri != null) ? UNIQUE((
          FOR c IN UNION((FOR c1 IN Org FILTER c1._uri != null
                          FILTER c1._uri == row._uri … RETURN c1), …)
          FILTER HAS(c, "name")
          RETURN c.name
        )) : (HAS(row, "name") ? [row.name] : [])
        FOR n IN vals                          -- ?n unbound
        FILTER <bound value> IN vals           -- ?n bound, or a constant
    """
    row = visitor.state.copy_members.get(alias)
    if row is None:
        return False
    members = row.members
    if not row.probed:
        if triple == row.compared_by:
            return False
        if not row.compared and read_from_own_copy(visitor, prop, members):
            # One triple only: a row reads it from its own document, so a
            # second one read that way would have to be on the same copy
            # (review of #171: ``name "O"`` on Org/s and ``nick`` on Person/s
            # matched no row; ``:name ?a ; :name ?b`` paired only one copy's
            # names). The others are read from every copy.
            row.compared.append(prop.field_path)
            row.compared_by = triple
            return False
    builder = visitor.builder
    sources: dict[str, list[ResolvedClass]] = {c: list(cs) for c, cs in members.items()}
    for collection, classes in visitor._range_classes(visitor.resolver.domain_classes(prop.iri)).items():
        sources.setdefault(collection, classes)
    sub = builder.create_child()

    def scan(into: AqlQueryBuilder, collection: str) -> str:
        """Open *collection*'s copies of the row's IRI in *into*."""
        inner = into.fresh_alias()
        into.for_(inner, collection)
        # Implied by the guard, but the optimizer cannot see it inside the
        # subquery: without it a sparse ``_uri`` index is not used.
        into.filter_raw(f"{inner}._uri != null")
        into.filter_raw(f"{inner}._uri == {alias}._uri")
        graph = visitor.resolver.graph_field
        into.filter_raw(f"{attribute_ref(inner, graph)} == {attribute_ref(alias, graph)}")
        scope = visitor._graph_scope_test(inner)
        if scope is not None:
            into.filter_raw(scope)
        for tenant in _distinct_tenants(sources[collection]):
            visitor._enforce_tenant_scope(inner, tenant, builder=into)
        # An indexed ``_uri`` lookup per row, as the probe's (§6.1).
        indexes = visitor.resolver.uri_indexes
        if indexes is not None and indexes.scanned(collection, members):
            visitor.resolver.warn_unindexed_uri(collection)
        return inner

    # A union has two members or more, so there are always two arms.
    arms = []
    for collection in sorted(sources):
        arm = sub.create_child()
        arm.return_scalar(scan(arm, collection))
        arms.append(sub.absorb_child(arm))
    copy = sub.fresh_alias()
    sub.for_union(copy, arms, scans_documents=True)
    sub.filter_raw(sub.attribute_exists(copy, prop.field_path))
    sub.return_scalar(attribute_path_ref(copy, prop.field_path))
    values = builder.fresh_alias(prefix="vals")
    own = (
        f"({builder.attribute_exists(alias, prop.field_path)} ? "
        f"[{attribute_path_ref(alias, prop.field_path)}] : [])"
    )
    builder.let(
        values, f"({alias}._uri != null) ? UNIQUE((\n{_indent(builder.absorb_child(sub))}\n)) : {own}"
    )
    # Under ``fan_out_list_values`` a list value is one triple per element,
    # as a one-document read fans it out (``FLATTEN([attr])``).
    elements = f"UNIQUE(FLATTEN({values}))" if visitor.resolver.fan_out_list_values else values
    if isinstance(obj, Variable):
        existing = visitor.state.var_to_expr.get(str(obj))
        if existing is None:
            value = builder.fresh_alias(prefix="lv")
            builder.for_inline(value, elements)
            visitor._record_var_expr(obj, value)
        else:
            builder.filter_raw(f"{existing} IN {elements}")
        return True
    if isinstance(obj, (Literal, URIRef)):
        from .visitor import _term_to_python

        bind = builder.bind(
            _term_to_python(obj),
            hint=prop.field_path[-1] if is_aql_identifier(prop.field_path[-1]) else None,
        )
        builder.filter_raw(f"{bind} IN {elements}")
        return True
    raise UnsupportedSparqlError(
        f"object term type {type(obj).__name__!r} is not supported in triple {triple!r}"
    )


def _distinct_tenants(classes: list[ResolvedClass]) -> list[ResolvedClass]:
    """One class per tenant scoping among *classes*."""
    return list({(c.tenant_field, c.tenant_entity): c for c in classes}.values())


def _indent(text: str) -> str:
    return "\n".join("  " + line for line in text.splitlines())
