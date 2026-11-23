"""Golden tests for SPARQL 1.1 sub-SELECT + VALUES (``ToMultiSet``).

The corpus lives in :mod:`subselect.yml` next to this file — all
cases run against an empty resolver (default-collection fallback),
which is the configuration the W3C analyzer drives.

Three independent blocks of tests:

* **Supported goldens** — sub-SELECT (with / without LIMIT / ORDER /
  DISTINCT / aggregates / shared-variable join semantics) and
  VALUES (single-var, multi-var, UNDEF, standalone, integer
  literals). Each pins the exact AQL string and bind-vars dict so
  alias-numbering / bind-name / clause-ordering regressions surface
  loudly.

* **Resolver-driven interactions** — tests that need a populated
  resolver to exercise (PG-class-bound subject in a sub-SELECT,
  child-builder counter seeding across multiple sub-SELECTs in one
  query). These live in Python rather than YAML because they need
  richer setup than the YAML harness's single ``ontology`` field.

This slice bumped W3C query-evaluation coverage from 27.3 % to
32.8 % (+5.5 pp, +14 newly-passing tests across the ``ToMultiSet``
and ``values`` algebra buckets combined).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver

GOLDEN_PATH = Path(__file__).parent / "subselect.yml"


def _load_supported() -> list[tuple[str, str, str, str, dict]]:
    """Return ``(name, ontology_ttl, sparql, expected_aql,
    expected_bind_vars)`` per supported golden."""
    data = yaml.safe_load(GOLDEN_PATH.read_text())
    ttl = data["ontology"]
    out: list[tuple[str, str, str, str, dict]] = []
    for case in data["cases"]:
        out.append(
            (
                case["name"],
                ttl,
                case["sparql"],
                case["expected_aql"].rstrip("\n"),
                case["expected_bind_vars"],
            )
        )
    return out


@pytest.mark.parametrize(
    "name, ontology_ttl, sparql, expected_aql, expected_bind_vars",
    _load_supported(),
    ids=[c[0] for c in _load_supported()],
)
def test_subselect_golden(
    name: str,
    ontology_ttl: str,
    sparql: str,
    expected_aql: str,
    expected_bind_vars: dict,
) -> None:
    """Each supported golden produces the exact AQL the YAML declares.

    Pinning the AQL byte-for-byte protects against three classes
    of regression:

    1. **Child-builder bleed** — if the child's clauses leaked
       into the outer builder (or vice-versa), the inner block's
       indentation / structure would shift.
    2. **Counter seeding** — a regression in :meth:`create_child`
       that didn't seed counters would produce alias collisions
       (two ``doc1`` aliases, two ``_p1`` binds) — both would
       surface as visible string changes.
    3. **VALUES bind shape** — a regression in
       ``_emit_values`` that changed the row-dict structure
       (key ordering, UNDEF handling) would change the bind-vars
       dict in a way the equality check catches.
    """
    resolver = SchemaResolver.from_turtle(ontology_ttl, default_collection="Document")
    result = translate(sparql, resolver=resolver)
    assert result.aql == expected_aql, (
        f"AQL mismatch for {name!r}:\n--- expected ---\n{expected_aql}\n--- actual ---\n{result.aql}"
    )
    assert result.bind_vars == expected_bind_vars, (
        f"bind_vars mismatch for {name!r}:\n"
        f"--- expected ---\n{expected_bind_vars}\n"
        f"--- actual ---\n{result.bind_vars}"
    )


# ---------------------------------------------------------------------------
# Resolver-driven interaction tests.
#
# These live in Python (not YAML) because the inputs are richer than
# the YAML harness's single ``ontology`` field, or because the
# assertion targets an internal invariant rather than a deterministic
# AQL string.
# ---------------------------------------------------------------------------


_PG_PERSON_OWL = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
:Person a owl:Class ;
    phys:collectionName "Person" .
"""


def test_subselect_class_bound_subject() -> None:
    """Inner sub-SELECT with a typed subject reads from the
    typed-class collection, not the default one. Confirms the
    child visitor's resolver is the same instance as the
    parent's (otherwise the inner ``?s a :Person`` would
    refuse with ``SchemaResolutionError``)."""
    resolver = SchemaResolver.from_turtle(_PG_PERSON_OWL)
    result = translate(
        "PREFIX : <http://ex.org/> SELECT ?s WHERE { { SELECT ?s WHERE { ?s a :Person } } }",
        resolver=resolver,
    )
    assert result.aql == (
        "FOR row2 IN (\n  FOR doc1 IN @@c1_Person\n  RETURN { s: NOT_NULL(doc1._uri, doc1._id) }\n)\nRETURN { s: row2.s }"
    ), result.aql
    assert result.bind_vars == {"@c1_Person": "Person"}


