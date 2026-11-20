"""Cross-validation for RPT-native cross-subject ``OPTIONAL``
(ADR-0002 Problem 1, Option A).

A *cross-subject* OPTIONAL binds its subject only as a value (the object
of a prior triple), never as a document. On RPT it compiles to a
``[null]``-padded left-join scan of the triples table
(:mod:`arango_sparql.translate.optional_crosssubject`). This module
proves that emission is binding-equivalent to the W3C ground truth by
running the translated AQL through the in-memory interpreter and
comparing against pyoxigraph over the *same* triples.

The dataset deliberately exercises the three left-join cases:

* **fan-out** — ``:bob`` has two triples, so ``OPTIONAL { ?o ?p2 ?o2 }``
  produces two rows for the ``(alice, bob)`` solution.
* **single match** — ``:carol`` has exactly one triple.
* **no match → null pad** — ``:erin`` has no triples, so the
  ``(dave, erin)`` solution survives with ``?p2`` / ``?o2`` unbound
  (the property that distinguishes a LEFT join from an INNER join).

Comparison is order-insensitive: ``?o2`` ranges over both IRIs and
literals, and SPARQL ``ORDER BY`` over mixed term types is not the
property under test here (it is covered for single-type keys in the
MINUS cross suite).
"""

from __future__ import annotations

from typing import Any

import pytest
import rdflib

from arango_sparql.api import translate
from arango_sparql.translate.mapping import MappingBundle, MappingSource
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

# Single source of truth: both the pyoxigraph store and the RPT triples
# document store derive from this TTL, so they describe identical facts.
DATA = """
@prefix : <http://ex.org/> .
:alice a :Person ; :knows :bob , :carol .
:dave  a :Person ; :knows :erin .
:bob   :email "bob@x.org" ; :city "NYC" .
:carol a :Person .
"""

# RPT ontology — every column override is the default, listed explicitly
# so the doc store below and the translator agree on the schema.
RPT_ONTOLOGY = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
:Person a owl:Class ;
    phys:mappingStyle "RPT" ;
    phys:triplesCollection "_triples" ;
    phys:subjectColumn "subject_uri" ;
    phys:predicateColumn "predicate" ;
    phys:objectUriColumn "object_uri" ;
    phys:objectValueColumn "object_value" .
"""


def _rpt_resolver() -> SchemaResolver:
    bundle = MappingBundle(
        physical_mapping={"entities": {}, "relationships": {}},
        owl_turtle=RPT_ONTOLOGY,
        source=MappingSource(kind="manual"),
    )
    return SchemaResolver.from_mapping_bundle(bundle)


def _triples_store() -> dict[str, list[dict[str, Any]]]:
    """One ``_triples`` row per RDF triple in :data:`DATA`.

    IRI objects land in ``object_uri``, literals in ``object_value``
    (the legacy Foxx RPT layout); the absent column is omitted, which
    the interpreter reads as null — exactly what
    ``COALESCE(object_uri, object_value)`` recovers.
    """
    graph = rdflib.Graph()
    graph.parse(data=DATA, format="turtle")
    rows: list[dict[str, Any]] = []
    for s, p, o in graph:
        row: dict[str, Any] = {"subject_uri": str(s), "predicate": str(p)}
        if isinstance(o, rdflib.URIRef):
            row["object_uri"] = str(o)
        else:
            row["object_value"] = o.toPython()
        rows.append(row)
    return {"_triples": rows}


CASES = [
    pytest.param(
        "SELECT ?s ?o ?p2 ?o2 WHERE { ?s a :Person ; :knows ?o . OPTIONAL { ?o ?p2 ?o2 } }",
        id="variable_predicate",
    ),
    pytest.param(
        "SELECT ?s ?o ?email WHERE { ?s a :Person ; :knows ?o . OPTIONAL { ?o :email ?email } }",
        id="fixed_predicate",
    ),
    # Same-subject: the subject was opened by the type pattern as a triples
    # row, and the OPTIONAL read ``doc.knows`` off it (always unbound; review
    # of #189). It now runs as a correlated subquery over the table.
    pytest.param(
        "SELECT ?s ?o WHERE { ?s a :Person . OPTIONAL { ?s :knows ?o } }",
        id="same_subject",
    ),
    pytest.param(
        "SELECT ?s ?o ?email WHERE { ?s a :Person . OPTIONAL { ?s :knows ?o . ?o :email ?email } }",
        id="same_subject_two_triples",
    ),
    # Inside MINUS an unshared OPTIONAL variable, filtered or not, cannot
    # change the result (a refusal of the filtered case was over-broad).
    pytest.param(
        'SELECT ?s WHERE { ?s a :Person MINUS { ?t a :Person ; :knows ?s OPTIONAL { ?t :email ?m FILTER(?m = "bob@x.org") } } }',
        id="minus_unshared_filtered",
    ),
    # #190: the first OPTIONAL leaves ?x unbound for :carol (she knows nobody);
    # an unbound variable is compatible with any value (SPARQL 1.1 §18.3), so
    # the next pattern on ?x joins every subject and ?x takes it. The join
    # used to require ``subject_uri == null`` and dropped the row.
    pytest.param(
        "SELECT ?s ?x ?m WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } OPTIONAL { ?x :email ?m } }",
        id="chained_optional_unbound_join",
    ),
    pytest.param(
        "SELECT ?s ?x ?m ?c WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } OPTIONAL { ?x :email ?m . ?x :city ?c } }",
        id="chained_optional_two_triples",
    ),
    pytest.param(
        "SELECT ?s ?x ?m WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } ?x :email ?m }",
        id="required_join_after_optional",
    ),
    pytest.param(
        "SELECT ?s ?x WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } FILTER EXISTS { ?x :email ?m } }",
        id="exists_on_optional_unbound",
    ),
    pytest.param(
        "SELECT ?s ?x WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } MINUS { ?x :email ?m } }",
        id="minus_on_optional_unbound",
    ),
    # A type pattern on the possibly-unbound variable joins every member.
    pytest.param(
        "SELECT ?s ?x ?y WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } OPTIONAL { ?x a :Person . ?x :knows ?y } }",
        id="type_pattern_on_optional_unbound",
    ),
    pytest.param(
        "SELECT ?s ?x WHERE { ?s a :Person OPTIONAL { ?s :email ?x } ?x a :Person }",
        id="required_type_pattern_after_optional",
    ),
    # A UNION arm that fills the possibly-unbound variable carries it out;
    # the arm joined it but the solution kept ?x unbound (review of #192).
    pytest.param(
        "SELECT ?s ?x ?m ?n WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } { ?x :email ?m } UNION { ?x :city ?n } }",
        id="union_arms_fill_optional_unbound",
    ),
    pytest.param(
        "SELECT ?s ?x ?m ?n WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } { ?x :email ?m } UNION { ?s :knows ?n } }",
        id="union_one_arm_fills",
    ),
]


@pytest.mark.cross
@pytest.mark.parametrize("where", CASES)
def test_rpt_cross_subject_optional_matches_oxigraph(where: str) -> None:
    query = "PREFIX : <http://ex.org/> " + where

    result = translate(query, resolver=_rpt_resolver())
    actual = [drop_null_bindings(r) for r in run_aql_subset(result.aql, result.bind_vars, _triples_store())]

    store = load_store_from_string(DATA)
    expected = [normalize_oxi_row(r) for r in oxi_bindings(store, query)]

    assert_bindings_equal(expected, actual)
