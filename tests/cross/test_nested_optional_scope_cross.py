"""Cross-validation: a group with a nested OPTIONAL is evaluated on its own (#194).

SPARQL 1.1 §18.5 evaluates a group, OPTIONAL bodies included, on its own
and joins its solutions with the solutions around it by compatibility
(§18.3). The translator visits the group in sequence after the patterns
around it, so the group sees their bindings. That substitution gives the
same answer only for a variable the group binds in every solution. Take

    ?s a :Person ; :knows ?x
    OPTIONAL { ?s :name ?n OPTIONAL { ?x :email ?m } }

The second OPTIONAL's group binds ``?x`` only through its nested OPTIONAL,
so on its own ``?x`` ranges over every subject with an email (``:b`` and
``:e``). For ``(a, x = c)`` neither is compatible with ``c``, so the outer
row keeps ``?n`` unbound. The translator read the nested OPTIONAL against
the outer ``?x = c`` instead and bound ``?n = "A"``.

Every case runs the translated AQL through the in-memory interpreter on
three stores built from one Turtle source (a triples table, PG documents
with a dedicated edge collection, LPG documents with a typed generic edge
collection) and compares with pyoxigraph on the same facts.
"""

from __future__ import annotations

from typing import Any

import pytest
import rdflib

from arango_sparql.api import translate
from arango_sparql.errors import UnsupportedSparqlError
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.aql_interp import run_aql_subset
from tests.helpers.oxi import (
    assert_bindings_equal,
    drop_null_bindings,
    load_store_from_string,
    normalize_oxi_row,
    oxi_bindings,
)

oxi = pytest.importorskip("pyoxigraph", reason="pyoxigraph required for cross tests")

EX = "http://ex.org/"

# a knows b and c, d knows e, e knows itself; b and e have emails, c has
# none; f has nothing. g knows c and likes nobody (review of #197): a group
# whose ?x comes from ``OPTIONAL { ?s :likes ?x }`` leaves it unbound for g.
PEOPLE: dict[str, dict[str, Any]] = {
    "a": {"name": "A", "age": 30, "knows": ["b", "c"], "likes": ["b"]},
    "b": {"name": "B", "age": 40, "email": "b@x.org", "likes": ["c"]},
    "c": {"name": "C", "age": 20},
    "d": {"name": "D", "age": 50, "knows": ["e"]},
    "e": {"name": "E", "email": "e@x.org", "knows": ["e"]},
    "f": {},
    "g": {"name": "G", "knows": ["c"]},
}
EDGES = ("knows", "likes")


def _data_ttl() -> str:
    lines = [f"@prefix : <{EX}> ."]
    for key, props in PEOPLE.items():
        parts = [f":{key} a :Person"]
        for p, v in props.items():
            if p in EDGES:
                parts += [f":{p} :{t}" for t in v]
            else:
                parts.append(f":{p} {v}" if isinstance(v, int) else f':{p} "{v}"')
        lines.append(" ; ".join(parts) + " .")
    return "\n".join(lines)


DATA = _data_ttl()

_HEAD = f"""
@prefix : <{EX}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix phys: <https://arango.solutions/phys#> .
"""
_DATATYPE = """
:name a owl:DatatypeProperty ; rdfs:domain :Person ; phys:attributeName "name" .
:email a owl:DatatypeProperty ; rdfs:domain :Person ; phys:attributeName "email" .
:age a owl:DatatypeProperty ; rdfs:domain :Person ; phys:attributeName "age" .
"""

RPT_ONTOLOGY = _HEAD + ':Person a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "_triples" .\n'
PG_ONTOLOGY = (
    _HEAD
    + """
:Person a owl:Class ; phys:collectionName "Person" .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; rdfs:domain :Person ; rdfs:range :Person .
:likes a owl:ObjectProperty ; phys:edgeCollectionName "likes" ; rdfs:domain :Person ; rdfs:range :Person .
"""
    + _DATATYPE
)
LPG_ONTOLOGY = (
    _HEAD
    + """
:Person a owl:Class ; phys:collectionName "vertices" ; phys:mappingStyle "LABEL" ;
    phys:typeField "type" ; phys:typeValue "Person" .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "edges" ; phys:mappingStyle "GENERIC_WITH_TYPE" ;
    phys:typeField "type" ; phys:typeValue "knows" ; rdfs:domain :Person ; rdfs:range :Person .
:likes a owl:ObjectProperty ; phys:edgeCollectionName "edges" ; phys:mappingStyle "GENERIC_WITH_TYPE" ;
    phys:typeField "type" ; phys:typeValue "likes" ; rdfs:domain :Person ; rdfs:range :Person .
"""
    + _DATATYPE
)


