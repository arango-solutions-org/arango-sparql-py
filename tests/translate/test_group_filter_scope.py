"""Issue #107 — a FILTER is scoped to its group (SPARQL 1.1 §18.2.2).

A nested group or a UNION arm is translated after the pattern around it, so
its FILTER found the outer bindings in scope and read them. In
``?a a :Person { FILTER EXISTS { ?a :knows ?x } } UNION { ?a :livesIn ?r }``
``?a`` is unbound in the first arm: the EXISTS is true once anyone knows
anyone, the arm is one empty solution, and it joins with every person. We
tested each person instead. Each case runs on the AQL interpreter; the
expected rows are SPARQL's, worked by hand in the comment beside them.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.translate.mapping import MappingBundle, MappingSource
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.aql_interp import run_aql_subset

_C = "urn:arango-sparql:concept#"
_E = "http://ex.org/"
_PREFIXES = f"PREFIX : <{_C}>\nPREFIX ex: <{_E}>\n"


def _person(key: str, **fields: Any) -> dict[str, Any]:
    return {"_id": f"people/{key}", "_key": key, "_uri": f"{_E}{key}", **fields}


def _bundle(rels: list[str], props: list[str]) -> MappingBundle:
    return MappingBundle(
        conceptual_schema={
            "entities": [{"name": "Person", "properties": [{"name": p} for p in props]}],
            "relationships": [{"name": r, "fromEntity": "Person", "toEntity": "Person"} for r in rels],
        },
        physical_mapping={
            "entities": {
                "Person": {
                    "style": "COLLECTION",
                    "collectionName": "people",
                    "properties": {p: {"field": p} for p in props},
                }
            },
            "relationships": {
                r: {
                    "style": "DEDICATED_COLLECTION",
                    "edgeCollectionName": r,
                    "fromEntity": "Person",
                    "toEntity": "Person",
                }
                for r in rels
            },
        },
        source=MappingSource(kind="manual"),
    )


# The issue's data: :a :knows :b ; :livesIn :r . :a, :b and :c are Person.
_DOCS: dict[str, list[dict[str, Any]]] = {
    "people": [_person("a"), _person("b"), _person("c")],
    "knows": [{"_from": "people/a", "_to": "people/b"}],
    "livesIn": [{"_from": "people/a", "_to": "places/r"}],
    "places": [{"_id": "places/r", "_key": "r", "_uri": f"{_E}r"}],
}
_NOBODY_KNOWS = {**_DOCS, "knows": []}
_ISSUE = "SELECT ?a WHERE { ?a a :Person { FILTER EXISTS { ?a :knows ?x } } UNION { ?a :livesIn ?r } }"


def _rows(query: str, docs: dict[str, list[dict[str, Any]]], bundle: MappingBundle) -> Counter[str]:
    result = translate(_PREFIXES + query, resolver=SchemaResolver.from_mapping_bundle(bundle))
    return Counter(
        ",".join(f"{k}={str(v).rsplit('/', 1)[-1]}" for k, v in sorted(row.items()) if v is not None)
        for row in run_aql_subset(result.aql, result.bind_vars, docs)
    )


@pytest.mark.parametrize(
    ("query", "docs", "expected"),
    [
        # The arm is one empty solution (someone knows someone): every person, plus :a from :livesIn.
        pytest.param(_ISSUE, _DOCS, ["a", "a", "b", "c"], id="issue"),
        # Nobody knows anybody: the arm is empty, only :livesIn's :a is left.
        pytest.param(_ISSUE, _NOBODY_KNOWS, ["a"], id="exists-false"),
        pytest.param(
            "SELECT ?a WHERE { ?a a :Person { ?a :livesIn ?r } UNION { FILTER EXISTS { ?a :knows ?x } } }",
            _DOCS,
            ["a", "a", "b", "c"],
            id="filter-in-second-arm",
        ),
        # NOT EXISTS is false once anyone knows anyone: the arm is empty.
        pytest.param(
            "SELECT ?a WHERE { ?a a :Person { FILTER NOT EXISTS { ?a :knows ?x } } UNION { ?a :livesIn ?r } }",
            _DOCS,
            ["a"],
            id="not-exists",
        ),
        # ?a is unbound in the arm, so BOUND(?a) is false.
        pytest.param(
            "SELECT ?a WHERE { ?a a :Person { FILTER(BOUND(?a)) } UNION { ?a :livesIn ?r } }",
            _DOCS,
            ["a"],
            id="bound-reads-unbound",
        ),
        # A nested group without UNION: the same scope rule.
        pytest.param(
            "SELECT ?a WHERE { ?a a :Person { FILTER EXISTS { ?a :knows ?x } } }",
            _DOCS,
            ["a", "b", "c"],
            id="nested-group",
        ),
        # Unchanged: the arm binds ?a itself, so the filter tests the arm's ?a.
        pytest.param(
            "SELECT ?a WHERE { ?a a :Person { ?a a :Person FILTER EXISTS { ?a :knows ?x } } UNION { ?a :livesIn ?r } }",
            _DOCS,
            ["a", "a"],
            id="arm-binds-the-variable",
        ),
        # Unchanged: a FILTER in the group that binds ?a.
        pytest.param(
            "SELECT ?a WHERE { ?a a :Person FILTER EXISTS { ?a :knows ?x } }",
            _DOCS,
            ["a"],
            id="same-group",
        ),
        # Unchanged: inside an EXISTS the outer ?a is substituted (§18.6), a
        # constant in its nested group's FILTER too.
        pytest.param(
            "SELECT ?a WHERE { ?a a :Person FILTER EXISTS { ?a :knows ?x { FILTER(?a = ex:a) } } }",
            _DOCS,
            ["a"],
            id="exists-substitutes-into-nested-filter",
        ),
    ],
)
def test_filter_reads_only_its_groups_variables(
    query: str, docs: dict[str, list[dict[str, Any]]], expected: list[str]
) -> None:
    assert _rows(query, docs, _bundle(["knows", "livesIn"], [])) == Counter(f"a={a}" for a in expected)


# #204's data: :a knows :b and :c; :d knows :e; :e knows itself; :g knows :c.
# :b and :e have an email; everyone but :f has a name.
_DOCS_204: dict[str, list[dict[str, Any]]] = {
    "people": [
        _person("a", name="A"),
        _person("b", name="B", email="b@x.org"),
        _person("c", name="C"),
        _person("d", name="D"),
        _person("e", name="E", email="e@x.org"),
        _person("f"),
        _person("g", name="G"),
    ],
    "knows": [{"_from": f"people/{s}", "_to": f"people/{o}"} for s, o in ["ab", "ac", "de", "ee", "gc"]],
}


def test_union_arm_filter_does_not_read_outer_variable() -> None:
    """#204 case 2: ?x is unbound in the first arm, so ``?x = ex:c`` is an error for every row."""
    query = (
        "SELECT ?s ?x ?n ?e WHERE { ?s :knows ?x { ?s :name ?n FILTER(?x = ex:c) } UNION { ?s :email ?e } }"
    )
    assert _rows(query, _DOCS_204, _bundle(["knows"], ["name", "email"])) == Counter(["e=e@x.org,s=e,x=e"])


