"""Schema detection against a live ArangoDB — the ``schema_live`` tier (PRD §13.1).

The unit tier (``tests/schema/``) drives the detector through a duck-typed
mock database. That proves the rules, but not that python-arango and a real
server hand the detector what the mock does — nor that the analyzer, a
separate package, still returns what this repo assumes. This tier seeds real
databases for each physical model PRD §6.1 names (PG, LPG, RPT, hybrid, and
all three at once) and asserts:

* **Classification** — ``classify_schema`` names the model (§6.3.1 step 6).
* **Per-collection style** — every collection is detected with the style its
  schema fixture declares, so the unit corpus and the live tier cannot drift
  apart (§3.5, §13.3).
* **Endpoint inference** — relationship domain/range resolved per document,
  including the PG -> LPG cross-collection case, and a polymorphic edge left
  ``"Any"`` rather than guessed (§6.3.1 step 5).
* **Bundle integrity and queryability** — every endpoint names an entity the
  bundle declares, and every detected entity and edge relationship
  translates to AQL that executes and returns the seeded rows. A bundle
  that classifies correctly but cannot be queried is not a working mapping.
* **Enrichment on the analyzer path** — the RPT overlay runs even though the
  analyzer knows only PG/LPG (§6.3.2 step 2).
* **Live fingerprints** — row churn moves the counts fingerprint but not the
  shape fingerprint; a new collection moves the shape (§6.3.3).
* **Named-graph scope** — ``graph_name`` down-selects to that graph's
  collections (§6.3.2).
* **Service collections stay out** — this service's and arango-cypher-py's
  cache and examples collections never become classes, shard families,
  statistics or OWL, and writing them does not move the fingerprints (§6.3.3).

Every test runs both acquisition strategies where both apply: ``heuristic``
(this repo's detector) and ``auto`` (the analyzer, with this repo's
enrichment layered on). The analyzer is forced onto its deterministic
baseline: with no provider named it infers one from whichever API key is in
the environment, which would make this suite non-deterministic and billable.

Each model gets its own database, created and dropped by its fixture —
detection scans every collection in a database, so models cannot share one.

Run with::

    RUN_INTEGRATION=1 .venv/bin/pytest -m schema_live
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest

from arango_sparql.api import translate
from arango_sparql.errors import SchemaResolutionError
from arango_sparql.schema.acquire import (
    acquire_mapping_bundle,
    analyzer_available,
    db_counts_fingerprint,
    db_shape_fingerprint,
    service_collections,
)
from arango_sparql.schema.detect import classify_schema
from arango_sparql.service.mapping import _resolver_from_request
from arango_sparql.translate.mapping import MappingBundle
from arango_sparql.translate.resolver import SchemaResolver
from tests.integration.conftest import (
    DEFAULT_ARANGO_PASSWORD,
    DEFAULT_ARANGO_URL,
    DEFAULT_ARANGO_USER,
    arangodb_reachable,
    integration_enabled,
    try_boot_arangodb_via_compose,
)
from tests.integration.schema_live_seeds import NS as SEED_NS
from tests.integration.schema_live_seeds import SEEDS

pytestmark = [pytest.mark.integration, pytest.mark.schema_live]

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "schema" / "fixtures"
_DB_PREFIX = "sparql_schema_live_"

#: Environment that would let the analyzer pick an LLM provider.
_LLM_ENV = (
    "SCHEMA_ANALYZER_PROVIDER",
    "LLM_PROVIDER",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
)

EXPECTED_MODEL = {
    "pg": "pg",
    "lpg": "lpg",
    "hybrid": "hybrid",
    "rpt": "rpt",
    "rpt_pg_lpg_hybrid": "hybrid",
}

#: Detected endpoints per relationship, as physical identities (see
#: :func:`_identity`) so the table holds for both strategies, which key
#: entities differently. Keyed by (edge collection or RPT predicate, type value).
EXPECTED_ENDPOINTS: dict[str, dict[tuple[str, str | None], tuple[str, str]]] = {
    "pg": {("follows", None): ("users", "users")},
    "lpg": {("edges", "FOLLOWS"): ("User", "User"), ("edges", "LIKES"): ("User", "Doc")},
    # LIKES crosses PG -> LPG: resolved per document on the LPG side.
    "hybrid": {("edges", "FOLLOWS"): ("users", "users"), ("edges", "LIKES"): ("users", "Doc")},
    # RPT object properties are typed from rdf:type rows, not _from/_to.
    "rpt": {("AUTHORED", None): ("Person", "Doc"), ("KNOWS", None): ("Person", "Person")},
    "rpt_pg_lpg_hybrid": {
        ("authored", None): ("persons", "Doc"),
        ("edges", "ANNOTATED_BY"): ("Note", "Doc"),
        ("TAGGED_WITH", None): ("Doc", "Tag"),
    },
}

STRATEGIES = ("heuristic", "auto")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def arango_client() -> Iterator[Any]:
    if not integration_enabled():
        pytest.skip("set RUN_INTEGRATION=1 to enable integration tests")
    if not arangodb_reachable() and not try_boot_arangodb_via_compose():
        pytest.skip(f"ArangoDB at {DEFAULT_ARANGO_URL} is unreachable and could not be booted")
    from arango import ArangoClient

    client = ArangoClient(hosts=DEFAULT_ARANGO_URL.rstrip("/"))
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="module", autouse=True)
def _deterministic_analyzer() -> Iterator[None]:
    with pytest.MonkeyPatch.context() as mp:
        for name in _LLM_ENV:
            mp.delenv(name, raising=False)
        yield


def _fresh_db(client: Any, name: str) -> Any:
    sys_db = client.db("_system", username=DEFAULT_ARANGO_USER, password=DEFAULT_ARANGO_PASSWORD)
    if sys_db.has_database(name):
        sys_db.delete_database(name)
    sys_db.create_database(name)
    return client.db(name, username=DEFAULT_ARANGO_USER, password=DEFAULT_ARANGO_PASSWORD)


def _drop_db(client: Any, name: str) -> None:
    sys_db = client.db("_system", username=DEFAULT_ARANGO_USER, password=DEFAULT_ARANGO_PASSWORD)
    if sys_db.has_database(name):
        sys_db.delete_database(name)


@pytest.fixture(scope="module", params=sorted(SEEDS))
def seeded(request: pytest.FixtureRequest, arango_client: Any) -> Iterator[tuple[str, Any]]:
    """``(model, db)`` for one seeded physical model, dropped on teardown."""
    model: str = request.param
    name = f"{_DB_PREFIX}{model}"
    db = _fresh_db(arango_client, name)
    try:
        SEEDS[model](db)
        yield model, db
    finally:
        _drop_db(arango_client, name)


@pytest.fixture
def scratch_db(request: pytest.FixtureRequest, arango_client: Any) -> Iterator[Any]:
    """A per-test database for tests that mutate schema."""
    name = f"{_DB_PREFIX}scratch_{request.node.name}".replace("[", "_").replace("]", "")[:60]
    db = _fresh_db(arango_client, name)
    try:
        yield db
    finally:
        _drop_db(arango_client, name)


def _acquire(db: Any, strategy: str, **kwargs: Any) -> MappingBundle:
    if strategy == "auto" and not analyzer_available():
        pytest.skip("arangodb-schema-analyzer not installed; the auto strategy would not run it")
    return acquire_mapping_bundle(db, strategy=strategy, **kwargs)


# ---------------------------------------------------------------------------
# Projections — compare bundles by physical meaning, not by producer naming
# ---------------------------------------------------------------------------


def _load_fixture(model: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / f"{model}.export.json").read_text(encoding="utf-8"))


def _entity_styles(physical: dict[str, Any]) -> set[tuple[str, str, str | None]]:
    """``{(collection, style, typeValue)}`` — one row per physical entity.

    RPT entities collapse to their triples table: the fixtures key them per
    class while the detector keys them per collection, but both describe the
    same physical fact.
    """
    out = set()
    for spec in (physical.get("entities") or {}).values():
        style = spec.get("style")
        if style == "RPT":
            out.add((spec.get("triplesCollection") or "_triples", "RPT", None))
        else:
            out.add((spec.get("collectionName"), style, spec.get("typeValue")))
    return out


def _relationship_key(name: str, spec: dict[str, Any]) -> tuple[str, str | None]:
    if spec.get("style") == "RPT_EDGE":
        return name, None
    edge = spec.get("edgeCollectionName") or spec.get("collectionName")
    return edge, spec.get("typeValue")


def _relationship_styles(physical: dict[str, Any]) -> set[tuple[tuple[str, str | None], str]]:
    return {
        (_relationship_key(name, spec), spec.get("style"))
        for name, spec in (physical.get("relationships") or {}).items()
    }


def _identity(bundle: MappingBundle, entity: str) -> str:
    """An endpoint's physical identity: a PG entity's collection, an LPG
    entity's type value, otherwise the name itself (RPT classes, ``Any``)."""
    spec = (bundle.physical_mapping.get("entities") or {}).get(entity)
    if not isinstance(spec, dict):
        return entity
    if spec.get("style") == "COLLECTION":
        return spec.get("collectionName") or entity
    if spec.get("style") == "LABEL":
        return spec.get("typeValue") or entity
    return entity


