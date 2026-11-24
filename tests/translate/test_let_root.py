"""A LET for the top of the whole query, from any nested builder (#234).

``AqlQueryBuilder.let_root`` queues a LET that reads nothing of any
enclosing scope; :meth:`absorb_child` hands it to the parent, and the root
renders it before everything else. A child that is never absorbed (a UNION
arm's first pass, a probe tried and dropped) takes its LET with it, so no
query references a bind variable it did not declare.

MINUS uses it for a right side evaluated on its own: placed at the top of an
OPTIONAL subquery instead, it was rebuilt per outer row (5.9 s on 12,000
people, now 0.66 s; timed live, not here).
"""

from __future__ import annotations

import re

from arango_sparql.api import translate
from arango_sparql.translate.builder import AqlQueryBuilder
from arango_sparql.translate.resolver import SchemaResolver


def _child_with_root_let(parent: AqlQueryBuilder) -> AqlQueryBuilder:
    child = parent.create_child()
    value = child.bind([1, 2], hint="vals")
    child.let_root("hoisted", f"NOOPT({value})")
    inner = child.fresh_alias()
    child.for_(inner, "Thing")
    child.return_scalar(inner)
    return child


def _root() -> AqlQueryBuilder:
    root = AqlQueryBuilder()
    alias = root.fresh_alias()
    root.for_(alias, "Thing")
    return root


def test_an_absorbed_child_root_let_is_rendered_first_by_the_root() -> None:
    root = _root()
    root.let("arm", f"(\n{root.absorb_child(_child_with_root_let(root))}\n)")
    root.return_scalar("1")
    aql, binds = root.finalize()
    assert aql.startswith("LET hoisted = NOOPT(@")
    assert aql.index("LET hoisted") < aql.index("FOR ")
    # Its bind variable came up with the child's.
    assert all(name.lstrip("@") in binds for name in re.findall(r"@(\w+)", aql.split("\n")[0]))


def test_a_child_renders_no_root_let_of_its_own() -> None:
    root = _root()
    inner = root.absorb_child(_child_with_root_let(root))
    assert "LET hoisted" not in inner


def test_a_discarded_child_leaves_no_root_let() -> None:
    """Never absorbed: neither its binds nor its LET reach the query."""
    root = _root()
    _child_with_root_let(root)  # created and dropped
    root.return_scalar("1")
    aql, _ = root.finalize()
    assert "hoisted" not in aql


def test_a_grandchild_root_let_reaches_the_root() -> None:
    root = _root()
    child = root.create_child()
    child.for_(child.fresh_alias(), "Thing")
    child.let("arm", f"(\n{child.absorb_child(_child_with_root_let(child))}\n)")
    child.return_scalar("1")
    root.let("outer", f"(\n{root.absorb_child(child)}\n)")
    root.return_scalar("1")
    aql, _ = root.finalize()
    assert aql.startswith("LET hoisted")
    assert aql.count("LET hoisted") == 1


_HEAD = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix phys: <https://arango.solutions/phys#> .
:Person a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "triples" .
"""


def _aql(query: str) -> str:
    return translate("PREFIX : <http://ex.org/> " + query, resolver=SchemaResolver.from_turtle(_HEAD)).aql


def test_a_minus_inside_an_optional_is_hoisted_to_the_query_top() -> None:
    aql = _aql(
        "SELECT ?s ?k WHERE { ?s :name ?n OPTIONAL { ?s :knows ?k "
        "MINUS { { ?k :email ?m } UNION { VALUES ?q { 1 } } } } }"
    )
    assert aql.startswith("LET minus_values")


def test_a_minus_in_a_union_arm_is_hoisted_once() -> None:
    """``union_paths`` translates each arm twice; the first pass is dropped."""
    aql = _aql(
        "SELECT ?s WHERE { { ?s :name ?n MINUS { { ?s :likes ?y } UNION { BIND(1 AS ?q) } } } "
        "UNION { ?s :knows ?k } }"
    )
    assert aql.startswith("LET minus_values")
    assert aql.count("LET minus_values") == 1


def test_a_minus_inside_exists_reading_the_outer_row_stays_per_row() -> None:
    """Its body reads the outer row's ?s (EXISTS binds it on both sides), so
    it cannot be computed once."""
    aql = _aql(
        "SELECT ?s WHERE { ?s :name ?n FILTER NOT EXISTS { ?s :knows ?k "
        "MINUS { { ?k :email ?m } UNION { VALUES ?q { 1 } } } } }"
    )
    assert not aql.startswith("LET minus_")


def test_a_discarded_conditional_add_attempt_leaves_one_root_let() -> None:
    """MINUS first tries the conditional-add probe (``sink_only``) and drops
    it when its OPTIONAL does not bind every shared variable; this query
    drops it several times and must still hoist each body once (review of
    #235)."""
    aql = _aql(
        "SELECT ?s ?k WHERE { ?s :knows ?k MINUS { { ?k :email ?m MINUS { VALUES (?m ?y) "
        '{ ("b@x.org" 1) (UNDEF 2) } } } UNION { ?z :age 20 } OPTIONAL { ?s :likes ?w } } }'
    )
    assert aql.count("LET minus_values") <= 1
    assert aql.count("LET minus_rows") <= 1
    assert aql.count("LET minus_") >= 1


def test_absorbing_a_child_twice_hoists_its_let_once() -> None:
    root = _root()
    child = _child_with_root_let(root)
    root.absorb_child(child)
    root.absorb_child(child)
    root.return_scalar("1")
    aql, _ = root.finalize()
    assert aql.count("LET hoisted") == 1
