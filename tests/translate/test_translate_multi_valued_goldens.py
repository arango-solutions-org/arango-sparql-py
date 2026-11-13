"""Golden tests for attributes declared multi-valued (contextual-data-fabric #220).

The goldens live in ``multi_valued.yml`` next to this file. See
``.cursor/rules/200-testing.mdc`` for the golden-test contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver

GOLDEN_PATH = Path(__file__).parent / "multi_valued.yml"


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
def test_multi_valued_golden(
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
