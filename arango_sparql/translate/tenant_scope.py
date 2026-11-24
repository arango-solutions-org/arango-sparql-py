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

A document of an unscoped class passes, as :meth:`AlgebraVisitor._tenant_test`
lets one a walk reaches pass; one with labels of two scoped classes is held
to both. Membership is the class's label (its ``phys:typeField``) and, when
the classes span collections, its collection. Two classes in one collection
with no label to tell them apart cannot be scoped differently, so that read
is refused. Every class's tenant root is committed, so classes under two
roots are refused as a join across them is (:class:`CrossTenantJoinError`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..errors import UnsupportedSparqlError
from .builder import AqlQueryBuilder, attribute_ref, is_aql_identifier, type_predicate

if TYPE_CHECKING:
    from .resolver import ResolvedClass
    from .visitor import AlgebraVisitor


def enforce_classes_tenant_scope(
    visitor: AlgebraVisitor,
    alias: str,
    classes: Sequence[ResolvedClass],
    builder: AqlQueryBuilder | None = None,
) -> None:
    """Emit the tenant FILTER for *alias*, a document of one of *classes*."""
    if not classes:
        return
    if len({(c.tenant_field, c.tenant_entity) for c in classes}) == 1:
        visitor._enforce_tenant_scope(alias, classes[0], builder=builder)
        return
    target = builder or visitor.builder
    spans_collections = len({c.collection for c in classes}) > 1
    terms = []
    for cls in classes:
        placeholder = visitor._tenant_placeholder(cls)
        if placeholder is None:
            continue
        assert cls.tenant_field is not None  # _tenant_placeholder guarantees it
        member = _membership(target, alias, cls, classes, spans_collections)
        terms.append(f"(!({member}) || {attribute_ref(alias, cls.tenant_field)} == {placeholder})")
    if terms:
        target.filter_raw(terms[0] if len(terms) == 1 else " && ".join(terms))


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
            if c is not cls
            and c.collection == cls.collection
            and (c.tenant_field, c.tenant_entity) != (cls.tenant_field, cls.tenant_entity)
        ]
        if others:
            raise UnsupportedSparqlError(
                f"classes {cls.iri!r} and {others[0].iri!r} share collection {cls.collection!r} "
                f"with no type field to tell their documents apart, but are tenant-scoped "
                f"differently; one read cannot hold each document to its own class's tenant "
                f"field (PRD §6.5.1)"
            )
    return " && ".join(tests)