def test_sibling_subselects_have_disjoint_aliases() -> None:
    """Two sibling sub-SELECTs in the same outer scope must not
    collide on document aliases or bind-variable names.

    This is the core invariant ``create_child`` / ``absorb_child``
    defend: seeding the child's counters with the parent's CURRENT
    state guarantees the child mints names disjoint from any name
    the parent already used, and absorbing pushes the counters
    back so subsequent parent mints don't repeat names the child
    just used. If either side dropped its counter sync, this test
    would fail with two ``doc1`` aliases in the output."""
    resolver = SchemaResolver.from_turtle("", default_collection="Document")
    result = translate(
        "PREFIX : <http://www.example.org/> "
        "SELECT ?x ?y ?z WHERE { "
        "  { SELECT ?x ?y WHERE { ?x :p1 ?y } } "
        "  { SELECT ?z WHERE { ?z :p2 ?w } } "
        "}",
        resolver=resolver,
    )
    # Two FOR loops over distinct doc aliases inside their
    # respective sub-queries; two distinct row aliases at the
    # outer level.
    assert "FOR doc1 IN @@c1_Document" in result.aql
    assert "FOR doc3 IN @@c2_Document" in result.aql
    assert "FOR row2 IN (" in result.aql
    assert "FOR row4 IN (" in result.aql
    # And the bind dict has two distinct collection binds.
    assert result.bind_vars == {
        "@c1_Document": "Document",
        "@c2_Document": "Document",
    }


def test_nested_subselect_counter_seeding() -> None:
    """A sub-SELECT inside another sub-SELECT must thread the
    counter through both levels — the inner-most's aliases must
    not collide with the middle-level's, which must not collide
    with the outer's.

    Regression coverage for the case where ``create_child`` was
    used recursively but the child failed to absorb its
    grand-child's counters back: the grand-child's mints would
    re-collide with the middle level's because the middle's
    counter never advanced."""
    resolver = SchemaResolver.from_turtle("", default_collection="Document")
    result = translate(
        "PREFIX : <http://ex.org/> "
        "SELECT ?s WHERE { "
        "  ?s :name ?n . "
        "  { SELECT ?s WHERE { "
        "      ?s :age ?a . "
        "      { SELECT ?s WHERE { ?s :foo ?bar } LIMIT 100 } "
        "    } "
        "  } "
        "}",
        resolver=resolver,
    )
    # Three levels deep ⇒ three distinct ``doc<N>`` aliases.
    assert "FOR doc1 IN @@c1_Document" in result.aql
    assert "FOR doc2 IN @@c2_Document" in result.aql
    assert "FOR doc3 IN @@c3_Document" in result.aql
    # And the inner-most LIMIT is in the inner-most block —
    # not at the outer or middle layer. ``index(")", ...)`` is too
    # naive now that predicate-existence ``FILTER HAS(doc, "attr")``
    # introduces its own parentheses; walk the paren stack instead
    # so we close on the actual sub-query boundary.
    inner_block_start = result.aql.index("FOR doc3 IN @@c3_Document")
    depth = 1
    inner_block_end = inner_block_start
    for idx in range(inner_block_start, len(result.aql)):
        ch = result.aql[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                inner_block_end = idx
                break
    assert "LIMIT 100" in result.aql[inner_block_start:inner_block_end]


def test_absorb_child_detects_bind_collision() -> None:
    """Sanity check on the bind-collision guard in
    :meth:`AqlQueryBuilder.absorb_child`.

    This guards against a subtle bug: if some downstream code
    resets the parent's bind-counter (or a future refactor
    bypasses ``create_child``), the child could mint the same
    bind name with a different value. Last-write-wins corruption
    of the bind-vars dict would be silent — the guard turns it
    into a loud :class:`AqlEmitError`.
    """
    from arango_sparql.errors import AqlEmitError
    from arango_sparql.translate.builder import AqlQueryBuilder

    parent = AqlQueryBuilder()
    parent.bind("alice", hint="uri")  # mints _p1_uri = "alice"
    # Bypass create_child() so the child's counter starts at zero
    # and collides with the parent's already-minted _p1_uri.
    child = AqlQueryBuilder()
    child.bind("bob", hint="uri")  # also mints _p1_uri (= "bob")
    child.for_("x", "Coll")
    child.return_scalar("1")
    with pytest.raises(AqlEmitError, match="bind-var name collision"):
        parent.absorb_child(child)


def test_absorb_child_never_moves_the_parent_counters_backwards() -> None:
    """The parent can mint while a child is open (an edge OPTIONAL's tenant
    guard binds into the parent). Absorbing the child then must keep the
    parent's higher counters, or the next child reuses the parent's names
    (review of #214: ``_pN_tenant_colls`` collided)."""
    from arango_sparql.translate.builder import AqlQueryBuilder

    parent = AqlQueryBuilder()
    child = parent.create_child()
    parent.bind(["Person"], hint="tenant_colls")  # parent mints while the child is open
    parent.fresh_alias()
    child.bind("x", hint="uri")
    child.for_("d", "Doc")
    child.return_scalar("d")
    parent.absorb_child(child)
    later = parent.create_child()
    later.bind(["Doc"], hint="tenant_colls")
    later.for_("e", "Doc")
    later.return_scalar("e")
    parent.absorb_child(later)  # raised AqlEmitError (bind-var name collision) before
    assert sorted(v for k, v in parent._bind_vars.items() if k.endswith("_tenant_colls")) == [
        ["Doc"],
        ["Person"],
    ]
