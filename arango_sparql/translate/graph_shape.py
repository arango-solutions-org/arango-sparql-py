"""The graph a SELECT draws: its triple patterns, and a run that returns every entity in them.

A SELECT answers in rows of the variables it projects, so a question that projects names
(``SELECT ?memberName ?bookTitle``) carries no entity to draw, and the rows alone do not say
how the entities connect. The query does: ``?loan :borrowedBy ?member`` is an edge from each
loan to its member, ``?member :name ?memberName`` a property of the member, ``?loan a
:Loan`` its class. So a graph is drawn from the query's patterns and a run of the same query
that projects every variable in them.

* :func:`query_patterns` lists the basic graph patterns' triples, each term a variable, an
  IRI or a literal (a property path or blank node is left out).
* :func:`widen_projection` rewrites a plain ``SELECT … WHERE`` to ``SELECT DISTINCT *``.
  Anything it cannot widen safely (an aggregate, ``GROUP BY``, an expression in the
  projection, a subquery) answers ``None``, and the caller draws the rows as they are.
"""

from __future__ import annotations

import re
from typing import Any

from rdflib import BNode, Literal, URIRef, Variable

from .parser import parse_sparql

# Spans that may contain keywords or brackets without being syntax: IRIs, strings (long
# forms first) and comments. Masked to spaces, so positions in the masked copy are the
# original's.
_OPAQUE = re.compile(
    r'"""(?:[^"\\]|\\.|"(?!""))*"""'
    r"|'''(?:[^'\\]|\\.|'(?!''))*'''"
    r'|"(?:[^"\\\n]|\\.)*"'
    r"|'(?:[^'\\\n]|\\.)*'"
    r"|<[^<>\"{}|^`\\\s]*>"
    r"|#[^\n]*",
)
_SELECT = re.compile(r"\bSELECT\b", re.IGNORECASE)
_NOT_WIDENED = re.compile(r"\bGROUP\s+BY\b|\bHAVING\b|\bVALUES\b", re.IGNORECASE)
_WHERE = re.compile(r"\bWHERE\b|\{", re.IGNORECASE)
_AGGREGATES = ("Aggregate", "AggregateJoin", "Group")


def _masked(text: str) -> str:
    return _OPAQUE.sub(lambda m: " " * len(m.group(0)), text)


def widen_projection(sparql: str) -> str | None:
    """*sparql* with its projection replaced by ``DISTINCT *``, or ``None`` when it is not a
    plain SELECT that can be widened safely."""
    masked = _masked(sparql)
    selects = list(_SELECT.finditer(masked))
    if len(selects) != 1 or _NOT_WIDENED.search(masked):
        return None
    start = selects[0].start()
    where = _WHERE.search(masked, selects[0].end())
    if where is None or "(" in masked[selects[0].end() : where.start()]:
        return None  # an expression (or aggregate) in the projection
    widened = sparql[:start] + "SELECT DISTINCT * " + sparql[where.start() :]
    try:
        parsed = parse_sparql(widened)
    except Exception:  # noqa: BLE001 — not widenable is an answer, not an error
        return None
    if _contains(parsed.algebra, _AGGREGATES):
        return None
    return widened


def query_patterns(sparql: str) -> list[dict[str, dict[str, str]]]:
    """The triples of *sparql*'s basic graph patterns, in order, each once:
    ``{"subject": term, "predicate": term, "object": term}`` with a term
    ``{"var": name}``, ``{"iri": iri}`` or ``{"literal": text}``. A triple with a property
    path or a blank node is left out."""
    out: list[dict[str, dict[str, str]]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for s, p, o in _triples(parse_sparql(sparql).algebra):
        terms = (_term(s), _term(p), _term(o))
        if any(t is None for t in terms) or "literal" in terms[1]:  # type: ignore[operator]
            continue
        if (s, p, o) in seen:
            continue
        seen.add((s, p, o))
        out.append({"subject": terms[0], "predicate": terms[1], "object": terms[2]})  # type: ignore[dict-item]
    return out


def _term(node: Any) -> dict[str, str] | None:
    if isinstance(node, Variable):
        return {"var": str(node)}
    if isinstance(node, URIRef):
        return {"iri": str(node)}
    if isinstance(node, Literal):
        return {"literal": str(node)}
    if isinstance(node, BNode):
        return None
    return None  # a property path


def _triples(node: Any) -> list[tuple[Any, Any, Any]]:
    found: list[tuple[Any, Any, Any]] = []
    stack = [node]
    while stack:
        current = stack.pop()
        name = getattr(current, "name", None)
        if name == "BGP":
            found.extend(tuple(t) for t in current.get("triples", []) or [])
            continue
        if isinstance(current, dict):
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, (list, tuple)):
            stack.extend(reversed(current))
    return found


def _contains(node: Any, names: tuple[str, ...]) -> bool:
    stack = [node]
    while stack:
        current = stack.pop()
        if getattr(current, "name", None) in names:
            return True
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
    return False
