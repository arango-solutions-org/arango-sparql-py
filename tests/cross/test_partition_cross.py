"""Cross-validation for the federation entry point (CDF M5 WP-C2).

Two layers, both against pyoxigraph as the W3C ground truth:

1. **Seeded-partition parity** — a partition with ``seed_bindings``
   must produce exactly the bindings pyoxigraph produces for the same
   query with the same ``VALUES`` rows.
2. **Two-leg federation** — the M5 execution model in miniature:
   leg 1 selects canonical keys, its result rows (in SPARQL-JSON
   binding shape, exactly what an engine would hold) seed leg 2, and
   the engine-side hash-join of the two legs must equal pyoxigraph
   evaluating the *un-partitioned* conceptual query over the whole
   dataset. This is the property federation correctness rests on:
   partition + push down + join == evaluate whole.
"""

from __future__ import annotations

from typing import Any

import pytest

from arango_sparql.api import translate_partition
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.aql_interp import run_aql_subset
from tests.helpers.oxi import (
    assert_bindings_equal,
    load_store_from_string,
    normalize_oxi_row,
    oxi_bindings,
)

oxi = pytest.importorskip("pyoxigraph", reason="pyoxigraph required for cross tests")

pytestmark = pytest.mark.cross

ONTOLOGY_TTL = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .

:Person  a owl:Class ; phys:collectionName "Person" .
:Project a owl:Class ; phys:collectionName "Project" .
"""

DATA_TTL = """
@prefix : <http://ex.org/> .
:alice a :Person ; :name "Alice" ; :dept "eng" .
:bob   a :Person ; :name "Bob"   ; :dept "eng" .
:carol a :Person ; :name "Carol" ; :dept "ops" .

:p1 a :Project ; :title "Apollo"   ; :owner :alice .
:p2 a :Project ; :title "Beacon"   ; :owner :bob .
:p3 a :Project ; :title "Catalyst" ; :owner :alice .
:p4 a :Project ; :title "Orphan" .
"""

ARANGO_DOCS: dict[str, list[dict[str, Any]]] = {
    "Person": [
        {"_uri": "http://ex.org/alice", "name": "Alice", "dept": "eng"},
        {"_uri": "http://ex.org/bob", "name": "Bob", "dept": "eng"},
        {"_uri": "http://ex.org/carol", "name": "Carol", "dept": "ops"},
    ],
    "Project": [
        {"_uri": "http://ex.org/p1", "title": "Apollo", "owner": "http://ex.org/alice"},
        {"_uri": "http://ex.org/p2", "title": "Beacon", "owner": "http://ex.org/bob"},
        {"_uri": "http://ex.org/p3", "title": "Catalyst", "owner": "http://ex.org/alice"},
        {"_uri": "http://ex.org/p4", "title": "Orphan"},
    ],
}


@pytest.fixture(scope="module")
def store() -> Any:
    return load_store_from_string(DATA_TTL)


@pytest.fixture()
def resolver() -> SchemaResolver:
    return SchemaResolver.from_turtle(ONTOLOGY_TTL)


def test_seeded_partition_matches_oxigraph(store: Any, resolver: SchemaResolver) -> None:
    partition = "PREFIX : <http://ex.org/> SELECT ?name WHERE { ?p a :Person ; :name ?name }"
    seeds = [
        {"p": {"type": "uri", "value": "http://ex.org/alice"}},
        {"p": {"type": "uri", "value": "http://ex.org/carol"}},
    ]
    result = translate_partition(partition, resolver=resolver, canonical_keys=["p"], seed_bindings=seeds)
    actual = run_aql_subset(result.aql, result.bind_vars, ARANGO_DOCS)
    # Ground truth: the effective query (partition + VALUES) — exactly
    # the text recorded in provenance — evaluated by pyoxigraph. The
    # canonical-key column is OUR addition (pyoxigraph honours the
    # partition's own ``SELECT ?name``), so compare the declared
    # projection separately from the key column.
    expected = [normalize_oxi_row(row) for row in oxi_bindings(store, result.provenance.query_text)]
    assert_bindings_equal([{"name": row["name"]} for row in actual], expected)
    # The key column must carry exactly the surviving seeds' IRIs.
    assert sorted(row["p"] for row in actual) == [
        "http://ex.org/alice",
        "http://ex.org/carol",
    ]


def test_a_seed_row_with_undef_matches_any_value(store: Any, resolver: SchemaResolver) -> None:
    # A seed row an engine builds from an earlier leg that left one of its
    # columns unbound: the ``None`` cell travels as UNDEF, and an unbound
    # variable joins with any value (SPARQL §18.3).
    partition = "PREFIX : <http://ex.org/> SELECT ?p ?dept WHERE { ?p a :Person ; :dept ?dept }"
    seeds = [
        {"p": {"type": "uri", "value": "http://ex.org/alice"}, "dept": None},
        {"p": None, "dept": {"type": "literal", "value": "ops"}},
    ]
    result = translate_partition(partition, resolver=resolver, canonical_keys=["p"], seed_bindings=seeds)
    actual = run_aql_subset(result.aql, result.bind_vars, ARANGO_DOCS)
    expected = [normalize_oxi_row(row) for row in oxi_bindings(store, result.provenance.query_text)]
    assert_bindings_equal(actual, expected)
    assert sorted(row["p"] for row in actual) == ["http://ex.org/alice", "http://ex.org/carol"]


def test_two_leg_federation_equals_whole_query(store: Any, resolver: SchemaResolver) -> None:
    # Leg 1 — "which people are in eng?" — returns canonical keys.
    leg1 = translate_partition(
        'PREFIX : <http://ex.org/> SELECT ?p WHERE { ?p a :Person ; :dept "eng" }',
        resolver=resolver,
        canonical_keys=["p"],
    )
    leg1_rows = run_aql_subset(leg1.aql, leg1.bind_vars, ARANGO_DOCS)

    # The engine re-shapes leg 1's rows as SPARQL-JSON bindings — the
    # canonical key is a subject IRI per contract decision #2.
    seeds = [{"p": {"type": "uri", "value": row[leg1.canonical_key_columns["p"]]}} for row in leg1_rows]

    # Leg 2 — "titles of projects owned by ?p" — seeded by leg 1.
    leg2 = translate_partition(
        "PREFIX : <http://ex.org/> SELECT ?title ?p WHERE { ?prj a :Project ; :owner ?p ; :title ?title }",
        resolver=SchemaResolver.from_turtle(ONTOLOGY_TTL),
        canonical_keys=["p"],
        seed_bindings=seeds,
    )
    leg2_rows = run_aql_subset(leg2.aql, leg2.bind_vars, ARANGO_DOCS)

    # Engine-side hash-join on the canonical key column.
    leg1_keys = {row["p"] for row in leg1_rows}
    joined = [{"title": row["title"], "p": row["p"]} for row in leg2_rows if row["p"] in leg1_keys]

    # Ground truth: pyoxigraph evaluates the un-partitioned query.
    whole = (
        "PREFIX : <http://ex.org/> SELECT ?title ?p WHERE { "
        '?p a :Person ; :dept "eng" . '
        "?prj a :Project ; :owner ?p ; :title ?title }"
    )
    expected = [normalize_oxi_row(row) for row in oxi_bindings(store, whole)]
    assert_bindings_equal(joined, expected)
    # Sanity: the pushdown actually constrained leg 2 — Carol's (ops)
    # projects would be absent even if she had any, and the seed rows
    # travelled as bind data, not inline text.
    assert leg2.bind_vars["_p1_values"] == [
        {"p": "http://ex.org/alice"},
        {"p": "http://ex.org/bob"},
    ]
