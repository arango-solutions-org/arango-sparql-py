"""Federation entry point — translate a query-graph partition (CDF M5 WP-C2).

The contextual-data-fabric federated query engine (module 05) splits a
conceptual SPARQL query into per-source partitions and hands each
source's leg a self-contained sub-query. This module is the Arango
leg's contract, per ``docs/architecture/proposals/federation-entry-point.md``
with the co-design questions resolved as below. (CDF does not call it today:
its executor sends each leg through :func:`arango_sparql.api.translate` and
writes the bind-join ``VALUES`` itself, for every engine; see the proposal's
status.)

1. **Wire shape** — a partition arrives as a *sub-SELECT string*
   (serializable, replayable, provider-agnostic). An algebra fast path
   can be added later without breaking this contract.
2. **Canonical key** — the *subject IRI* is the join key. For every
   variable named in ``canonical_keys`` we make sure the result object
   carries that variable's binding (adding it to the projection when
   the partition's own SELECT omitted it); no surrogate-key mapping
   annotation exists in v1.
3. **Seed bindings** — ``seed_bindings`` (rows from an earlier leg)
   are pushed down by appending a SPARQL ``VALUES`` clause after the
   solution modifiers (grammar: ``SelectQuery ::= … SolutionModifier
   ValuesClause``), so the seeds constrain the query *inside*
   ArangoDB instead of hauling the full result across the wire.
4. **``as_of``** — translation is pure; the M5 *executor* stamps
   :attr:`PartitionProvenance.as_of` at cursor-execution time. It is
   ``None`` here by design.

Failure semantics (CC-5 / FR-11): this function raises the library's
typed errors (:class:`~arango_sparql.errors.SparqlParseError`,
:class:`~arango_sparql.errors.UnsupportedSparqlError`, …) — the
engine turns an exception into a *declared-failed leg*, never a
silently dropped one; the stable ``code`` attribute is the
machine-readable reason.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from rdflib import Literal, URIRef

from .errors import UnsupportedSparqlError
from .translate.builder import AqlQueryBuilder
from .translate.parser import parse_sparql
from .translate.resolver import SchemaResolver
from .translate.visitor import AlgebraVisitor, collect_query_triples

_SCHEMA_WARNING_CODE_PREFIX = "W_SCHEMA_"

# Conservative IRI guard for text-level VALUES injection: characters
# that could terminate the ``<…>`` form or smuggle syntax into the
# query string are refused outright. (Everything literal-shaped goes
# through _escape_literal instead.)
_IRI_UNSAFE_RE = re.compile(r'[\s<>"{}|^`\\]')


@dataclass
class PartitionProvenance:
    """Per-leg provenance stamp (fabric FR-5 / FR-11 / FR-12).

    ``as_of`` is deliberately ``None`` at translate time — the
    executor stamps it when the cursor actually runs, so the citation
    reflects when the data was read, and translation stays a pure
    function (goldens, caching, replay).
    """

    source: str
    query_text: str
    aql: str
    source_objects: list[str]
    as_of: datetime | None = None


@dataclass
class PartitionResult:
    """What the M5 engine needs to execute and join one Arango leg."""

    aql: str
    bind_vars: dict[str, Any] = field(default_factory=dict)
    projected_vars: list[str] = field(default_factory=list)
    canonical_key_columns: dict[str, str] = field(default_factory=dict)
    provenance: PartitionProvenance | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)
    schema_warnings: list[dict[str, Any]] = field(default_factory=list)


def translate_partition(
    partition_sparql: str,
    *,
    resolver: SchemaResolver,
    canonical_keys: Sequence[str] = (),
    seed_bindings: Sequence[Mapping[str, Any]] | None = None,
    source: str = "arangodb",
    tenant_id: str | None = None,
) -> PartitionResult:
    """Translate one federation partition into an executable AQL leg.

    Parameters
    ----------
    partition_sparql:
        A self-contained ``SELECT`` query — the partition the M5
        planner scoped to Arango-resident sources.
    resolver:
        Same contract as :func:`arango_sparql.api.translate` (CSI /
        MappingBundle / OWL-Turtle derived).
    canonical_keys:
        Variable names (with or without the ``?``) whose bindings the
        engine will join on. Each is guaranteed a column in the result
        object; the binding value is the subject IRI when the variable
        binds an entity (decision #2 above). Note the column
        participates in ``DISTINCT`` exactly as if the user projected
        it.
    seed_bindings:
        Rows from an earlier leg to push down as a ``VALUES`` clause.
        Each row maps a variable name to one of: an ``rdflib.URIRef``
        / ``rdflib.Literal``; a SPARQL-JSON binding dict
        (``{"type": "uri"|"literal", "value": …, "datatype"?, "xml:lang"?}``
        — the shape a prior leg's SELECT results already have);
        a Python ``bool`` / ``int`` / ``float`` (numeric literal);
        a plain ``str`` (treated as a *plain literal* — pass IRIs as
        ``URIRef`` or ``{"type": "uri"}``); or ``None`` (``UNDEF``).
    source:
        Logical source name for the provenance stamp (CC-7: logical
        names only, never connection details).
    tenant_id:
        Same tenant gating as :func:`arango_sparql.api.translate`.
    """
    effective_sparql = partition_sparql
    if seed_bindings:
        effective_sparql = f"{partition_sparql.rstrip()}\n{_values_clause(seed_bindings)}"

    parsed = parse_sparql(effective_sparql)
    if getattr(parsed.algebra, "name", None) != "SelectQuery":
        raise UnsupportedSparqlError(
            "a federation partition must be a SELECT query; ASK/CONSTRUCT/"
            "DESCRIBE legs are not part of the WP-C2 contract"
        )

    key_vars = [k.lstrip("?") for k in canonical_keys]
    builder = AqlQueryBuilder()
    with resolver.collecting_warnings() as resolver_warnings:  # this translation's only (#157)
        visitor = AlgebraVisitor(
            builder=builder,
            resolver=resolver,
            explicit_projection=parsed.explicit_projection,
            base_iri=parsed.base_iri,
            tenant_id=tenant_id,
            extra_projection=key_vars,
            query_triples=collect_query_triples(parsed.algebra),
        )
        visitor.visit(parsed.algebra)
        aql, bind_vars = builder.finalize()

    if parsed.explicit_projection is not None:
        projected = [str(v) for v in parsed.explicit_projection]
    else:
        # ``SELECT *`` — mirror _emit_projection's fallback order.
        projected = list(visitor.state.var_to_expr.keys())
    for key_var in key_vars:
        if key_var not in projected:
            projected.append(key_var)

    combined_warnings: list[dict[str, Any]] = list(resolver_warnings) + list(builder.warnings)
    return PartitionResult(
        aql=aql,
        bind_vars=bind_vars,
        projected_vars=projected,
        canonical_key_columns={k: k for k in key_vars},
        provenance=PartitionProvenance(
            source=source,
            query_text=effective_sparql,
            aql=aql,
            # Collection bind vars are the leg's read set — the
            # builder routes every collection through @@-binds, so
            # this enumeration is complete by construction.
            source_objects=sorted({str(v) for k, v in bind_vars.items() if k.startswith("@")}),
        ),
        warnings=combined_warnings,
        schema_warnings=[
            w for w in combined_warnings if str(w.get("code", "")).startswith(_SCHEMA_WARNING_CODE_PREFIX)
        ],
    )


# ---------------------------------------------------------------------------
# Seed-binding serialization (SPARQL 1.1 §10.2.2 trailing ValuesClause)
# ---------------------------------------------------------------------------


def _values_clause(rows: Sequence[Mapping[str, Any]]) -> str:
    """Render seed rows as a post-query ``VALUES`` clause.

    Variables are the union of the rows' keys (missing cells become
    ``UNDEF``), sorted for deterministic output. Rendering goes
    through :func:`_term_to_sparql`, which validates IRIs and escapes
    literals — seeds come from a previous leg's *results*, i.e.
    untrusted data, and this text is spliced into a query.
    """
    if not rows:
        return ""
    var_names = sorted({str(k).lstrip("?") for row in rows for k in row})
    if not var_names:
        raise UnsupportedSparqlError("seed_bindings rows must bind at least one variable")
    header = " ".join(f"?{v}" for v in var_names)
    rendered_rows = []
    for row in rows:
        normalized = {str(k).lstrip("?"): v for k, v in row.items()}
        cells = " ".join(_term_to_sparql(normalized.get(v)) for v in var_names)
        rendered_rows.append(f"({cells})")
    return f"VALUES ({header}) {{ {' '.join(rendered_rows)} }}"


def _term_to_sparql(value: Any) -> str:
    """Serialize one seed cell to SPARQL term syntax (or ``UNDEF``)."""
    if value is None:
        return "UNDEF"
    if isinstance(value, URIRef):
        return _iri(str(value))
    if isinstance(value, Literal):
        if value.datatype is not None:
            return f"{_escape_literal(str(value))}^^{_iri(str(value.datatype))}"
        if value.language:
            return f"{_escape_literal(str(value))}@{value.language}"
        return _escape_literal(str(value))
    if isinstance(value, Mapping):
        # SPARQL-JSON results binding — the natural shape when the
        # engine feeds leg N's SELECT rows into leg N+1.
        kind = value.get("type")
        lex = value.get("value")
        if kind == "uri":
            return _iri(str(lex))
        if kind in ("literal", "typed-literal"):
            datatype = value.get("datatype")
            lang = value.get("xml:lang") or value.get("lang")
            if datatype:
                return f"{_escape_literal(str(lex))}^^{_iri(str(datatype))}"
            if lang:
                return f"{_escape_literal(str(lex))}@{lang}"
            return _escape_literal(str(lex))
        raise UnsupportedSparqlError(
            f"seed binding dict has unsupported type {kind!r} (expected 'uri' or 'literal')"
        )
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _escape_literal(value)
    raise UnsupportedSparqlError(
        f"seed binding value of type {type(value).__name__!r} is not serializable to a SPARQL term"
    )


def _iri(iri: str) -> str:
    if _IRI_UNSAFE_RE.search(iri):
        raise UnsupportedSparqlError(
            f"seed binding IRI {iri!r} contains characters unsafe for <…> serialization"
        )
    return f"<{iri}>"


def _escape_literal(text: str) -> str:
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'
