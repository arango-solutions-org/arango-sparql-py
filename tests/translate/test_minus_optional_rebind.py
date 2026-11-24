"""Issue #143 — an OPTIONAL in a MINUS right side that re-binds a variable its own required side binds.

In ``?x a :Person MINUS { ?x :likes ?y OPTIONAL { ?x :knows ?y } }`` the right
side's solutions are every ``?x :likes ?y`` row: the OPTIONAL extends a row
when ``?x :knows ?y`` holds and keeps it unextended when it does not (§18.5
LeftJoin), so it removes nothing. The MINUS probe tested the OPTIONAL as a
conditional add instead, dropping a row whose subject knows someone else, and
kept outer rows MINUS must remove. Each case runs on the AQL interpreter; the
expected rows are SPARQL's, worked by hand in the comment beside them.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.errors import UnsupportedSparqlError
from arango_sparql.translate.mapping import MappingBundle, MappingSource
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.aql_interp import run_aql_subset

_C = "urn:arango-sparql:concept#"
_E = "http://ex.org/"
_PREFIXES = f"PREFIX : <{_C}>\n"
_RELS = ["likes", "knows"]

_BUNDLE = MappingBundle(
    conceptual_schema={
        "entities": [{"name": "Person", "properties": [{"name": "age"}]}],
        "relationships": [{"name": r, "fromEntity": "Person", "toEntity": "Person"} for r in _RELS],
    },
    physical_mapping={
        "entities": {
            "Person": {
                "style": "COLLECTION",
                "collectionName": "people",
                "properties": {"age": {"field": "age"}},
            }
        },
        "relationships": {
            r: {
                "style": "DEDICATED_COLLECTION",
                "edgeCollectionName": r,
                "fromEntity": "Person",
                "toEntity": "Person",
            }
            for r in _RELS
        },
    },
    source=MappingSource(kind="manual"),
)


def _edges(pairs: str) -> list[dict[str, str]]:
    return [{"_from": f"people/{p[0]}", "_to": f"people/{p[1]}"} for p in pairs.split()]


# :a likes :b (knows nobody); :b likes and knows :c; :c likes :a and knows :b (not :a); :d likes nobody.
_DOCS: dict[str, list[dict[str, Any]]] = {
    "people": [{"_id": f"people/{k}", "_key": k, "_uri": f"{_E}{k}"} for k in "abcd"],
    "likes": _edges("ab bc ca"),
    "knows": _edges("bc cb"),
}


def _rows(query: str) -> Counter[str]:
    result = translate(_PREFIXES + query, resolver=SchemaResolver.from_mapping_bundle(_BUNDLE))
    return Counter(
        ",".join(f"{k}={str(v).rsplit('/', 1)[-1]}" for k, v in sorted(row.items()) if v is not None)
        for row in run_aql_subset(result.aql, result.bind_vars, _DOCS)
    )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # Every :likes subject is a right-side solution: only :d is kept (pyoxigraph's answer in #143).
        pytest.param(
            "SELECT ?x WHERE { ?x a :Person MINUS { ?x :likes ?y OPTIONAL { ?x :knows ?y } } }",
            ["x=d"],
            id="issue",
        ),
        # The re-bound variable is the OPTIONAL's subject's partner the other way round: still only :d.
        pytest.param(
            "SELECT ?x WHERE { ?x a :Person MINUS { ?x :likes ?y OPTIONAL { ?y :knows ?x } } }",
            ["x=d"],
            id="rebinds-x",
        ),
        # Shared {?x, ?y}: each outer :likes row is itself a right-side solution, so none is kept.
        pytest.param(
            "SELECT ?x ?y WHERE { ?x :likes ?y MINUS { ?x :likes ?y OPTIONAL { ?x :knows ?y } } }",
            [],
            id="outer-binds-y",
        ),
        # Outer :knows rows (b,c), (c,b); right side (a,b), (b,c), (c,a): only (c,b) is kept.
        pytest.param(
            "SELECT ?x ?y WHERE { ?x :knows ?y MINUS { ?x :likes ?y OPTIONAL { ?x :knows ?y } } }",
            ["x=c,y=b"],
            id="outer-knows",
        ),
        # Controls, right on main too: an OPTIONAL binding a new variable, and a plain join.
        pytest.param(
            "SELECT ?x WHERE { ?x a :Person MINUS { ?x :likes ?y OPTIONAL { ?x :knows ?z } } }",
            ["x=d"],
            id="control-new-variable",
        ),
        pytest.param(
            "SELECT ?x WHERE { ?x a :Person MINUS { ?x :likes ?y . ?x :knows ?y } }",
            ["x=a", "x=c", "x=d"],
            id="control-join",
        ),
        # Control: ?o is bound only outside, so the OPTIONAL's value decides the probe (conditional add,
        # unchanged). Each outer (s, o) has t = s with t :knows o, so every row is removed.
        pytest.param(
            "SELECT ?s ?o WHERE { ?s :knows ?o MINUS { ?t a :Person OPTIONAL { ?t :knows ?o } } }",
            [],
            id="control-outer-only-rebind",
        ),
    ],
)
def test_minus_optional_rebind_rows(query: str, expected: list[str]) -> None:
    assert _rows(query) == Counter(expected)


def test_rebind_with_other_triples_is_refused() -> None:
    # ?a is bound only where ?x also knows ?y; the inline emitter cannot gate it on that.
    query = "SELECT ?x ?a WHERE { ?x :age ?a MINUS { ?x :likes ?y OPTIONAL { ?x :knows ?y ; :age ?a } } }"
    with pytest.raises(UnsupportedSparqlError, match=r"re-binds \?y, already bound by its required side"):
        translate(_PREFIXES + query, resolver=SchemaResolver.from_mapping_bundle(_BUNDLE))
