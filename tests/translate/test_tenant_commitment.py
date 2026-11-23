"""A query commits to one tenant root wherever its classes sit (#211, PRD §6.5.1).

The commitment was copied into a UNION arm or OPTIONAL subquery and never
reached the parent, and a MINUS / EXISTS probe or a sub-select started
without it. A join across tenant roots was refused in one pattern but
translated when the second root sat inside MINUS, EXISTS or a sub-select,
or in a UNION or OPTIONAL placed first. Every nested scope now shares the
one commitment (``_BindingState.share_query_state``).
"""

from __future__ import annotations

import pytest

from arango_sparql.api import translate
from arango_sparql.errors import CrossTenantJoinError
from arango_sparql.translate.resolver import SchemaResolver

_PREFIXES = """
@prefix :     <http://ex.org/> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
"""
# Person is under Org; Doc under Workspace (two roots), or under Org too.
_TWO_ROOTS = (
    _PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" ; phys:tenantField "org" ; phys:tenantEntity "Org" .
:Doc a owl:Class ; phys:collectionName "Doc" ; phys:tenantField "ws" ; phys:tenantEntity "Workspace" .
"""
)
_ONE_ROOT = _TWO_ROOTS.replace('phys:tenantEntity "Workspace"', 'phys:tenantEntity "Org"')

# Each body joins a Person with a Doc; the comment names the scope the Doc
# (or the Person) sits in.
_SHAPES = {
    "same_pattern": "?p a :Person . ?d a :Doc",
    "union_after": "?p a :Person { ?d a :Doc } UNION { ?d a :Doc }",
    "optional_after": "?p a :Person OPTIONAL { ?d a :Doc }",
    "minus": "?p a :Person MINUS { ?p a :Doc }",
    "exists": "?p a :Person FILTER EXISTS { ?d a :Doc }",
    "not_exists": "?p a :Person FILTER NOT EXISTS { ?d a :Doc }",
    "subselect_after": "?p a :Person { SELECT ?d WHERE { ?d a :Doc } }",
    "subselect_first": "{ SELECT ?d WHERE { ?d a :Doc } } ?p a :Person",
    "union_first": "{ ?d a :Doc } UNION { ?d a :Doc } ?p a :Person",
    "optional_first": "OPTIONAL { ?d a :Doc } ?p a :Person",
    # Two levels down: an EXISTS inside a UNION arm, a sub-select inside MINUS.
    "exists_in_union_arm": "?p a :Person { FILTER EXISTS { ?d a :Doc } } UNION { ?p a :Person }",
    "subselect_in_minus": "?p a :Person MINUS { SELECT ?p WHERE { ?p a :Doc } }",
}


def _translate(ttl: str, body: str, tenant: str | None = "t1"):  # type: ignore[no-untyped-def]
    return translate(
        f"PREFIX : <http://ex.org/>\nSELECT * WHERE {{ {body} }}",
        resolver=SchemaResolver.from_turtle(ttl),
        tenant_id=tenant,
    )


@pytest.mark.parametrize("body", list(_SHAPES.values()), ids=list(_SHAPES))
def test_a_join_across_tenant_roots_is_refused_in_every_scope(body: str) -> None:
    with pytest.raises(CrossTenantJoinError, match="across tenant roots"):
        _translate(_TWO_ROOTS, body)


@pytest.mark.parametrize("body", list(_SHAPES.values()), ids=list(_SHAPES))
def test_a_join_under_one_tenant_root_still_translates_in_every_scope(body: str) -> None:
    result = _translate(_ONE_ROOT, body)
    # Every tenant filter compares with the caller's tenant.
    tenant_binds = {k: v for k, v in result.bind_vars.items() if k.endswith("_tenant")}
    assert tenant_binds and set(tenant_binds.values()) == {"t1"}


@pytest.mark.parametrize(
    "key", ["minus", "exists", "not_exists", "subselect_after", "union_after", "optional_after"]
)
def test_a_nested_scope_reuses_the_tenant_bind_the_query_already_has(key: str) -> None:
    # The outer Person minted the tenant bind first; the probe and the
    # sub-select used to mint a second one.
    result = _translate(_ONE_ROOT, _SHAPES[key])
    assert sorted(k for k in result.bind_vars if k.endswith("_tenant")) == ["_p1_tenant"], result.aql


def test_without_a_tenant_context_a_scoped_class_in_a_probe_is_refused() -> None:
    # Unchanged by #211, pinned beside it: no tenant, no read of a scoped class.
    # The outer pattern reads no class, so the refusal comes from the probe.
    with pytest.raises(CrossTenantJoinError, match="no tenant context"):
        _translate(_ONE_ROOT, "VALUES ?x { :a } FILTER EXISTS { ?d a :Doc }", tenant=None)
