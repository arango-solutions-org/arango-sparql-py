"""Cross-validation: LIMIT / OFFSET on CONSTRUCT and ASK (issue #164).

The modifiers cut the WHERE solutions before the template is instantiated
(SPARQL 1.1 §16.2), so with ``ORDER BY`` they decide which people the graph or
the boolean is built from. The same people go into pyoxigraph as RDF and into
the interpreter as documents, and the triples (CONSTRUCT) or the answer (ASK)
must agree.

The interpreter reads a ``RETURN {…}`` row and a plain sub-query, not the
form's wrapper, so the wrapper is taken off first: each object of a
template's ``RETURN [{…}, {…}]`` is run as its own ``RETURN {…}``, and ASK's
``RETURN LENGTH(…) > 0`` runs its inner query and tests for a row.
"""

from __future__ import annotations

from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.aql_interp import run_aql_subset

oxi = pytest.importorskip("pyoxigraph", reason="pyoxigraph required for cross tests")

EX = "http://ex.org/"
NAMES = ["Alice", "Bob", "Carol", "Dave", "Eve"]

ONTOLOGY_TTL = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
:Person a owl:Class ; phys:collectionName "Person" .
:name a owl:DatatypeProperty .
"""

DATA_TTL = "@prefix : <http://ex.org/> .\n" + "".join(f':{n} a :Person ; :name "{n}" .\n' for n in NAMES)

DOCS: dict[str, list[dict[str, Any]]] = {"Person": [{"_uri": EX + n, "name": n} for n in NAMES]}

P = "PREFIX : <http://ex.org/>\n"

CONSTRUCT_CASES = [
    pytest.param(
        "CONSTRUCT { ?s :label ?n } WHERE { ?s a :Person ; :name ?n } ORDER BY DESC(?n) OFFSET 1 LIMIT 2",
        id="order_by_desc_offset_limit",
    ),
    pytest.param(
        "CONSTRUCT { ?s :label ?n } WHERE { ?s a :Person ; :name ?n } ORDER BY ?n LIMIT 2",
        id="order_by_limit",
    ),
    pytest.param(
        "CONSTRUCT { ?s :label ?n } WHERE { ?s a :Person ; :name ?n } ORDER BY ?n OFFSET 10 LIMIT 3",
        id="offset_past_the_end",
    ),
    pytest.param(
        "CONSTRUCT WHERE { ?s a :Person ; :name ?n } ORDER BY DESC(?n) LIMIT 2",
        id="short_form_order_by_desc_limit",
    ),
]

ASK_CASES = [
    pytest.param("ASK { ?s a :Person } OFFSET 4 LIMIT 5", id="offset_leaves_a_row"),
    pytest.param("ASK { ?s a :Person } OFFSET 5 LIMIT 5", id="offset_leaves_no_row"),
    pytest.param("ASK { ?s a :Person } LIMIT 0", id="limit_zero"),
    pytest.param("ASK { ?s a :Person } LIMIT 3", id="limit_only"),
]


@pytest.fixture(scope="module")
def oxi_store() -> Any:
    store = oxi.Store()
    store.load(DATA_TTL.encode("utf-8"), oxi.RdfFormat.TURTLE)
    return store


def _translate(sparql: str) -> Any:
    return translate(P + sparql, resolver=SchemaResolver.from_turtle(ONTOLOGY_TTL))


@pytest.mark.cross
@pytest.mark.parametrize("sparql", CONSTRUCT_CASES)
def test_construct_slice_matches_oxigraph(oxi_store: Any, sparql: str) -> None:
    result = _translate(sparql)
    head, _, template = result.aql.rpartition("\nRETURN ")
    assert template.startswith("[{") and template.endswith("}]"), template
    actual = {
        (r["subject"], r["predicate"], str(r["object"]))
        for triple in template[2:-2].split("}, {")
        for r in run_aql_subset(f"{head}\nRETURN {{{triple}}}", result.bind_vars, DOCS)
    }
    expected = {(t.subject.value, t.predicate.value, t.object.value) for t in oxi_store.query(P + sparql)}
    assert actual == expected


@pytest.mark.cross
@pytest.mark.parametrize("sparql", ASK_CASES)
def test_ask_slice_matches_oxigraph(oxi_store: Any, sparql: str) -> None:
    result = _translate(sparql)
    lines = result.aql.strip().splitlines()
    assert lines[0] == "RETURN LENGTH(" and lines[-1] == ") > 0", result.aql
    actual = bool(run_aql_subset("\n".join(lines[1:-1]), result.bind_vars, DOCS))
    assert actual is bool(oxi_store.query(P + sparql))