def _iri(label: str) -> str:
    """The resolver's synthetic IRI for a bundle label (PRD §6.3.2)."""
    return f"urn:arango-sparql:concept#{quote(label, safe='')}"


def _run(db: Any, bundle: MappingBundle, sparql: str) -> list[Any]:
    result = translate(sparql, resolver=SchemaResolver.from_mapping_bundle(bundle))
    return list(db.aql.execute(result.aql, bind_vars=result.bind_vars))


# ---------------------------------------------------------------------------
# Classification and per-collection style (§6.3.1, §3.5)
# ---------------------------------------------------------------------------


def test_classification_names_the_model(seeded: tuple[str, Any]) -> None:
    model, db = seeded

    assert classify_schema(db) == EXPECTED_MODEL[model]


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_entity_styles_match_the_fixture(seeded: tuple[str, Any], strategy: str) -> None:
    """Every collection is detected with the style its fixture declares."""
    model, db = seeded

    detected = _entity_styles(_acquire(db, strategy).physical_mapping)

    assert detected == _entity_styles(_load_fixture(model)["physicalMapping"])


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_relationship_styles_match_the_fixture(seeded: tuple[str, Any], strategy: str) -> None:
    model, db = seeded

    detected = _relationship_styles(_acquire(db, strategy).physical_mapping)

    assert detected == _relationship_styles(_load_fixture(model)["physicalMapping"])


