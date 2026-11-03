"""Golden tests for SPARQL aggregate translation (rdflib ``AggregateJoin``).

The goldens live in ``aggregate.yml`` next to this file. See
``.cursor/rules/200-testing.mdc`` for the golden-test contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from arango_sparql.api import translate
from arango_sparql.errors import UnsupportedSparqlError
from arango_sparql.translate.resolver import SchemaResolver

GOLDEN_PATH = Path(__file__).parent / "aggregate.yml"


def _load_cases() -> list[tuple[str, str, str, str, dict]]:
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
    _load_cases(),
    ids=[c[0] for c in _load_cases()],
)
def test_aggregate_golden(
    name: str,
    ontology_ttl: str,
    sparql: str,
    expected_aql: str,
    expected_bind_vars: dict,
) -> None:
    resolver = SchemaResolver.from_turtle(ontology_ttl)
    result = translate(sparql, resolver=resolver)
    assert result.aql == expected_aql, (
        f"AQL mismatch for {name!r}:\n--- expected ---\n{expected_aql}\n--- actual ---\n{result.aql}"
    )
    assert result.bind_vars == expected_bind_vars, (
        f"bind_vars mismatch for {name!r}:\n"
        f"--- expected ---\n{expected_bind_vars}\n"
        f"--- actual ---\n{result.bind_vars}"
    )


def test_group_key_date_literal_is_json_safe() -> None:
    result = translate(
        """
        PREFIX : <http://example/>
        PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
        SELECT ?x (SAMPLE(?v) AS ?sample)
        WHERE { ?s :p ?v . OPTIONAL { ?s :q ?w } }
        GROUP BY (COALESCE(?w, "1605-11-05"^^xsd:date) AS ?x)
        """,
        resolver=SchemaResolver.from_turtle(""),
    )

    json.dumps(result.bind_vars)
    assert "1605-11-05" in result.bind_vars.values()


def test_group_concat_uses_space_as_the_default_separator() -> None:
    result = translate(
        """
        PREFIX : <http://example/>
        SELECT (GROUP_CONCAT(?value) AS ?joined)
        WHERE { ?subject :p ?value }
        """,
        resolver=SchemaResolver.from_turtle(""),
    )

    separator_binds = {name: value for name, value in result.bind_vars.items() if name.endswith("_sep")}
    assert separator_binds == {"_p1_sep": " "}


def test_grouped_aggregate_preserves_the_empty_solution() -> None:
    result = translate(
        """
        PREFIX : <http://example/>
        SELECT ?subject (MAX(?value) AS ?maximum)
        WHERE { ?subject :p ?value }
        GROUP BY ?subject
        """,
        resolver=SchemaResolver.from_turtle(""),
    )

    assert "LET results" in result.aql
    assert "IN (LENGTH(results" in result.aql
    assert "? [{}] : results" in result.aql


def test_having_does_not_turn_filtered_groups_into_an_empty_solution() -> None:
    result = translate(
        """
        PREFIX : <http://example/>
        SELECT ?subject (COUNT(?value) AS ?count)
        WHERE { ?subject :p ?value }
        GROUP BY ?subject
        HAVING (COUNT(?value) > 10)
        """,
        resolver=SchemaResolver.from_turtle(""),
    )

    assert "LET results" not in result.aql
    assert "FILTER" in result.aql


def test_count_distinct_star_is_refused_not_mistranslated() -> None:
    """COUNT(DISTINCT *) counts distinct solutions. It used to emit
    ``COUNT_DISTINCT(1)``, which is 1 whatever the data (found with #46)."""
    with pytest.raises(UnsupportedSparqlError, match=r"COUNT\(DISTINCT \*\)"):
        translate(
            "PREFIX : <http://example/> SELECT (COUNT(DISTINCT *) AS ?c) WHERE { ?s :p ?o }",
            resolver=SchemaResolver.from_turtle(""),
        )


def test_every_group_by_term_is_a_collect_key() -> None:
    """An unprojected GROUP BY variable and an unaliased expression are keys too.

    rdflib synthesises a Sample only for projected GROUP BY variables; keys
    taken from those alone merged groups (LDBC IC5). Behaviour is
    cross-validated in ``test_bgp_select_cross.py`` and live in
    ``tests/integration/test_group_by_keys_live.py``.
    """
    result = translate(
        """
        PREFIX : <http://example/>
        SELECT ?t (COUNT(?p) AS ?c)
        WHERE { ?f :title ?t . ?p :in ?f . ?f :lang ?l }
        GROUP BY ?t ?f STR(?l)
        ORDER BY ?f
        """,
        resolver=SchemaResolver.from_turtle(""),
    )
    lines = [line.strip() for line in result.aql.splitlines()]
    assert (
        "COLLECT grp3 = doc1.title, grp4 = NOT_NULL(doc1._uri, doc1._id), grp5 = TO_STRING(doc1.lang) "
        "WITH COUNT INTO agg6"
    ) in lines
    assert "SORT grp4 ASC" in lines


def test_a_group_by_variable_the_pattern_never_binds_is_a_null_key() -> None:
    """``GROUP BY ?typo ?t``: rdflib's in-scope set lacks ?typo, so it is unbound on every row."""
    result = translate(
        """
        PREFIX : <http://example/>
        SELECT ?typo ?t (COUNT(?p) AS ?c)
        WHERE { ?f :title ?t . ?p :in ?f }
        GROUP BY ?typo ?t
        """,
        resolver=SchemaResolver.from_turtle(""),
    )
    collect = next(line.strip() for line in result.aql.splitlines() if line.strip().startswith("COLLECT"))
    assert "= null" in collect and "= doc1.title" in collect


def test_a_values_block_after_grouping_reads_nothing_out_of_scope() -> None:
    """Issue #116: after the COLLECT only its keys and aggregates are in scope.

    A trailing ``VALUES ?s`` on a variable that is no GROUP BY key binds a
    variable the solutions lack, so it joins every solution (one per VALUES
    row); it used to compare the pre-COLLECT ``doc1`` after the COLLECT,
    which ArangoDB refused (ERR 1203). Checked against pyoxigraph live in
    ``tests/integration/test_group_by_keys_live.py``.
    """
    result = translate(
        "PREFIX : <http://example/>\nSELECT (COUNT(*) AS ?k) WHERE { ?s :name ?n } VALUES ?s { <http://example/a> }",
        resolver=SchemaResolver.from_turtle(""),
    )
    after_collect = result.aql.split("COLLECT", 1)[1]
    assert "doc1" not in after_collect
    assert "FOR row" in after_collect
