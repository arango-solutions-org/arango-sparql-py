"""A group with a nested OPTIONAL is evaluated on its own, on ArangoDB (#194).

In ``?s :knows ?x OPTIONAL { ?s :name ?n OPTIONAL { ?x :email ?m } }`` the
second OPTIONAL's group binds ``?x`` only through its nested OPTIONAL. SPARQL
1.1 §18.5 evaluates that group without the outer ``?x`` and then keeps only
its solutions compatible with it (§18.3). The translator read the nested
OPTIONAL against the outer ``?x``, which bound ``?n`` for a person whose
contact has no email: a silent wrong answer on every mapping.

Live because the fix is a FILTER placed after a padded subquery, whose
result depends on how ArangoDB evaluates it. Each query runs on a triples
table and on PG documents with an edge collection, and must match pyoxigraph
on the same facts. Gated like the rest of the integration suite
(``RUN_INTEGRATION=1``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
import rdflib

from arango_sparql.api import translate
from arango_sparql.errors import UnsupportedSparqlError
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.oxi import drop_null_bindings, normalize_oxi_row, oxi_bindings
from tests.integration.conftest import (
    DEFAULT_ARANGO_DB,
    DEFAULT_ARANGO_PASSWORD,
    DEFAULT_ARANGO_URL,
    DEFAULT_ARANGO_USER,
    arangodb_reachable,
    ensure_test_database,
    integration_enabled,
    try_boot_arangodb_via_compose,
)

pytestmark = pytest.mark.integration

oxi = pytest.importorskip("pyoxigraph", reason="pyoxigraph is the ground truth for these tests")

EX = "http://example.org/"
_TRIPLES = "NestedOptTriples"
_PEOPLE = "NestedOptPerson"
_KNOWS = "NestedOptKnows"
_LIKES = "NestedOptLikes"
_EDGES = {"knows": _KNOWS, "likes": _LIKES}

# a knows b and c, d knows e, g knows c; only b and e have an email. a likes
# b and b likes c; g likes nobody (review of #197).
_PERSONS: dict[str, dict[str, Any]] = {
    "a": {"name": "A", "age": 30, "knows": ["b", "c"], "likes": ["b"]},
    "b": {"name": "B", "age": 40, "email": "b@x.org", "likes": ["c"]},
    "c": {"name": "C", "age": 20},
    "d": {"name": "D", "age": 50, "knows": ["e"]},
    "e": {"name": "E", "email": "e@x.org"},
    "g": {"name": "G", "knows": ["c"]},
}
_DATA = f"@prefix : <{EX}> .\n" + "\n".join(
    " ; ".join(
        [f":{k} a :Person"]
        + [f":{p} {v}" if isinstance(v, int) else f':{p} "{v}"' for p, v in d.items() if p not in _EDGES]
        + [f":{p} :{t}" for p in _EDGES for t in d.get(p, [])]
    )
    + " ."
    for k, d in _PERSONS.items()
)
_HEAD = f"""
@prefix :     <{EX}> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix phys: <https://arango.solutions/phys#> .
"""
_ONTOLOGIES = {
    "rpt": _HEAD + f':Person a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "{_TRIPLES}" .\n',
    "pg_edge": _HEAD
    + f"""