# ---------------------------------------------------------------------------
# Endpoint inference (§6.3.1 step 5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_endpoints_are_inferred_from_the_data(seeded: tuple[str, Any], strategy: str) -> None:
    model, db = seeded
    bundle = _acquire(db, strategy)

    detected = {
        _relationship_key(name, spec): (
            _identity(bundle, spec.get("fromEntity", "Any")),
            _identity(bundle, spec.get("toEntity", "Any")),
        )
        for name, spec in (bundle.physical_mapping.get("relationships") or {}).items()
    }

    assert detected == EXPECTED_ENDPOINTS[model]


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_edge_endpoints_name_entities_the_bundle_declares(seeded: tuple[str, Any], strategy: str) -> None:
    """An endpoint naming an undeclared entity makes the relationship unqueryable.

    The analyzer keys ``persons`` as ``Person``; endpoint enrichment used to
    fill the collection name, producing ``FOLLOWS: users -> users`` beside an
    entity keyed ``User``. RPT_EDGE is excluded: its endpoints are RDF class
    names, which the detector does not declare as entities (PRD §6.3.2 keys
    RPT entities per triples collection).
    """
    _model, db = seeded
    bundle = _acquire(db, strategy)
    entities = set(bundle.physical_mapping.get("entities") or {})

    dangling = [
        f"{name}.{side}={spec[side]!r}"
        for name, spec in (bundle.physical_mapping.get("relationships") or {}).items()
        if spec.get("style") != "RPT_EDGE"
        for side in ("fromEntity", "toEntity")
        if spec.get(side, "Any") not in entities | {"Any"}
    ]

    assert not dangling, f"endpoints name undeclared entities: {dangling}"