def _rpt_docs() -> dict[str, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    for s, p, o in rdflib.Graph().parse(data=DATA, format="turtle"):
        row: dict[str, Any] = {"subject_uri": str(s), "predicate": str(p)}
        if isinstance(o, rdflib.URIRef):
            row["object_uri"] = str(o)
        else:
            row["object_value"] = o.toPython()
        rows.append(row)
    return {"_triples": rows}


def _vertex(collection: str, key: str, **extra: Any) -> dict[str, Any]:
    props = {p: v for p, v in PEOPLE[key].items() if p not in EDGES}
    return {"_id": f"{collection}/{key}", "_key": key, "_uri": EX + key, **props, **extra}


def _pg_docs() -> dict[str, list[dict[str, Any]]]:
    return {
        "Person": [_vertex("Person", k) for k in PEOPLE],
        **{
            edge: [
                {"_from": f"Person/{k}", "_to": f"Person/{t}"}
                for k, d in PEOPLE.items()
                for t in d.get(edge, [])
            ]
            for edge in EDGES
        },
    }


def _lpg_docs() -> dict[str, list[dict[str, Any]]]:
    return {
        "vertices": [_vertex("vertices", k, type="Person") for k in PEOPLE],
        "edges": [
            {"_from": f"vertices/{k}", "_to": f"vertices/{t}", "type": edge}
            for k, d in PEOPLE.items()
            for edge in EDGES
            for t in d.get(edge, [])
        ],
    }


MODELS = [
    pytest.param(RPT_ONTOLOGY, _rpt_docs, id="rpt"),
    pytest.param(PG_ONTOLOGY, _pg_docs, id="pg_dedicated_edge"),
    pytest.param(LPG_ONTOLOGY, _lpg_docs, id="lpg_generic_edge"),
]

CASES = [
    # The issue's query: ?x comes from an earlier OPTIONAL.
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
        "SELECT ?s ?x ?n ?m WHERE { ?s a :Person ; :knows ?x "
        "OPTIONAL { ?s :name ?n OPTIONAL { ?s :name ?n2 OPTIONAL { ?x :email ?m } } } }",
        id="two_levels",
    ),
    # The nested OPTIONAL's own FILTER reads the group's ?x, not the outer one.
    pytest.param(
        "SELECT ?s ?x ?n ?m WHERE { ?s a :Person ; :knows ?x "
        "OPTIONAL { ?s :name ?n OPTIONAL { ?x :email ?m FILTER(?x != :e) } } }",
        id="nested_condition_reads_group_value",
    ),
    pytest.param(
        "SELECT ?s ?x ?n ?m WHERE { ?s a :Person ; :knows ?x "
        'OPTIONAL { ?s :name ?n OPTIONAL { ?x :email ?m } FILTER(?n != "D") } }',
        id="outer_condition",
    ),
    pytest.param(
        "SELECT ?s ?x ?n ?m ?e WHERE { ?s a :Person ; :knows ?x "
        "{ ?s :name ?n OPTIONAL { ?x :email ?m } } UNION { ?s :email ?e } }",
        id="union_arm",
    ),
    # Not inside an OPTIONAL: a required group drops the incompatible row.
    pytest.param(
        "SELECT ?s ?x ?n ?m WHERE { ?s a :Person ; :knows ?x { ?s :name ?n OPTIONAL { ?x :email ?m } } }",
        id="required_group",
    ),
    pytest.param(
        "SELECT ?s ?x ?n ?m WHERE { ?s a :Person ; :knows ?x "
        "OPTIONAL { ?s :name ?n OPTIONAL { ?s :knows ?x . ?x :email ?m } } }",
        id="nested_rebinds_through_triple",
    ),
    # MINUS evaluates its right side on its own too: (a, c) stays.
    pytest.param(
        "SELECT ?s ?x WHERE { ?s a :Person ; :knows ?x MINUS { ?s :name ?n OPTIONAL { ?x :email ?m } } }",
        id="minus",
    ),
    pytest.param(
        "SELECT ?s (COUNT(?n) AS ?c) WHERE { ?s a :Person ; :knows ?x "
        "OPTIONAL { ?s :name ?n OPTIONAL { ?x :email ?m } } } GROUP BY ?s",
        id="count",
    ),
    # The group's ?x comes from an earlier OPTIONAL in the same group (review
    # of #197): g likes nobody, so the group binds ?x only through
    # ``?x :email ?m``, to b or e, neither compatible with g's c, and the
    # (g, c) row keeps ?n unbound. Main refused the re-bind; the first fix
    # read the second OPTIONAL against the outer ?x and bound ?n = "G".
    pytest.param(
        "SELECT ?s ?x ?n ?m WHERE { ?s :knows ?x "
        "OPTIONAL { ?s :name ?n OPTIONAL { ?s :likes ?x } OPTIONAL { ?x :email ?m } } }",
        id="earlier_optional_binds_x",
    ),
    pytest.param(
        "SELECT ?s ?x ?n ?m WHERE { ?s :knows ?x "
        "{ ?s :name ?n OPTIONAL { ?s :likes ?x } OPTIONAL { ?x :email ?m } } }",
        id="earlier_optional_binds_x_required_group",
    ),
    pytest.param(
        "SELECT ?s ?x ?n ?xn WHERE { ?s :knows ?x OPTIONAL { ?s :name ?n OPTIONAL { ?s :likes ?x } ?x :name ?xn } }",
        id="earlier_optional_then_required",
    ),
    # Control: the nested OPTIONAL's subject is bound inside its own group.
    pytest.param(
        "SELECT ?s ?x ?m WHERE { ?s a :Person OPTIONAL { ?s :knows ?x OPTIONAL { ?x :email ?m } } }",
        id="control_well_designed",
    ),
    # Control: EXISTS substitutes the outer row into its pattern (§18.6).
    pytest.param(
        "SELECT ?s ?x WHERE { ?s a :Person ; :knows ?x FILTER EXISTS { ?s :name ?n OPTIONAL { ?x :email ?m } } }",
        id="control_exists_substitutes",
    ),
]


