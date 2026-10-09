"""Every valid ArangoDB collection name translates — including hyphenated ones.

Regression (prod.demo IAM, 2026-10-09): "How many chunks does each document
have?" crossed the edge collection ``IAM-TERRAFORM-DOCS-DEMO_Relations``.
``bind_collection`` checked collection names against the AQL *identifier*
pattern, raised ``ValueError`` on the hyphen, and the NL pipeline reported it
as "LLM transport failure". Collection names only ever reach AQL as ``@@``
bind parameters, so the check is ArangoDB's naming rule, not AQL's.
"""

from __future__ import annotations

import pytest

from arango_sparql.api import translate
from arango_sparql.errors import AqlEmitError
from arango_sparql.translate.builder import AqlQueryBuilder
from arango_sparql.translate.resolver import SchemaResolver

_TTL = """
@prefix :     <http://example.org/> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .

:Chunk a owl:Class ; phys:mappingStyle "COLLECTION" ; phys:collectionName "IAM-DOCS-DEMO_Chunks" .
:Document a owl:Class ; phys:mappingStyle "COLLECTION" ; phys:collectionName "IAM-DOCS-DEMO_Documents" .
:PART_OF a owl:ObjectProperty ; rdfs:domain :Chunk ; rdfs:range :Document ;
  phys:edgeCollectionName "IAM-TERRAFORM-DOCS-DEMO_Relations" .
"""
_TTL = "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n" + _TTL


def _finalized(name: str) -> tuple[str, dict]:
    b = AqlQueryBuilder()
    b.for_("doc", name)
    b.return_scalar("doc")
    return b.finalize()


@pytest.mark.parametrize("name", ["IAM-TERRAFORM-DOCS-DEMO_Relations", "a-b", "_system_like", "Résumé"])
def test_valid_arango_names_bind_and_keep_their_value(name: str) -> None:
    aql, bind_vars = _finalized(name)
    (key,) = [k for k in bind_vars if k.startswith("@")]
    # The placeholder is an AQL identifier; the bound VALUE is the real name,
    # which never appears in the query text.
    assert key[1:].replace("_", "").isascii() and key[1:].replace("_", "").isalnum()
    assert bind_vars[key] == name
    assert f"FOR doc IN @{key}" in aql  # referenced only through @@…
    if not name.replace("_", "").isalnum() or not name.isascii():
        assert name not in aql  # a non-identifier name never appears raw


def test_two_names_that_sanitize_alike_stay_distinct() -> None:
    b = AqlQueryBuilder()
    assert b.bind_collection("a-b") != b.bind_collection("a_b")


def test_repeated_name_reuses_one_bind_parameter() -> None:
    b = AqlQueryBuilder()
    assert b.bind_collection("a-b") == b.bind_collection("a-b")


@pytest.mark.parametrize("name", ["", "a/b", "tab\there", "x" * 257])
def test_invalid_names_are_a_translation_error_not_a_valueerror(name: str) -> None:
    with pytest.raises(AqlEmitError):
        AqlQueryBuilder().bind_collection(name)


def test_the_prod_demo_traversal_over_a_hyphenated_edge_collection() -> None:
    res = translate(
        "PREFIX : <http://example.org/>\n"
        "SELECT ?d (COUNT(?c) AS ?n) WHERE { ?c a :Chunk ; :PART_OF ?d . } GROUP BY ?d",
        resolver=SchemaResolver.from_turtle(_TTL),
    )
    assert "IAM-TERRAFORM-DOCS-DEMO_Relations" in res.bind_vars.values()
    assert "IAM-TERRAFORM" not in res.aql  # only ever bound, never inlined
