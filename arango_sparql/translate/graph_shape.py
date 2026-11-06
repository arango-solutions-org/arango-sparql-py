"""The graph a SELECT draws: its triple patterns, and a run that returns every entity in them.

A SELECT answers in rows of the variables it projects, so a question that projects names
(``SELECT ?memberName ?bookTitle``) carries no entity to draw, and the rows alone do not say
how the entities connect. The query does: ``?loan :borrowedBy ?member`` is an edge from each
loan to its member, ``?member :name ?memberName`` a property of the member, ``?loan a
:Loan`` its class. So a graph is drawn from the query's patterns and a run of the same query
that projects every variable in them.

* :func:`query_patterns` lists the basic graph patterns' triples, each term a variable, an
  IRI or a literal (a property path or blank node is left out). A pattern that holds only
  for some rows (inside ``OPTIONAL`` or a ``UNION`` branch) says which: ``when`` lists, for
  each such group around it, the variables only that group binds, and a row is drawn with it
  only when it binds one of each. A group that binds no variable of its own cannot say, so
  its patterns are left out, as is ``MINUS`` (what must *not* match).
* :func:`widen_projection` rewrites a plain ``SELECT … WHERE`` to ``SELECT DISTINCT *``,
  without ``ORDER BY``, ``LIMIT`` or ``OFFSET``: the graph shows the solutions behind the rows
  a client already has, which a limit on the widened rows would cut differently. Anything it
  cannot widen safely (an aggregate, ``GROUP BY``, an expression in the projection, a
  subquery) answers ``None``, and the caller draws the rows as they are.
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
# A keyword, not part of a variable (`?values`) or a prefixed name (`:having`, `select:x`).
_KW_START, _KW_END = r"(?<![?$:\w])", r"(?![:\w])"


def _keyword(pattern: str) -> re.Pattern[str]:
    return re.compile(_KW_START + pattern + _KW_END, re.IGNORECASE)


_SELECT = _keyword("SELECT")
_NOT_WIDENED = _keyword(r"(?:GROUP\s+BY|HAVING|VALUES)")
# Where the projection ends: a dataset clause (kept), WHERE, or the group itself.
_PROJECTION_END = re.compile(_KW_START + r"(?:FROM|WHERE)" + _KW_END + r"|\{", re.IGNORECASE)
_AGGREGATES = ("Aggregate", "AggregateJoin", "Group")
# An algebra node's sub-patterns. (rdflib's ``CompValue.get`` answers the key itself for a
# missing key, so presence is tested with ``in``.)
_CHILDREN = ("p", "p1", "p2")


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
    end = _PROJECTION_END.search(masked, selects[0].end())
    if end is None or "(" in masked[selects[0].end() : end.start()]:
        return None  # an expression (or aggregate) in the projection
    # The solution modifiers after the group (ORDER BY, LIMIT, OFFSET: GROUP BY, HAVING and
    # VALUES are refused above) are dropped; the group closes at the last brace.
    close = masked.rfind("}")
    if close < end.start():
        return None
    widened = sparql[:start] + "SELECT DISTINCT * " + sparql[end.start() : close + 1]
    try:
        # The original: an aggregate in the dropped ORDER BY still makes it one row per group.
        if _contains(parse_sparql(sparql).algebra, _AGGREGATES):
            return None
        parse_sparql(widened)
    except Exception:  # noqa: BLE001 — not widenable is an answer, not an error
        return None
    return widened


def query_patterns(sparql: str) -> list[dict[str, Any]]:
    """The triples of *sparql*'s basic graph patterns, in order, each once:
    ``{"subject": term, "predicate": term, "object": term}`` with a term
    ``{"var": name}``, ``{"iri": iri}`` or ``{"literal": text}``, and ``"when"`` for a pattern
    inside ``OPTIONAL`` or ``UNION``: a list of variable lists, each one a group around the
    pattern and the variables only that group binds (a row binds one of them exactly when the
    group matched it). A triple with a property path or a blank node is left out, as are
    ``MINUS`` and any group that binds no variable of its own."""
    algebra = parse_sparql(sparql).algebra
    total = _binding_sites(algebra)
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    def emit(triple: tuple[Any, Any, Any], when: tuple[tuple[str, ...], ...]) -> None:
        terms = tuple(_term(t) for t in triple)
        if any(t is None for t in terms) or "literal" in terms[1]:  # type: ignore[operator]
            return
        key = (*triple, when)
        if key in seen:
            return
        seen.add(key)
        pattern: dict[str, Any] = {"subject": terms[0], "predicate": terms[1], "object": terms[2]}
        if when:
            pattern["when"] = [list(group) for group in when]
        out.append(pattern)

    def group(node: Any, when: tuple[tuple[str, ...], ...]) -> None:
        """Walk *node*, a group that holds only for the rows binding one of its own variables."""
        own = _binding_sites(node)
        witness = tuple(sorted(v for v, n in own.items() if n == total.get(v)))
        if witness:
            walk(node, (*when, witness))

    def walk(node: Any, when: tuple[tuple[str, ...], ...]) -> None:
        name = getattr(node, "name", None)
        if name == "BGP":
            for triple in node.get("triples") or []:
                emit(tuple(triple), when)
        elif name == "Minus":
            walk(node["p1"], when)  # the right side is what must not match
        elif name == "LeftJoin":
            walk(node["p1"], when)
            group(node["p2"], when)
        elif name == "Union":
            group(node["p1"], when)
            group(node["p2"], when)
        elif isinstance(node, dict):
            for key in _CHILDREN:  # never an expression: EXISTS is a filter, not a fact
                if key in node and node[key] is not None:
                    walk(node[key], when)

    walk(algebra, ())
    return out


def _binding_sites(node: Any) -> dict[str, int]:
    """For each variable, how many places in *node* can bind it: a triple it is in, a
    ``BIND``, a ``GRAPH ?g``. ``MINUS`` and filters bind nothing."""
    counts: dict[str, int] = {}

    def add(term: Any) -> None:
        if isinstance(term, Variable):
            counts[str(term)] = counts.get(str(term), 0) + 1

    stack = [node]
    while stack:
        current = stack.pop()
        name = getattr(current, "name", None)
        if name == "BGP":
            for triple in current.get("triples") or []:
                for term in set(triple):
                    add(term)
            continue
        if name == "Extend":
            add(current.get("var"))
        elif name == "Graph":
            add(current.get("term"))
        if name == "Minus":
            stack.append(current["p1"])
        elif isinstance(current, dict):
            stack.extend(current[k] for k in _CHILDREN if k in current and current[k] is not None)
    return counts


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
