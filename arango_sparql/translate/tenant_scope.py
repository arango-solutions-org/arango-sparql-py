"""Hold a read of several classes to the tenant, each document by its own class (#218).

PRD §6.5.1: every read of a tenant-scoped class filters its documents on the
class's ``phys:tenantField``. A read can cover several classes at once: an
LPG collection whose labels are different classes, a property whose domain
names several, a supertype, a mixed-store routed read. When those classes
are scoped the same way (one tenant field, one tenant root, or none), one
``FILTER doc.<field> == @tenant`` holds every document, as before.

When they differ, the reads applied the first class's field to every
document (``Person`` documents carry ``org``, ``Doc`` documents ``ws``: every
Person was dropped, and one with a ``ws`` equal to the tenant passed whatever
its ``org``), or every distinct field to every document, which drops them
all. :func:`enforce_classes_tenant_scope` instead tests each document
against the class it belongs to::

    FILTER (!(<doc is a Person>) || doc.org == @tenant)
        && (!(<doc is a Doc>) || doc.ws == @tenant)

A document of a listed class with no tenant field passes; one with labels
of two listed scoped classes is held to both. Membership is the class's
label (its ``phys:typeField``) and, when the classes span collections, its
collection. That holds only where the read itself tests each document's
class. A read that does not (a copy of an IRI looked up by ``_uri``) may
meet a document of a class it does not name, so it also tests the document
against every tenant-scoped class stored in its collection: on that class's
field when the class is under the query's tenant root, and as not visible
when it is under another root or there is no tenant context. Those classes
commit no root. Two classes in one collection with no label to tell them
apart cannot be scoped differently, so that read is refused. Every listed
class's tenant root is committed, so classes under two roots are refused as
a join across them is (:class:`CrossTenantJoinError`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..errors import CrossTenantJoinError, UnsupportedSparqlError
from .builder import AqlQueryBuilder, attribute_ref, is_aql_identifier, type_predicate

if TYPE_CHECKING:
    from .resolver import ResolvedClass
    from .visitor import AlgebraVisitor


def enforce_classes_tenant_scope(
    visitor: AlgebraVisitor,
    alias: str,
    classes: Sequence[ResolvedClass],
    builder: AqlQueryBuilder | None = None,
    *,
    class_tested: bool = True,
) -> None:
    """Emit the tenant FILTER for *alias*, a document of one of *classes*.

    *class_tested* says the read already holds *alias* to *classes* (their
    collection and label test). A read that does not — a copy of an IRI
    looked up by ``_uri``, a vertex a reverse walk reached — can meet a
    document of another class stored in the same collection, so when the
    tenant fields there differ it is held, by its label and collection, to
    every tenant-scoped class stored there as well (review of #228, where
    such a copy of a t2 class passed); see :func:`_unlisted_placeholder`.
    """
    if not classes:
        return
    target = builder or visitor.builder
    listed = list(classes)
    # Every listed class's tenant root is committed: two roots are refused.
    placeholders: list[str | None] = [visitor._tenant_placeholder(cls) for cls in listed]
    if not class_tested:
        # The document may be of any class stored where it is read, so it is
        # held to every tenant-scoped one there too (review of #228, where a
        # copy of an unlisted t2 class passed). Those classes commit no root.
        stored = {name for cls in listed for name in (cls.shard_family or (cls.collection,))}
        for cls in visitor.resolver.tenant_scoped_classes():
            if stored & set(cls.shard_family or (cls.collection,)) and all(cls.iri != c.iri for c in listed):
                listed.append(cls)
                placeholders.append(_unlisted_placeholder(visitor, cls))
    if len({cls.tenant_field for cls in listed}) == 1 and _HIDDEN not in placeholders:
        # Scoped alike (one field, whatever the roots), or not at all: the
        # single, index-friendly FILTER. No unlisted class needs placing
        # under a root then (recheck of #228: refusing here refused reads
        # main answered). A listed class carries a bind when any does.
        bind = next((b for b in placeholders if b and b != _UNDECIDED), None)
        field = listed[0].tenant_field
        if bind is not None and field is not None:
            target.filter_eq(attribute_ref(alias, field), bind)
        return
    undecided = [cls for cls, bind in zip(listed, placeholders, strict=True) if bind == _UNDECIDED]
    if undecided:
        roots = sorted({c.tenant_entity for c in visitor.resolver.tenant_scoped_classes() if c.tenant_entity})
        raise CrossTenantJoinError(
            f"a read of the copies of an IRI can reach documents of {undecided[0].iri!r}, under tenant root "
            f"{undecided[0].tenant_entity!r}, before the query names a class under one of the mapping's "
            f"roots ({', '.join(roots)}); one tenant id cannot be placed under a root (PRD §6.5.1)"
        )
    spans_collections = len({c.collection for c in listed}) > 1
    terms = []
    for cls, placeholder in zip(listed, placeholders, strict=True):
        if placeholder is None:
            continue
        assert cls.tenant_field is not None  # tenant_scoped_classes / _tenant_placeholder guarantee it
        member = _membership(target, alias, cls, listed, spans_collections)
        test = (
            "false"
            if placeholder == _HIDDEN
            else f"{attribute_ref(alias, cls.tenant_field)} == {placeholder}"
        )
        terms.append(f"(!({member}) || {test})")
    if terms:
        target.filter_raw(terms[0] if len(terms) == 1 else " && ".join(terms))


_HIDDEN = "false"
_UNDECIDED = "undecided"


def _unlisted_placeholder(visitor: AlgebraVisitor, cls: ResolvedClass) -> str:
    """The tenant bind for a scoped class the read does not name, or
    ``_HIDDEN`` when its documents cannot be visible to this query.

    Hidden without a tenant context (as a walked document is), and when the
    class is under another tenant root than the query: the same tenant id
    under another root is another tenant. When the query has committed no
    root yet, the class is checked on its field if the mapping has one root
    at most, and is ``_UNDECIDED`` otherwise: the caller refuses the read
    unless one field holds every class there."""
    if visitor.tenant_id is None:
        return _HIDDEN
    committed = visitor.state.tenant.entity
    if cls.tenant_entity is not None:
        if committed is not None and committed != cls.tenant_entity:
            return _HIDDEN
        if committed is None:
            roots = sorted(
                {c.tenant_entity for c in visitor.resolver.tenant_scoped_classes() if c.tenant_entity}
            )
            if len(roots) > 1:
                # Which root the tenant id belongs to is not known yet: a
                # pattern read later may commit one. Hiding these copies
                # dropped t1 rows depending on pattern order (second recheck
                # of #228); the caller refuses the read if it needs this
                # class's own test.
                return _UNDECIDED
    if visitor.state.tenant_bind_placeholder is None:
        visitor.state.tenant_bind_placeholder = visitor.builder.bind(visitor.tenant_id, hint="tenant")
    return visitor.state.tenant_bind_placeholder


def _membership(
    builder: AqlQueryBuilder,
    alias: str,
    cls: ResolvedClass,
    classes: Sequence[ResolvedClass],
    spans_collections: bool,
) -> str:
    """The test that *alias* is a document of *cls*, among *classes*."""
    tests = []
    if spans_collections:
        names = cls.shard_family or (cls.collection,)
        same = [f"IS_SAME_COLLECTION({builder.bind(name, hint='coll')}, {alias})" for name in names]
        tests.append(same[0] if len(same) == 1 else "(" + " || ".join(same) + ")")
    if cls.type_field and cls.type_value:
        bind = builder.bind(
            cls.type_value, hint=cls.type_field if is_aql_identifier(cls.type_field) else None
        )
        tests.append(type_predicate(alias, cls.type_field, bind, is_array=cls.type_field_is_array))
    else:
        others = [
            c
            for c in classes
            if c is not cls and c.collection == cls.collection and c.tenant_field != cls.tenant_field
        ]
        if others:
            raise UnsupportedSparqlError(
                f"classes {cls.iri!r} and {others[0].iri!r} share collection {cls.collection!r} "
                f"with no type field to tell their documents apart, but are tenant-scoped "
                f"differently; one read cannot hold each document to its own class's tenant "
                f"field (PRD §6.5.1)"
            )
    return " && ".join(tests)