def _run(
    ontology_ttl: str, docs_factory: Any, where: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    query = f"PREFIX : <{EX}> " + where
    result = translate(query, resolver=SchemaResolver.from_turtle(ontology_ttl))
    actual = [drop_null_bindings(r) for r in run_aql_subset(result.aql, result.bind_vars, docs_factory())]
    expected = [normalize_oxi_row(r) for r in oxi_bindings(load_store_from_string(DATA), query)]
    return expected, actual


# A MINUS right side is evaluated on its own too. On a triples table the
# group-scoped OPTIONAL runs there; on documents the probe lowers the first
# OPTIONAL inline (a conditional add), whose own value for ?x is not
# tracked, so the second is refused rather than guessed.
MINUS_EARLIER_OPTIONAL = "SELECT ?s ?x WHERE { ?s :knows ?x MINUS { ?s :name ?n OPTIONAL { ?s :likes ?x } OPTIONAL { ?x :email ?m } } }"


@pytest.mark.cross
def test_minus_whose_group_binds_x_through_an_earlier_optional_matches_oxigraph_on_triples() -> None:
    expected, actual = _run(RPT_ONTOLOGY, _rpt_docs, MINUS_EARLIER_OPTIONAL)
    assert {"s": EX + "g", "x": EX + "c"} in expected
    assert_bindings_equal(expected, actual)


@pytest.mark.cross
@pytest.mark.parametrize(
    "ontology_ttl", [PG_ONTOLOGY, LPG_ONTOLOGY], ids=["pg_dedicated_edge", "lpg_generic_edge"]
)
def test_minus_whose_group_binds_x_through_an_earlier_optional_is_refused_on_documents(
    ontology_ttl: str,
) -> None:
    with pytest.raises(UnsupportedSparqlError, match="binds in only some solutions"):
        translate(
            f"PREFIX : <{EX}> " + MINUS_EARLIER_OPTIONAL, resolver=SchemaResolver.from_turtle(ontology_ttl)
        )


# A required side that binds ?x in only some solutions through VALUES with
# UNDEF or a sub-select (review of #197): the value it gave ?x is not tracked,
# so the padding test has nothing to compare with, and the query is refused.
# Main and the first fix counted those columns as always bound and bound ?n
# for (a, c) and (g, c) instead of leaving it unbound.
UNTRACKED_CASES = [
    pytest.param(
        "SELECT ?s ?x ?n ?m WHERE { ?s :knows ?x OPTIONAL { ?s :name ?n VALUES ?x { UNDEF } OPTIONAL { ?x :email ?m } } }",
        id="values_undef",
    ),
    pytest.param(
        "SELECT ?s ?x ?n ?m WHERE { ?s :knows ?x "
        "OPTIONAL { ?s :name ?n VALUES ?x { :b UNDEF } OPTIONAL { ?x :email ?m } } }",
        id="values_some_undef",
    ),
    pytest.param(
        "SELECT ?s ?x ?n ?m WHERE { ?s :knows ?x "
        "OPTIONAL { ?s :name ?n VALUES (?s ?x) { (:g UNDEF) (:a :b) } OPTIONAL { ?x :email ?m } } }",
        id="values_pair_undef",
    ),
    pytest.param(
        "SELECT ?s ?x ?n ?m WHERE { ?s :knows ?x { ?s :name ?n VALUES ?x { UNDEF } OPTIONAL { ?x :email ?m } } }",
        id="values_undef_required_group",
    ),
    pytest.param(
        "SELECT ?s ?x ?n ?m WHERE { ?s :knows ?x OPTIONAL { ?s :name ?n "
        "{ SELECT ?s ?x WHERE { ?s :name ?nn OPTIONAL { ?s :likes ?x } } } OPTIONAL { ?x :email ?m } } }",
        id="subselect_optional_column",
    ),
]


@pytest.mark.cross
@pytest.mark.parametrize("ontology_ttl,docs_factory", MODELS)
@pytest.mark.parametrize("where", UNTRACKED_CASES)
def test_group_scoped_variable_from_values_undef_or_subselect_is_refused(
    ontology_ttl: str, docs_factory: Any, where: str
) -> None:
    with pytest.raises(UnsupportedSparqlError, match="binds in only some solutions"):
        translate(f"PREFIX : <{EX}> " + where, resolver=SchemaResolver.from_turtle(ontology_ttl))


@pytest.mark.cross
@pytest.mark.parametrize(
    "where",
    [
        # Control: a VALUES column every row binds is substituted as before and
        # matches pyoxigraph. (The sub-select control runs live: the in-memory
        # interpreter does not run a sub-select's COLLECT inside a subquery.)
        pytest.param(
            "SELECT ?s ?x ?n ?m WHERE { ?s :knows ?x OPTIONAL { ?s :name ?n VALUES ?x { :b :c } OPTIONAL { ?x :email ?m } } }",
            id="values_all_bound",
        ),
    ],
)
@pytest.mark.parametrize("ontology_ttl,docs_factory", MODELS)
def test_always_bound_values_or_subselect_column_matches_oxigraph(
    ontology_ttl: str, docs_factory: Any, where: str
) -> None:
    expected, actual = _run(ontology_ttl, docs_factory, where)
    assert_bindings_equal(expected, actual)


# The probe's disjoint-domain guard (§8.3.4) for a group-scoped variable,
# which the emitter records in the probe's sink: ?t shares nothing with the
# outer row, so only a ?t that likes the outer ?x removes it. d and e know e,
# whom nobody likes, so they stay; without the guard every row went (review
# of #197).
MINUS_SINK_CASES = [
    pytest.param(
        "SELECT ?s ?x WHERE { ?s :knows ?x MINUS { ?t :name ?n OPTIONAL { ?t :likes ?x } } }",
        id="unshared_subject_likes",
    ),
    # The OPTIONAL's body reads ?o as a subject too, so on documents it is the
    # group-scoped subquery, not the inline conditional add.
    pytest.param(
        "SELECT ?s ?o WHERE { ?s :knows ?o MINUS { ?t :name ?n OPTIONAL { ?t :likes ?o . ?o :name ?on } } }",
        id="unshared_subject_likes_named",
    ),
]


@pytest.mark.cross
@pytest.mark.parametrize("ontology_ttl,docs_factory", MODELS)
@pytest.mark.parametrize("where", MINUS_SINK_CASES)
def test_minus_group_scoped_overlap_guard_matches_oxigraph(
    ontology_ttl: str, docs_factory: Any, where: str
) -> None:
    expected, actual = _run(ontology_ttl, docs_factory, where)
    assert expected, "the guard keeps some rows; an empty answer would not discriminate"
    assert_bindings_equal(expected, actual)


@pytest.mark.cross
@pytest.mark.parametrize("ontology_ttl,docs_factory", MODELS)
@pytest.mark.parametrize("where", CASES)
def test_nested_optional_group_scope_matches_oxigraph(
    ontology_ttl: str, docs_factory: Any, where: str
) -> None:
    query = f"PREFIX : <{EX}> " + where
    result = translate(query, resolver=SchemaResolver.from_turtle(ontology_ttl))
    actual = [drop_null_bindings(r) for r in run_aql_subset(result.aql, result.bind_vars, docs_factory())]

    expected = [normalize_oxi_row(r) for r in oxi_bindings(load_store_from_string(DATA), query)]

    assert_bindings_equal(expected, actual)
