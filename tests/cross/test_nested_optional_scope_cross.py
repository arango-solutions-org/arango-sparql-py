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

# a knows b and c, d knows e; b and e have emails, c has none; f has nothing.
PEOPLE: dict[str, dict[str, Any]] = {
    "a": {"name": "A", "knows": ["b", "c"]},
    "b": {"name": "B", "email": "b@x.org"},
    "c": {"name": "C"},
    "d": {"name": "D", "knows": ["e"]},
    "e": {"name": "E", "email": "e@x.org"},
    "f": {},
}


def _data_ttl() -> str:
    lines = [f"@prefix : <{EX}> ."]
    for key, props in PEOPLE.items():
        parts = [f":{key} a :Person"]
        parts += [f':{p} "{v}"' for p, v in props.items() if p != "knows"]
        parts += [f":knows :{t}" for t in props.get("knows", [])]
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
"""

RPT_ONTOLOGY = _HEAD + ':Person a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "_triples" .\n'
PG_ONTOLOGY = (
    _HEAD
    + """
:Person a owl:Class ; phys:collectionName "Person" .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; rdfs:domain :Person ; rdfs:range :Person .
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
    props = {p: v for p, v in PEOPLE[key].items() if p != "knows"}
    return {"_id": f"{collection}/{key}", "_key": key, "_uri": EX + key, **props, **extra}


def _pg_docs() -> dict[str, list[dict[str, Any]]]:
    return {
        "Person": [_vertex("Person", k) for k in PEOPLE],
        "knows": [
            {"_from": f"Person/{k}", "_to": f"Person/{t}"}
            for k, d in PEOPLE.items()
            for t in d.get("knows", [])
        ],
    }


def _lpg_docs() -> dict[str, list[dict[str, Any]]]:
    return {
        "vertices": [_vertex("vertices", k, type="Person") for k in PEOPLE],
        "edges": [
            {"_from": f"vertices/{k}", "_to": f"vertices/{t}", "type": "knows"}
            for k, d in PEOPLE.items()
            for t in d.get("knows", [])
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