:Person a owl:Class ; phys:collectionName "{_PEOPLE}" .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "{_KNOWS}" ; rdfs:domain :Person ; rdfs:range :Person .
:likes a owl:ObjectProperty ; phys:edgeCollectionName "{_LIKES}" ; rdfs:domain :Person ; rdfs:range :Person .
:name a owl:DatatypeProperty ; rdfs:domain :Person ; phys:attributeName "name" .
:email a owl:DatatypeProperty ; rdfs:domain :Person ; phys:attributeName "email" .
:age a owl:DatatypeProperty ; rdfs:domain :Person ; phys:attributeName "age" .
""",
}
_PREFIX = f"PREFIX : <{EX}>\n"


def _triple_rows() -> list[dict[str, Any]]:
    rows = []
    for s, p, o in rdflib.Graph().parse(data=_DATA, format="turtle"):
        row: dict[str, Any] = {"subject_uri": str(s), "predicate": str(p)}
        if isinstance(o, rdflib.URIRef):
            row["object_uri"] = str(o)
        else:
            row["object_value"] = o.toPython()
        rows.append(row)
    return rows


def _collections() -> dict[str, tuple[list[dict[str, Any]], bool]]:
    people = [
        {"_key": k, "_uri": EX + k, **{p: v for p, v in d.items() if p not in _EDGES}}
        for k, d in _PERSONS.items()
    ]
    edges = {
        collection: (
            [
                {"_from": f"{_PEOPLE}/{k}", "_to": f"{_PEOPLE}/{t}"}
                for k, d in _PERSONS.items()
                for t in d.get(p, [])
            ],
            True,
        )
        for p, collection in _EDGES.items()
    }
    return {_TRIPLES: (_triple_rows(), False), _PEOPLE: (people, False), **edges}


@pytest.fixture(scope="module")
def db() -> Iterator[Any]:
    if not integration_enabled():
        pytest.skip("set RUN_INTEGRATION=1 to enable integration tests")
    if not arangodb_reachable() and not try_boot_arangodb_via_compose():
        pytest.skip(f"ArangoDB at {DEFAULT_ARANGO_URL} is unreachable and could not be booted")
    from arango import ArangoClient

    ensure_test_database()
    handle = ArangoClient(hosts=DEFAULT_ARANGO_URL).db(
        DEFAULT_ARANGO_DB, username=DEFAULT_ARANGO_USER, password=DEFAULT_ARANGO_PASSWORD
    )
    for name, (docs, is_edge) in _collections().items():
        if handle.has_collection(name):
            handle.delete_collection(name)
        handle.create_collection(name, edge=is_edge).insert_many(docs)
    yield handle
    for name in _collections():
        handle.delete_collection(name)


@pytest.fixture(scope="module")
def oxi_store() -> Any:
    store = oxi.Store()
    store.load(_DATA.encode("utf-8"), oxi.RdfFormat.TURTLE)
    return store


def _multiset(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(json.dumps(r, sort_keys=True) for r in rows)


@pytest.mark.parametrize("mapping", sorted(_ONTOLOGIES))
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            "SELECT ?s ?x ?m ?n WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } "
            "OPTIONAL { ?s :name ?n OPTIONAL { ?x :email ?m } } }",
            id="issue_194",
        ),
        pytest.param(
            "SELECT ?s ?x ?n ?m WHERE { ?s a :Person ; :knows ?x OPTIONAL { ?s :name ?n OPTIONAL { ?x :email ?m } } }",
            id="required_x",
        ),
        pytest.param(
            "SELECT ?s ?x ?n ?m WHERE { ?s a :Person ; :knows ?x { ?s :name ?n OPTIONAL { ?x :email ?m } } }",
            id="required_group",
        ),
        pytest.param(
            "SELECT ?s ?x WHERE { ?s a :Person ; :knows ?x MINUS { ?s :name ?n OPTIONAL { ?x :email ?m } } }",
            id="minus",
        ),
        # Control: a sub-select column its own pattern always binds is
        # substituted as before (review of #197).
        pytest.param(
            "SELECT ?s ?x ?n ?m WHERE { ?s :knows ?x OPTIONAL { ?s :name ?n "
            "{ SELECT ?s ?x WHERE { ?s :likes ?x } } OPTIONAL { ?x :email ?m } } }",
            id="subselect_bound_column",
        ),
        # The group's ?x comes from an earlier OPTIONAL in it (review of #197):
        # g likes nobody, so (g, c) keeps ?n unbound. Main refused it.
        pytest.param(
            "SELECT ?s ?x ?n ?m WHERE { ?s :knows ?x "
            "OPTIONAL { ?s :name ?n OPTIONAL { ?s :likes ?x } OPTIONAL { ?x :email ?m } } }",
            id="earlier_optional_binds_x",
        ),
        # Three levels deep, with the innermost ?x from ``:likes`` (review of #197).
        pytest.param(
            "SELECT ?s ?x ?n ?a ?m WHERE { ?s :knows ?x OPTIONAL { ?s :name ?n "
            "OPTIONAL { ?s :age ?a OPTIONAL { ?s :likes ?x } OPTIONAL { ?x :email ?m } } } }",
            id="three_levels_rebind",
        ),
        # MINUS's disjoint-domain guard for a group-scoped variable: only a ?t
        # that likes the outer ?o removes the row.
        pytest.param(
            "SELECT ?s ?o WHERE { ?s :knows ?o MINUS { ?t :name ?n OPTIONAL { ?t :likes ?o . ?o :name ?on } } }",
            id="minus_overlap_guard",
        ),
        # The right side's OPTIONAL re-binds the outer ?o: it binds its own and
        # compares it (on a triples table it used to be refused).
        pytest.param(
            "SELECT ?s ?o WHERE { ?s a :Person ; :knows ?o MINUS { ?t a :Person OPTIONAL { ?t :knows ?o } } }",
            id="minus_rebind",
        ),
        # MINUS over a sub-select, with ?x unbound in the rows of d, e and g:
        # those rows overlap the right side on ?s alone, so a right-side
        # solution for their ?s removes them (§8.3.4). MINUS's walker could
        # not see into a sub-select and joined ?x as a value, keeping d and g
        # (code-quality review of #196–#199).
        pytest.param(
            "SELECT ?s ?x WHERE { ?s :name ?n OPTIONAL { ?s :likes ?x } "
            "MINUS { SELECT ?s ?x WHERE { ?s :knows ?x } } }",
            id="minus_subselect_open_outer",
        ),
        pytest.param(
            "SELECT ?s ?x WHERE { ?s :name ?n OPTIONAL { ?s :likes ?x } "
            "MINUS { SELECT DISTINCT ?s ?x WHERE { ?s :knows ?x } LIMIT 10 } }",
            id="minus_subselect_distinct_limit",
        ),
        # The sub-select's ?x can itself be unbound: no row is certain to
        # overlap, and every row is removed.
        pytest.param(
            "SELECT ?s ?x WHERE { ?s :name ?n OPTIONAL { ?s :likes ?x } "
            "MINUS { SELECT ?s ?x WHERE { ?s :name ?m OPTIONAL { ?s :knows ?x } } } }",
            id="minus_subselect_optional_column",
        ),
        pytest.param(
            "SELECT ?s ?x WHERE { ?s :name ?n OPTIONAL { ?s :likes ?x } "
            "MINUS { SELECT ?s (MIN(?k) AS ?x) WHERE { ?s :knows ?k } GROUP BY ?s } }",
            id="minus_subselect_aggregate_column",
        ),
        # A sub-select column computed by an aggregate, a projected copy, a
        # BIND or a constant: MINUS did not count it as shared at all, since
        # it read only triples and expressions, and matched on ?s alone,
        # keeping a's (a, b) or removing rows it must keep (#212).
        pytest.param(
            "SELECT ?s ?x WHERE { ?s :name ?n OPTIONAL { ?s :likes ?x } "
            "MINUS { SELECT ?s (COUNT(?k) AS ?x) WHERE { ?s :knows ?k } GROUP BY ?s } }",
            id="minus_subselect_count_column",
        ),
        pytest.param(
            "SELECT ?s ?x WHERE { ?s :name ?n OPTIONAL { ?s :likes ?x } "
            "MINUS { SELECT ?s (?k AS ?x) WHERE { ?s :knows ?k } } }",
            id="minus_subselect_copy_column",
        ),
        pytest.param(
            "SELECT ?s ?x WHERE { ?s :name ?n OPTIONAL { ?s :likes ?x } "
            "MINUS { SELECT ?s ?x WHERE { ?s :knows ?k BIND(?k AS ?x) } } }",
            id="minus_subselect_bind_column",
        ),
        pytest.param(
            "SELECT ?s ?x WHERE { ?s :name ?n OPTIONAL { ?s :likes ?x } "
            "MINUS { SELECT ?s (:c AS ?x) WHERE { ?s :knows ?k } } }",
            id="minus_subselect_constant_column",
        ),
        pytest.param(
            "SELECT ?s ?x WHERE { ?s :knows ?x MINUS { SELECT (?k AS ?x) WHERE { ?t :likes ?k } } }",
            id="minus_subselect_only_shared_column",
        ),
        # ?s is bound inside the sub-select but not projected: not shared.
        pytest.param(
            "SELECT ?s WHERE { ?s :name ?n MINUS { SELECT ?k WHERE { ?s :knows ?k } } }",
            id="minus_subselect_unprojected",
        ),
        # A right side that binds the shared variables only through VALUES
        # (#216), with UNDEF cells (#208): MINUS saw no shared variable and
        # removed nothing.
        pytest.param(
            "SELECT ?s WHERE { ?s :name ?n MINUS { VALUES ?s { :a :d } } }",
            id="minus_values_only",
        ),
        pytest.param(
            "SELECT ?s ?x WHERE { ?s :name ?n OPTIONAL { ?s :likes ?x } MINUS { VALUES (?s ?x) { (:a :b) } } }",
            id="minus_values_open_outer",
        ),
        pytest.param(
            "SELECT ?s ?x WHERE { ?s :knows ?x MINUS { VALUES ?x { :c } } }",
            id="minus_values_object",
        ),
        pytest.param(
            "SELECT ?s ?x WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } "
            "MINUS { VALUES (?s ?x) { (:z UNDEF) (UNDEF :b) } } }",
            id="minus_values_undef_per_row",
        ),
        pytest.param(
            "SELECT ?s ?x WHERE { ?s :knows ?x MINUS { VALUES (?s ?x) { (:a :c) (:g UNDEF) } } }",
            id="minus_values_undef_cell",
        ),
        pytest.param(
            "SELECT ?s ?x WHERE { ?s :name ?n OPTIONAL { ?s :likes ?x } "
            "MINUS { SELECT ?s ?x WHERE { VALUES (?s ?x) { (:a :b) (:d UNDEF) (:g :c) } } } }",
            id="minus_subselect_values",
        ),
        # A right-side solution that leaves every shared variable unbound
        # shares nothing with the row and removes nothing (§8.3.4). Joined
        # with the outer values, such a solution matched every row: the
        # branch of #227 removed them all (its review). The body is now
        # evaluated apart and the overlap tested per solution.
        pytest.param(
            "SELECT ?s ?n WHERE { ?s :name ?n MINUS { VALUES ?s { UNDEF :b } } }",
            id="minus_values_all_undef_row",
        ),
        pytest.param(
            "SELECT ?s ?n WHERE { ?s :name ?n MINUS { VALUES (?s ?n) { (UNDEF UNDEF) (:a UNDEF) } } }",
            id="minus_values_all_undef_pair",
        ),
        pytest.param(
            "SELECT ?s ?a WHERE { ?s :age ?a MINUS { SELECT (MIN(?x) AS ?a) WHERE { ?t :age ?x FILTER(?x > 100) } } }",
            id="minus_aggregate_over_no_rows",
        ),
        pytest.param(
            "SELECT ?s ?n WHERE { ?s :name ?n MINUS { { ?s :likes ?y } UNION { ?t :knows ?k } } }",
            id="minus_union_arm_binds_nothing_shared",
        ),
        pytest.param(
            "SELECT ?s ?n WHERE { ?s :name ?n MINUS { SELECT ?s WHERE { ?t :knows ?k OPTIONAL { ?t :likes ?s } } } }",
            id="minus_subselect_optional_only_column",
        ),
        pytest.param(
            "SELECT ?s ?e WHERE { ?s :name ?n OPTIONAL { ?s :email ?e } "
            "MINUS { ?x :name ?nn OPTIONAL { ?x :email ?e } } }",
            id="minus_optional_only_shared",
        ),
        # Mixed certainty: ?s is bound in every right-side solution but the
        # outer row can leave it unbound, and ?n / ?e are bound by only some
        # solutions. No anchor, so the body is evaluated apart; joined with
        # the outer values the unbound ?s matched nothing and kept rows
        # §8.3.4 removes (second review of #227; wrong on main).
        pytest.param(
            "SELECT ?x ?n ?s WHERE { ?x :name ?n OPTIONAL { ?x :likes ?s } "
            'MINUS { ?s :knows ?k VALUES ?n { "G" UNDEF } } }',
            id="minus_mixed_certainty_values",
        ),
        pytest.param(
            "SELECT ?x ?n ?s WHERE { ?x :name ?n OPTIONAL { ?x :likes ?s } "
            "MINUS { ?s :knows ?k OPTIONAL { ?s :name ?n } } }",
            id="minus_mixed_certainty_optional",
        ),
        pytest.param(
            "SELECT ?x ?e ?s WHERE { ?x :name ?nn OPTIONAL { ?x :likes ?s } OPTIONAL { ?x :email ?e } "
            "MINUS { ?s :name ?w OPTIONAL { ?s :email ?e } } }",
            id="minus_mixed_certainty_two_optionals",
        ),
        # Inside FILTER EXISTS / NOT EXISTS the outer row's variables are
        # bound on both sides of a nested MINUS, as pyoxigraph (and rdflib)
        # evaluate it: they are shared, so a right side that binds nothing
        # else still removes every left solution (third review of #227; main
        # took it as a no-op, and the apart probe read ?s as unbound).
        pytest.param(
            "SELECT ?s ?n WHERE { ?s :name ?n FILTER NOT EXISTS { ?t :knows ?k MINUS { ?z :email ?w } } }",
            id="exists_minus_outer_variable_shared",
        ),
        pytest.param(
            "SELECT ?s ?n WHERE { ?s :name ?n FILTER NOT EXISTS { ?s :knows ?k "
            "MINUS { { ?k :email ?m FILTER(?k != ?s) } UNION { VALUES ?q { 1 } } } } }",
            id="exists_minus_union_reads_outer",
        ),
        pytest.param(
            "SELECT ?s ?n WHERE { ?s :name ?n FILTER EXISTS { ?s :knows ?k MINUS { VALUES ?k { UNDEF :b } } } }",
            id="exists_minus_values_undef",
        ),
        pytest.param(
            "SELECT ?s ?n WHERE { ?s :name ?n FILTER NOT EXISTS { ?t :knows ?k MINUS { ?s :likes ?y } } }",
            id="exists_minus_right_binds_outer",
        ),
        # A column or BIND whose expression can error is unbound where it
        # errors (§18.5), compatible with any value; joined as bound it found
        # no match there (#226; wrong on main).
        pytest.param(
            "SELECT ?s ?x WHERE { ?s :likes ?x { SELECT ?s (IF(?k = :c, ?k, 1/0) AS ?x) WHERE { ?s :knows ?k } } }",
            id="erroring_column_join_after",
        ),
        pytest.param(
            "SELECT ?s ?x WHERE { { SELECT ?s (IF(?k = :c, ?k, 1/0) AS ?x) WHERE { ?s :knows ?k } } ?s :likes ?x }",
            id="erroring_column_join_before",
        ),
        pytest.param(
            "SELECT ?s ?x WHERE { ?s :name ?n OPTIONAL { ?s :likes ?x } "
            "MINUS { SELECT ?s (IF(?k = :c, ?k, 1/0) AS ?x) WHERE { ?s :knows ?k } } }",
            id="erroring_column_minus",
        ),
        pytest.param(
            "SELECT ?s ?x WHERE { ?s :knows ?k BIND(IF(?k = :c, ?k, 1/0) AS ?x) ?s :likes ?x }",
            id="erroring_bind_join",
        ),
        pytest.param(
            "SELECT ?s ?x WHERE { ?s :knows ?k BIND(IF(?k = :c, ?k, 1/0) AS ?x) ?x :name ?n }",
            id="erroring_bind_subject",
        ),
        pytest.param(
            "SELECT ?s ?a ?b WHERE { ?s :age ?a BIND(?a + 1 AS ?b) ?t :age ?b }",
            id="arithmetic_bind_join",
        ),
        # An OPTIONAL value joined to an attribute of its own document: the
        # scan is never split around itself (ERR 1203 on an earlier head of
        # #236).
        pytest.param(
            "SELECT ?s ?x WHERE { ?s a :Person OPTIONAL { ?s :email ?x } ?s :name ?x }",
            id="optional_value_own_attribute",
        ),
        # A MINUS evaluated apart inside an OPTIONAL, a UNION arm or an ASK
        # is computed once at the top of the query (#234); its answers are
        # unchanged.
        pytest.param(
            "SELECT ?s ?k WHERE { ?s :name ?n OPTIONAL { ?s :knows ?k "
            "MINUS { { ?k :email ?m } UNION { VALUES ?q { 1 } } } } }",
            id="hoisted_minus_in_optional",
        ),
        pytest.param(
            "SELECT ?s WHERE { { ?s :name ?n MINUS { { ?s :likes ?y } UNION { BIND(1 AS ?q) } } } "
            "UNION { ?s :knows ?k } }",
            id="hoisted_minus_in_union_arm",
        ),
        pytest.param(
            "SELECT ?s ?c WHERE { ?s :name ?n { SELECT ?s (COUNT(?k) AS ?c) WHERE { ?s :knows ?k "
            "MINUS { VALUES ?k { UNDEF :c } } } GROUP BY ?s } }",
            id="hoisted_minus_in_grouped_subselect",
        ),
        # ?x is read by the right side's FILTER, never bound by it: it is
        # outside every right-side solution, the sides share nothing, and
        # MINUS removes nothing (#209, its first shape).
        pytest.param(
            "SELECT ?s ?x WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } MINUS { ?y :name ?n FILTER(?y = ?x) } }",
            id="minus_filter_only_variable",
        ),
        # A BIND in an OPTIONAL of the group binds the group's own ?x: it is
        # computed, never substituted by the outer value, so a later
        # OPTIONAL and the padding read the group's value (review of #213;
        # main counted the BIND target as always bound and kept a row for
        # a's ?x = c with a's ?k = b).
        pytest.param(
            "SELECT ?s ?x ?n ?k WHERE { ?s :knows ?x OPTIONAL { ?s :name ?n "
            "OPTIONAL { ?s :likes ?k BIND(?k AS ?x) } } }",
            id="group_optional_bind_copy",
        ),
        pytest.param(
            "SELECT ?s ?x ?n ?m WHERE { ?s :knows ?x OPTIONAL { ?s :name ?n "
            "OPTIONAL { ?s :likes ?k BIND(?k AS ?x) } OPTIONAL { ?x :email ?m } } }",
            id="group_optional_bind_copy_then_contact",
        ),
        pytest.param(
            "SELECT ?s ?x WHERE { ?s :knows ?x MINUS { ?s :name ?n OPTIONAL { ?s :likes ?k BIND(?k AS ?x) } } }",
            id="minus_optional_bind_copy",
        ),
        # Refused on main ("binds in only some solutions"), answered now.
        pytest.param(
            "SELECT ?s ?x ?n WHERE { ?s :knows ?x OPTIONAL { ?s :name ?n OPTIONAL { BIND(:c AS ?x) } } }",
            id="group_optional_bind_constant",
        ),
        pytest.param(
            "SELECT ?s ?x ?n ?m WHERE { ?s :knows ?x OPTIONAL { ?s :name ?n "
            "OPTIONAL { ?s :likes ?k BIND(IF(?k = :c, ?k, 1/0) AS ?x) } OPTIONAL { ?x :email ?m } } }",
            id="group_optional_bind_expression",
        ),
        # A sub-select column that copies a variable bound in every solution
        # is substituted, as on main.
        pytest.param(
            "SELECT ?s ?x ?n ?m WHERE { ?s :knows ?x OPTIONAL { ?s :name ?n "
            "{ SELECT ?s (?k AS ?x) WHERE { ?s :likes ?k } } OPTIONAL { ?x :email ?m } } }",
            id="subselect_copy_column",
        ),
        pytest.param(
            "SELECT ?s ?x ?n ?m WHERE { ?s :knows ?x OPTIONAL { ?s :name ?n "
            "{ SELECT ?s ?x WHERE { ?s :likes ?k BIND(?k AS ?x) } } OPTIONAL { ?x :email ?m } } }",
            id="subselect_bind_copy_column",
        ),
    ],
)
def test_nested_optional_group_scope_matches_oxigraph(
    db: Any, oxi_store: Any, mapping: str, body: str
) -> None:
    res = translate(_PREFIX + body, resolver=SchemaResolver.from_turtle(_ONTOLOGIES[mapping]))
    db.aql.validate(res.aql)
    actual = [drop_null_bindings(r) for r in db.aql.execute(res.aql, bind_vars=res.bind_vars)]
    expected = [normalize_oxi_row(r) for r in oxi_bindings(oxi_store, _PREFIX + body)]
    assert _multiset(actual) == _multiset(expected)


def test_issue_194_keeps_the_name_unbound_when_no_contact_is_compatible(db: Any) -> None:
    """The issue's row by hand: c has no email, so the (x = c) rows of a and g get no ?n."""
    body = (
        "SELECT ?s ?x ?m ?n WHERE { ?s a :Person ; :knows ?x "
        "OPTIONAL { ?s :name ?n OPTIONAL { ?x :email ?m } } FILTER(?x = :c) }"
    )
    for mapping in sorted(_ONTOLOGIES):
        res = translate(_PREFIX + body, resolver=SchemaResolver.from_turtle(_ONTOLOGIES[mapping]))
        rows = [drop_null_bindings(r) for r in db.aql.execute(res.aql, bind_vars=res.bind_vars)]
        assert sorted(rows, key=lambda r: r["s"]) == [
            {"s": EX + "a", "x": EX + "c"},
            {"s": EX + "g", "x": EX + "c"},
        ], mapping