def test_a_polymorphic_edge_stays_any(scratch_db: Any) -> None:
    """Never guess a majority: an edge joining several types keeps ``"Any"``."""
    users = scratch_db.create_collection("vertices")
    u = [users.insert({"type": "User", "n": i})["_id"] for i in range(2)]
    d = [users.insert({"type": "Doc", "n": i})["_id"] for i in range(2)]
    edges = scratch_db.create_collection("edges", edge=True)
    edges.insert_many(
        [
            {"_from": u[0], "_to": d[0], "type": "LINKS"},
            {"_from": d[1], "_to": u[1], "type": "LINKS"},
        ]
    )

    links = acquire_mapping_bundle(scratch_db, strategy="heuristic").physical_mapping["relationships"][
        "LINKS"
    ]

    assert (links["fromEntity"], links["toEntity"]) == ("Any", "Any")


# ---------------------------------------------------------------------------
# Queryability — the bundle must actually drive translation (§6.2)
# ---------------------------------------------------------------------------


def _expected_entity_rows(db: Any, spec: dict[str, Any]) -> int:
    if spec["style"] == "LABEL":
        cursor = db.aql.execute(
            "RETURN COUNT(FOR d IN @@c FILTER d[@f] == @v RETURN 1)",
            bind_vars={"@c": spec["collectionName"], "f": spec["typeField"], "v": spec["typeValue"]},
        )
        return next(iter(cursor))
    return db.collection(spec["collectionName"]).count()


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_every_pg_and_lpg_entity_returns_its_seeded_rows(seeded: tuple[str, Any], strategy: str) -> None:
    """RPT entities are excluded: a detected triple store has no classes of its
    own (PRD §6.3.2), so its generated IRI is refused — see
    :func:`test_a_detected_triple_store_is_queried_by_class_only_through_an_ontology`.
    """
    _model, db = seeded
    bundle = _acquire(db, strategy)
    entities = {
        k: v for k, v in (bundle.physical_mapping.get("entities") or {}).items() if v.get("style") != "RPT"
    }

    counts = {k: len(_run(db, bundle, f"SELECT ?s WHERE {{ ?s a <{_iri(k)}> }}")) for k in entities}

    assert counts == {k: _expected_entity_rows(db, v) for k, v in entities.items()}


_RPT_ONTOLOGY = f"""
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
<{SEED_NS}Person> a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "_triples" .
"""


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_a_detected_triple_store_is_queried_by_class_only_through_an_ontology(
    scratch_db: Any, strategy: str
) -> None:
    """A triple store detected without an ontology has no classes (PRD §6.3.2).

    Its generated class and predicate IRIs are refused rather than run: no row
    stores them, so they could only answer nothing, and they used to return 0
    rows with no error. The data's own class IRI is refused with the table
    named; once an ontology declares it, it returns every subject the seed
    typed with it, on the library and the service resolver alike.
    """
    SEEDS["rpt"](scratch_db)
    bundle = _acquire(scratch_db, strategy)
    person = f"{SEED_NS}Person"
    people = sorted(f"{SEED_NS}person{i}" for i in range(3))

    for generated in (
        f"SELECT ?s WHERE {{ ?s a <{_iri('_triples')}> }}",
        f"SELECT ?s ?o WHERE {{ ?s <{_iri('KNOWS')}> ?o }}",
    ):
        with pytest.raises(SchemaResolutionError, match="generated from the mapping"):
            _run(scratch_db, bundle, generated)
    for resolver in (
        SchemaResolver.from_mapping_bundle(bundle),
        _resolver_from_request(SimpleNamespace(ontology_ttl=None, mapping=None), analyzer_bundle=bundle),
    ):
        with pytest.raises(SchemaResolutionError, match="keeps RDF triples in '_triples'"):
            translate(f"SELECT ?s WHERE {{ ?s a <{person}> }}", resolver=resolver)

    declared = _run(
        scratch_db, replace(bundle, owl_turtle=_RPT_ONTOLOGY), f"SELECT ?s WHERE {{ ?s a <{person}> }}"
    )
    service = _resolver_from_request(
        SimpleNamespace(ontology_ttl=_RPT_ONTOLOGY, mapping=None), analyzer_bundle=bundle
    )
    result = translate(f"SELECT ?s WHERE {{ ?s a <{person}> }}", resolver=service)
    served = list(scratch_db.aql.execute(result.aql, bind_vars=result.bind_vars))

    assert sorted(r["s"] for r in declared) == people
    assert sorted(r["s"] for r in served) == people


