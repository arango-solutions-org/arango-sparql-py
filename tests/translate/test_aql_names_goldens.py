"""Golden tests: names ArangoDB cannot take bare are quoted (issues #75, #76).

The corpus lives in :mod:`aql_names.yml` next to this file. Each case pins the
exact AQL string and bind-vars dict. A case may carry its own ``ontology``
(the RPT column case); otherwise the file-level one applies.

The companion live test (``tests/integration/test_aql_names_live.py``) runs
the same shapes on ArangoDB against pyoxigraph, and re-validates the keyword
list against the server.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver

GOLDEN_PATH = Path(__file__).parent / "aql_names.yml"


def _load() -> list[tuple[str, str, str, str, dict]]:
    """Return ``(name, ontology_ttl, sparql, expected_aql, expected_bind_vars)``."""
    data = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))
    return [
        (
            case["name"],
            case.get("ontology", data["ontology"]),
            case["sparql"],
            case["expected_aql"].rstrip("\n"),
            case["expected_bind_vars"],
        )
        for case in data["cases"]
    ]


_CASES = _load()


@pytest.mark.parametrize(
    "name, ontology_ttl, sparql, expected_aql, expected_bind_vars",
    _CASES,
    ids=[c[0] for c in _CASES],
)
def test_aql_names_golden(
    name: str,
    ontology_ttl: str,
    sparql: str,
    expected_aql: str,
    expected_bind_vars: dict,
) -> None:
    result = translate(sparql, resolver=SchemaResolver.from_turtle(ontology_ttl))
    assert result.aql == expected_aql, f"{name}: AQL drifted"
    assert result.bind_vars == expected_bind_vars, f"{name}: bind vars drifted"


def test_corpus_names_are_unique() -> None:
    names = [c[0] for c in _CASES]
    assert len(names) == len(set(names))