@pytest.mark.parametrize("mapping", sorted(_ONTOLOGIES))
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            "SELECT ?s ?x ?n ?m WHERE { ?s :knows ?x OPTIONAL { ?s :name ?n VALUES ?x { UNDEF } OPTIONAL { ?x :email ?m } } }",
            id="values_undef",
        ),
        pytest.param(
            "SELECT ?s ?x ?n ?m WHERE { ?s :knows ?x OPTIONAL { ?s :name ?n "
            "{ SELECT ?s ?x WHERE { ?s :name ?nn OPTIONAL { ?s :likes ?x } } } OPTIONAL { ?x :email ?m } } }",
            id="subselect_optional_column",
        ),
    ],
)
def test_untracked_group_value_is_refused_or_answered_correctly(
    db: Any, oxi_store: Any, mapping: str, body: str
) -> None:
    """VALUES with UNDEF or a sub-select in the group's required side (review of #197).

    Translation refuses these today. Should they be answered later, the answer
    must match pyoxigraph: main and the first fix bound ?n for (a, c) and (g, c).
    """
    try:
        res = translate(_PREFIX + body, resolver=SchemaResolver.from_turtle(_ONTOLOGIES[mapping]))
    except UnsupportedSparqlError:
        return
    actual = [drop_null_bindings(r) for r in db.aql.execute(res.aql, bind_vars=res.bind_vars)]
    expected = [normalize_oxi_row(r) for r in oxi_bindings(oxi_store, _PREFIX + body)]
    assert _multiset(actual) == _multiset(expected)