def _expected_edge_rows(db: Any, spec: dict[str, Any]) -> int:
    edge = spec["edgeCollectionName"]
    if spec["style"] == "GENERIC_WITH_TYPE":
        cursor = db.aql.execute(
            "RETURN COUNT(FOR e IN @@c FILTER e[@f] == @v RETURN 1)",
            bind_vars={"@c": edge, "f": spec["typeField"], "v": spec["typeValue"]},
        )
        return next(iter(cursor))
    return db.collection(edge).count()


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_every_edge_relationship_returns_its_seeded_rows(seeded: tuple[str, Any], strategy: str) -> None:
    """Each relationship queried from its detected domain returns every edge.

    This is where a dangling endpoint surfaces as a user-visible failure: the
    domain class does not resolve, and the relationship cannot be queried.
    """
    _model, db = seeded
    bundle = _acquire(db, strategy)
    edges = {
        k: v
        for k, v in (bundle.physical_mapping.get("relationships") or {}).items()
        if v.get("style") in ("DEDICATED_COLLECTION", "GENERIC_WITH_TYPE")
    }
    if not edges:
        pytest.skip("no edge-collection relationships in this model")

    counts = {
        k: len(_run(db, bundle, f"SELECT ?s ?o WHERE {{ ?s a <{_iri(v['fromEntity'])}> ; <{_iri(k)}> ?o }}"))
        for k, v in edges.items()
    }

    assert counts == {k: _expected_edge_rows(db, v) for k, v in edges.items()}


# ---------------------------------------------------------------------------
# Enrichment on the analyzer path (§6.3.2 step 2)
# ---------------------------------------------------------------------------


def test_analyzer_path_runs_the_analyzer_and_overlays_rpt(arango_client: Any) -> None:
    """The analyzer knows PG/LPG only; RPT must still be detected on its bundle.

    Also pins provenance: ``auto`` with the analyzer installed must report
    ``source.kind == "analyzer"`` — a silent fall back to the heuristic would
    otherwise pass every style assertion above for the wrong reason.
    """
    if not analyzer_available():
        pytest.skip("arangodb-schema-analyzer not installed")
    name = f"{_DB_PREFIX}analyzer_rpt"
    db = _fresh_db(arango_client, name)
    try:
        SEEDS["rpt_pg_lpg_hybrid"](db)

        bundle = acquire_mapping_bundle(db, strategy="auto")

        assert bundle.source.kind == "analyzer"
        assert "rpt" in (bundle.metadata.get("detectedPatterns") or [])
        overlays = [
            e for e in bundle.metadata.get("enrichmentApplied") or [] if e.get("kind") == "rpt_overlay"
        ]
        assert overlays and "_triples" in overlays[0]["collections"]
    finally:
        _drop_db(arango_client, name)


# ---------------------------------------------------------------------------
# Live fingerprints (§6.3.3)
# ---------------------------------------------------------------------------