def test_minus_side_filter_does_not_read_outer_variable() -> None:
    """#209 shape 1: MINUS's right side is evaluated on its own, so ?x is unbound in its FILTER and nothing is removed."""
    docs = {
        "people": [
            _person("a", name="A"),
            _person("b", name="B"),
            _person("c", name="C"),
            _person("d", name="D"),
            _person("e", name="E"),
            _person("f"),
        ],
        "knows": [{"_from": f"people/{s}", "_to": f"people/{o}"} for s, o in ["ab", "ac", "de"]],
    }
    query = (
        "SELECT ?s ?x WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } MINUS { ?y :name ?n FILTER(?y = ?x) } }"
    )
    assert _rows(query, docs, _bundle(["knows"], ["name"])) == Counter(
        ["s=a,x=b", "s=a,x=c", "s=b", "s=c", "s=d,x=e", "s=e", "s=f"]
    )


# :a and :b in the named graph ex:g1, :c in ex:g2, :z in the default graph.
_DOCS_GRAPHS: dict[str, list[dict[str, Any]]] = {
    "people": [
        _person("a", _graph=f"{_E}g1"),
        _person("b", _graph=f"{_E}g1"),
        _person("c", _graph=f"{_E}g2"),
        _person("z"),
    ],
}


@pytest.mark.parametrize(
    "query",
    [
        # Control: a FILTER on ?g in the GRAPH's own group.
        pytest.param("SELECT ?g ?s WHERE { GRAPH ?g { ?s a :Person FILTER(?g = ex:g1) } }", id="graph-group"),
        # The same FILTER in a nested group: the active GRAPH variable stays visible.
        pytest.param(
            "SELECT ?g ?s WHERE { GRAPH ?g { ?s a :Person { FILTER(?g = ex:g1) } } }", id="nested-group"
        ),
    ],
)
def test_filter_reads_the_active_graph_variable(query: str) -> None:
    """The active GRAPH variable is kept in scope, as the OPTIONAL emitter keeps it (pyoxigraph agrees)."""
    assert _rows(query, _DOCS_GRAPHS, _bundle([], [])) == Counter(["g=g1,s=a", "g=g1,s=b"])
