"""Thin wrapper over :mod:`rdflib.plugins.sparql` for SPARQL 1.1 parsing.

The rest of the codebase MUST use this module rather than touching
``rdflib`` directly so that:

1. Parser errors are converted to :class:`~arango_sparql.errors.SparqlParseError`
   with stable error codes for the HTTP layer.
2. The Algebra translation step (``translateQuery``) is always applied,
   so visitors see the optimized algebra rather than the raw parse tree.
3. The original projection-variable declaration order is preserved.
   ``rdflib.algebra.translateQuery`` collapses ``Project.PV`` into a
   set-iteration order that is **non-deterministic** across Python runs
   (set hash randomization). We capture the explicit projection list
   from the parsed query before that information is lost.

See ``.cursor/skills/sparql-to-aql/SKILL.md`` step 1 for the algebra
inspection workflow.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from rdflib import Literal, URIRef, Variable
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery

from ..errors import SparqlParseError, UnsupportedSparqlError

# NOTE (known issue — see .planning/phases/04-.../deferred-items.md):
# rdflib's SPARQL grammar uses module-level singleton pyparsing
# ``ParserElement`` objects with no thread-safety guarantee, so
# concurrent ``parseQuery``/``translateQuery`` calls can race under
# FastAPI's threaded dispatch (observed as a spurious
# ``<lambda>() missing 1 required positional argument: 'x'`` under
# concurrent load in ``tests/perf/test_concurrency.py``). A module-level
# lock was trialled but reverted (Phase 04): acquiring it from the async
# ``/sparql`` routes stalled the asyncio event loop. A proper fix
# (offloading parse to a threadpool executor, or a per-thread grammar)
# is deferred to a dedicated plan.


@dataclass
class ParsedSparql:
    """Result of :func:`parse_sparql`.

    ``algebra`` is the rdflib Algebra root node the visitor walks.
    ``explicit_projection`` is the declared projection variable list
    *iff* the query used an explicit ``SELECT ?a ?b`` form; ``None``
    when the query used ``SELECT *`` (in which case the visitor falls
    back to its own deterministic variable-binding order).

    ``describe_resources`` is the declared resource list for a
    ``DESCRIBE`` query — either the explicit IRI list (``DESCRIBE
    <a> <b>``) or the variable list (``DESCRIBE ?s ?o WHERE …``).
    The visitor reads this in preference to the algebra's ``PV``
    field because ``translateQuery`` collapses ``PV`` through a set
    iteration (non-deterministic across Python runs courtesy of
    ``PYTHONHASHSEED``). ``None`` for non-DESCRIBE queries and for
    ``DESCRIBE *`` (which rdflib stores as the literal string
    ``"var"`` — an unsupported shape that the visitor will refuse
    cleanly).

    ``base_iri`` is the absolute IRI from a SPARQL ``BASE`` directive,
    or ``None`` when the query declares none. Used by ``IRI()`` /
    ``URI()`` to resolve relative string arguments (SPARQL §17.4.2.8).
    """

    algebra: Any
    explicit_projection: list[Variable] | None = None
    describe_resources: list[Variable | URIRef] | None = None
    base_iri: str | None = None


#: SPARQL 1.1's PN_CHARS_U (grammar [164], [165]): the letters a name may use.
#: Raw, so ``re`` reads the escapes and the source shows no invisible characters.
_PN_CHARS_U = (
    r"_A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u02ff\u0370-\u037d\u037f-\u1fff"
    r"\u200c-\u200d\u2070-\u218f\u2c00-\u2fef\u3001-\ud7ff\uf900-\ufdcf\ufdf0-\ufffd"
    r"\U00010000-\U000effff"
)

#: A variable name inline VALUES accepts: SPARQL 1.1's VARNAME (grammar [166]),
#: so any name a query can spell can be given as data, ``?straße`` and ``?1st``
#: included. Names are written into the query text, and every character of a
#: VARNAME is part of the name's token there: nothing can end it early.
#: Anything else is refused, never escaped.
_VARNAME = re.compile(rf"[{_PN_CHARS_U}0-9][{_PN_CHARS_U}0-9\u00b7\u0300-\u036f\u203f-\u2040]*")


@dataclass(frozen=True)
class InlineValues:
    """Rows for a trailing ``VALUES`` clause, given as data, not text.

    ``variables`` are bare names (no ``?``). Each row holds one cell per
    variable: an :class:`~rdflib.term.URIRef` or :class:`~rdflib.term.Literal`
    as rdflib would parse it, or ``None`` for ``UNDEF``.

    A bind join's seed is data before it is ever text, and rdflib parses a
    ``VALUES`` row at about 0.13 ms (arango-solutions-org/arango-sparql-py#160):
    a thousand-row seed costs more than the rest of the query. Passing the rows
    here skips that parse. The query text is still parsed by rdflib alone, and
    rdflib still builds the algebra from the rows (see :func:`parse_sparql`).
    """

    variables: tuple[str, ...]
    rows: Sequence[Sequence[URIRef | Literal | None]]


def parse_sparql(query: str, values: InlineValues | None = None) -> ParsedSparql:
    """Parse *query* and return the rdflib Algebra plus declared projection.

    With *values*, the query is parsed with an empty trailing data block,
    ``VALUES (?a ?b) { }``, and the rows are set on rdflib's own
    ``ValuesClause`` before ``translateQuery``: the algebra is exactly the one
    rdflib builds from the same rows written as text, without parsing them.

    Raises
    ------
    SparqlParseError
        If ``rdflib`` cannot parse the input string, or *values* is
        malformed, or the query already ends in a ``VALUES`` clause and
        *values* is given too.
    UnsupportedSparqlError
        If ``rdflib``'s parser exceeds Python's recursion limit: deeply
        nested groups or expressions, and always a ``SERVICE`` nested in a
        ``SERVICE``. That says nothing about whether the query is valid, so
        it is not reported as malformed either.
    """
    if not isinstance(query, str) or not query.strip():
        raise SparqlParseError("SPARQL query must be a non-empty string")
    rows = None if values is None else _checked_rows(values)
    try:
        if values is None:
            parsed = parseQuery(query)
        else:
            parsed = _parse_with_empty_values(query, values)
            parsed[1]["valuesClause"]["value"] = rows
        translated = translateQuery(parsed)
    except RecursionError as exc:
        # upstream: RDFLib/rdflib#3082 — remove when an rdflib release includes RDFLib/rdflib#3608
        raise UnsupportedSparqlError(
            "the SPARQL parser (rdflib) went past Python's recursion limit on this query, so it "
            "cannot be translated. Deeply nested groups or expressions do this, and a SERVICE nested "
            "in a SERVICE always does (RDFLib/rdflib#3082, fixed on rdflib's main branch by #3608, "
            "not yet released)"
        ) from exc
    except SparqlParseError:
        raise
    except Exception as exc:
        raise SparqlParseError(f"failed to parse SPARQL: {exc}") from exc
    explicit = _extract_explicit_projection(parsed)
    describe = _extract_describe_resources(parsed)
    base_iri = _extract_base_iri(parsed)
    return ParsedSparql(
        algebra=translated.algebra,
        explicit_projection=explicit,
        describe_resources=describe,
        base_iri=base_iri,
    )


def _checked_rows(values: InlineValues) -> list[list[Any]]:
    """*values*' rows as rdflib's parse tree holds them, or why they can't be.

    rdflib keeps ``UNDEF`` as the string ``"UNDEF"`` in a ``ValuesClause``;
    callers say ``None``, so that sentinel stays rdflib's.
    """
    variables = values.variables
    # A str is a sequence of strs: ``"sn"`` would read as ``?s ?n``.
    if isinstance(variables, str):
        raise SparqlParseError(
            f"inline VALUES variables must be a sequence of names, not the string {variables!r}"
        )
    if not variables:
        raise SparqlParseError("inline VALUES needs at least one variable")
    for name in variables:
        if not isinstance(name, str) or not _VARNAME.fullmatch(name):
            raise SparqlParseError(f"inline VALUES variable {name!r} is not a SPARQL variable name")
    if len(set(variables)) != len(variables):
        raise SparqlParseError(f"inline VALUES names a variable twice: {list(variables)}")
    rows: list[list[Any]] = []
    for index, row in enumerate(values.rows):
        # URIRef and Literal are strs too: a bare term would read as its characters.
        if isinstance(row, str):
            raise SparqlParseError(
                f"inline VALUES row {index} is the single term {row!r}, not a sequence of cells"
            )
        if len(row) != len(variables):
            raise SparqlParseError(
                f"inline VALUES row {index} has {len(row)} cells for {len(variables)} variables"
            )
        cells: list[Any] = []
        for cell in row:
            if cell is None:
                cells.append("UNDEF")
            elif isinstance(cell, (URIRef, Literal)):
                cells.append(cell)
            else:
                raise SparqlParseError(
                    f"inline VALUES row {index} holds {type(cell).__name__} {cell!r}: a cell is "
                    "a URIRef, a Literal, or None for UNDEF"
                )
        rows.append(cells)
    return rows


def _parse_with_empty_values(query: str, values: InlineValues) -> Any:
    """Parse *query* with an empty trailing data block for *values*' variables.

    A query that already ends in ``VALUES`` would then have two, which the
    grammar refuses; that case is told apart from any other parse error.
    """
    declared = " ".join(f"?{name}" for name in values.variables)
    try:
        return parseQuery(f"{query}\nVALUES ({declared}) {{ }}")
    except RecursionError:
        raise
    except Exception:
        own = parseQuery(query)  # raises the query's own error if it has one
        # ``in``, not ``get``: CompValue.get returns the key's name when absent.
        if "valuesClause" in own[1]:
            raise SparqlParseError(
                "the query already ends in a VALUES clause; give its rows either in the text "
                "or as inline values, not both"
            ) from None
        raise


def _extract_base_iri(parsed: Any) -> str | None:
    """Return the SPARQL ``BASE`` IRI string, or ``None`` if undeclared.

    rdflib's ``parseQuery`` returns ``(prologue, query)`` where the
    prologue is a list of ``Base`` / ``PrefixDecl`` parse nodes. A
    ``Base`` node carries ``iri`` as a :class:`~rdflib.term.URIRef`.
    """
    try:
        prologue, _query = parsed
    except (ValueError, TypeError):
        return None
    if prologue is None:
        return None
    try:
        entries = list(prologue)
    except TypeError:
        return None
    for entry in entries:
        if getattr(entry, "name", None) != "Base":
            continue
        iri = entry.get("iri") if hasattr(entry, "get") else getattr(entry, "iri", None)
        if iri is not None:
            return str(iri)
    return None


def _extract_explicit_projection(parsed: Any) -> list[Variable] | None:
    """Return the declared projection list, or ``None`` for ``SELECT *``.

    rdflib's ``parseQuery`` returns ``(prologue, query)``. For an
    explicit projection (``SELECT ?a ?b``) the query carries a
    ``projection`` key whose value is a list of ``vars`` records — each
    with a ``var`` slot pointing at the actual ``Variable``. ``SELECT *``
    omits the key entirely.
    """
    try:
        _, query = parsed
    except (ValueError, TypeError):
        return None
    if getattr(query, "name", None) != "SelectQuery":
        return None
    projection = query.get("projection")
    if not projection:
        return None
    out: list[Variable] = []
    for entry in projection:
        if not hasattr(entry, "get"):
            continue
        # Plain ``?x`` projection entries set ``var``; aliased
        # projections (``(<expr> AS ?x)``, including aggregates and
        # ``BIND``-shaped projections inside SELECT) set ``evar``.
        # Both produce the same downstream binding semantics — pick
        # whichever the entry carries.
        #
        # Subtlety: rdflib's ``CompValue.get`` returns the *key name*
        # as the default when the key is absent (instead of ``None``),
        # so we must verify the result is actually a ``Variable``
        # before treating it as one.
        var = entry.get("var")
        if not isinstance(var, Variable):
            var = entry.get("evar")
        if isinstance(var, Variable):
            out.append(var)
    return out or None


def _extract_describe_resources(parsed: Any) -> list[Variable | URIRef] | None:
    """Return the declared DESCRIBE resource list in source order.

    Mirrors :func:`_extract_explicit_projection` but for DESCRIBE: the
    pre-translation parsed query carries a ``"var"`` slot on the
    DescribeQuery node that holds either a list of explicit terms
    (``DESCRIBE <a> <b>`` → ``[<a>, <b>]``, ``DESCRIBE ?s ?o`` →
    ``[?s, ?o]``) or the literal string ``"var"`` for the wildcard
    ``DESCRIBE *`` shape (an inert sentinel from rdflib's
    ``CompValue.get`` default-key behaviour).

    Returns ``None`` for non-DESCRIBE queries and for the wildcard
    sentinel; the visitor turns ``None`` for a DESCRIBE into a
    typed error so a wildcard never silently degrades to "describe
    nothing".
    """

    try:
        _, query = parsed
    except (ValueError, TypeError):
        return None
    if getattr(query, "name", None) != "DescribeQuery":
        return None
    raw = query.get("var")
    if not isinstance(raw, list):
        # ``CompValue.get`` returns the key name when the slot is
        # missing — the wildcard ``DESCRIBE *`` case lands here.
        return None
    out: list[Variable | URIRef] = []
    for term in raw:
        if isinstance(term, (Variable, URIRef)):
            out.append(term)
    return out or None