def test_row_churn_moves_counts_but_not_shape(scratch_db: Any) -> None:
    """The two-fingerprint cache design: ordinary writes refresh statistics,
    only a structural change forces re-introspection."""
    if not analyzer_available():
        pytest.skip("live fingerprints come from arangodb-schema-analyzer")
    SEEDS["pg"](scratch_db)
    shape, counts = db_shape_fingerprint(scratch_db), db_counts_fingerprint(scratch_db)
    assert shape and counts, "fingerprints must be computed for a populated database"

    scratch_db.collection("persons").insert({"name": "Late Arrival"})

    assert db_shape_fingerprint(scratch_db) == shape
    assert db_counts_fingerprint(scratch_db) != counts


def test_a_new_collection_moves_the_shape(scratch_db: Any) -> None:
    if not analyzer_available():
        pytest.skip("live fingerprints come from arangodb-schema-analyzer")
    SEEDS["pg"](scratch_db)
    shape = db_shape_fingerprint(scratch_db)

    scratch_db.create_collection("studios")

    assert db_shape_fingerprint(scratch_db) != shape


def _write_schema_caches(db: Any, revision: int) -> None:
    """Write a document like the ones each transpiler keeps in its cache and
    examples collections."""
    for name in sorted(service_collections()):
        col = db.collection(name) if db.has_collection(name) else db.create_collection(name)
        col.insert(
            {"_key": "mapping", "schema_version": 1, "revision": revision, "bundle": {"metadata": {}}},
            overwrite=True,
        )


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_cache_collections_never_reach_the_analyzed_schema(scratch_db: Any, strategy: str) -> None:
    """Before the filter the analyzer turned both caches into classes, put them
    in an invented ``SchemaCache`` shard family, counted them in the
    statistics and exported them as OWL classes."""
    SEEDS["pg"](scratch_db)
    _write_schema_caches(scratch_db, revision=1)

    bundle = _acquire(scratch_db, strategy, include_owl=True)

    dumped = json.dumps(
        [bundle.conceptual_schema, bundle.physical_mapping, bundle.metadata, bundle.owl_turtle or ""]
    )
    # (The scratch database is named after the test, so the test name must not
    # contain "schema_cache" — the analyzer records the database name.)
    assert "schema_cache" not in dumped and "SchemaCache" not in dumped
    assert "_examples" not in dumped and "Examples" not in dumped
    entity_collections = {s["collectionName"] for s in bundle.physical_mapping["entities"].values()}
    assert entity_collections == {"docs", "persons", "places", "users"}


def test_cache_writes_do_not_move_the_fingerprints(scratch_db: Any) -> None:
    """A cache write must not mark the cache it just wrote as stale — nor may
    arango-cypher-py's cache writes make this service re-analyze."""
    if not analyzer_available():
        pytest.skip("live fingerprints come from arangodb-schema-analyzer")
    SEEDS["pg"](scratch_db)
    shape, counts = db_shape_fingerprint(scratch_db), db_counts_fingerprint(scratch_db)

    _write_schema_caches(scratch_db, revision=1)
    for name in service_collections():
        scratch_db.collection(name).insert({"_key": "graph-scoped", "bundle": {}})  # count moves too

    assert (db_shape_fingerprint(scratch_db), db_counts_fingerprint(scratch_db)) == (shape, counts)


# ---------------------------------------------------------------------------
# Named-graph scope (§6.3.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_graph_name_scopes_the_bundle_to_its_collections(scratch_db: Any, strategy: str) -> None:
    SEEDS["pg"](scratch_db)
    scratch_db.create_graph(
        "social",
        edge_definitions=[
            {
                "edge_collection": "follows",
                "from_vertex_collections": ["users"],
                "to_vertex_collections": ["users"],
            }
        ],
    )

    physical = _acquire(scratch_db, strategy, graph_name="social").physical_mapping

    assert {s["collectionName"] for s in physical["entities"].values()} == {"users"}
    assert {s["edgeCollectionName"] for s in physical["relationships"].values()} == {"follows"}


@pytest.fixture(autouse=True)
def _quiet_http_logs() -> None:
    # python-arango logs every request at INFO; keep failures readable.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
