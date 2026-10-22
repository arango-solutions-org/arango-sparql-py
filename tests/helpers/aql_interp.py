"""Tiny AQL-subset interpreter for cross-validation tests.

Cross-validation tests treat ``pyoxigraph`` as the W3C ground truth and
assert that the AQL our translator emits — when "executed" against an
in-memory document store derived from the same triples — yields the
same bindings. Rather than stand up a live ArangoDB for every case
(those go under the ``integration`` marker), we run the translated AQL
through this pure-Python interpreter that understands exactly the
clause shapes the visitor emits today:

    FOR / FILTER / LET / COLLECT / SORT / LIMIT / RETURN

It is deliberately *not* a general AQL engine. The grammar grows only
when the visitor learns to emit a new clause, and every extension is
covered by a cross test so the interpreter cannot silently diverge from
real AQL semantics.

This module is the single home for the interpreter so every
cross-validation module (PG-only ``test_bgp_select_cross`` and the
multi-model ``test_multimodel_cross``) shares one implementation —
mirroring how :mod:`tests.helpers.oxi` centralises the pyoxigraph side.
"""

from __future__ import annotations

import re
from typing import Any

# The keyword list is ArangoDB's, re-validated against a live server by
# ``tests/integration/test_aql_names_live.py`` — so importing it here keeps
# one list, pinned to the server, not a second copy that could drift.
from arango_sparql.translate.builder import AQL_RESERVED_NAMES

_FOR_RE = re.compile(r"FOR\s+(\w+)\s+IN\s+@@(\w+)")
# Graph traversal the edge-collection visitor emits for object
# properties: ``FOR v2, e3 IN OUTBOUND doc1 @@c2_knows``. The start
# vertex (``doc1``) is an already-bound document; OUTBOUND follows
# edges whose ``_from`` equals the start's ``_id`` and binds the target
# vertex (``v2``) and the edge (``e3``). DEDICATED_COLLECTION emits this
# bare; GENERIC_WITH_TYPE follows it with a ``FILTER e3.<field> == @x``.
_FOR_TRAVERSAL_RE = re.compile(r"FOR\s+(\w+)\s*,\s*(\w+)\s+IN\s+OUTBOUND\s+(\w+)\s+@@(\w+)")
_FILTER_RE = re.compile(r"FILTER\s+(.+)$")
_LET_RE = re.compile(r"LET\s+(\w+)\s*=\s*(.+)$")
_RETURN_RE = re.compile(r"RETURN(?:\s+(DISTINCT))?\s+\{\s*(.+?)\s*\}\s*$")
# Scalar projection the MINUS / EXISTS probe subquery emits
# (``RETURN 1``) — anything after RETURN that is not a ``{…}`` object
# and not the ``DISTINCT`` keyword. Checked only after the object-form
# RETURN above fails to match.
_RETURN_SCALAR_RE = re.compile(r"RETURN\s+(?!DISTINCT\b)([^{].*?)\s*$")
# Correlated row-count subquery the MINUS / NOT EXISTS / EXISTS emitter
# wraps an inner probe in: ``LET <alias> = LENGTH((`` opens the block,
# a bare ``))`` closes it, and the lines between are a self-contained
# (but outer-correlated) FOR/FILTER/LIMIT/RETURN-scalar query. The
# interpreter executes that inner query against the current outer row's
# environment and binds ``<alias>`` to the resulting row count.
_LEN_SUBQUERY_START_RE = re.compile(r"LET\s+(\w+)\s*=\s*LENGTH\(\(\s*$")
_SUBQUERY_END_RE = re.compile(r"^\)\)\s*$")
# Row-returning correlated subquery the cross-subject OPTIONAL emitter
# (ADR-0002 Problem 1, Option A) binds to a LET: ``LET <alias> = (``
# opens the block, a bare ``)`` closes it, and the inner lines are a
# self-contained (outer-correlated) FOR/FILTER/RETURN-object query. The
# alias is bound to the *list of row dicts* (not a count), which the
# following FOR-inline then iterates with ``[null]`` padding. Checked
# after ``_LEN_SUBQUERY_START_RE`` so ``LET x = LENGTH((`` never matches
# this opener.
_ROWS_SUBQUERY_START_RE = re.compile(r"LET\s+(\w+)\s*=\s*\(\s*$")
_SUBQUERY_PAREN_END_RE = re.compile(r"^\)\s*$")
# FOR over an inline list expression — the ``[null]``-padded left-join
# loop the cross-subject OPTIONAL emitter writes:
# ``FOR <row> IN (LENGTH(<opt>) > 0 ? <opt> : [null])``. Distinct from
# ``_FOR_RE`` (collection bind) and ``_FOR_TRAVERSAL_RE`` (OUTBOUND) by
# the parenthesised expression source; checked after both.
_FOR_INLINE_RE = re.compile(r"FOR\s+(\w+)\s+IN\s+(\(.+\))\s*$")
# FOR over an array literal — the empty-BGP scope opener the visitor emits
# before a BIND with no pattern of its own: ``FOR empty1 IN [1]``.
_FOR_ARRAY_RE = re.compile(r"FOR\s+(\w+)\s+IN\s+(\[.*\])\s*$")
# FOR over a list-valued bind variable — the SPARQL VALUES shape the
# visitor emits via ``builder.for_values`` (``FOR row1 IN @_p1_values``)
# and the federation entry point's seed-binding pushdown reuses.
# Checked after ``_FOR_RE`` (which requires the double-@ collection
# form) and before ``_FOR_INLINE_RE``.
_FOR_VALUES_RE = re.compile(r"FOR\s+(\w+)\s+IN\s+@(_\w+)\s*$")
# FOR over a builtin call — the list fan-out under ``fan_out_list_values``:
# ``FOR lv2 IN FLATTEN([doc1.email])`` iterates a list attribute's elements
# (required triples, and OPTIONAL since issue #53).
_FOR_CALL_RE = re.compile(r"FOR\s+(\w+)\s+IN\s+([A-Z_][A-Z0-9_]*\(.+\))\s*$")
# FOR over a LET-bound row list — the shape ``finalize()`` emits for
# ``SELECT DISTINCT … LIMIT`` (``LET deduped2 = (…)`` then
# ``FOR row3 IN deduped2``). A bare identifier, so neither the ``@``
# forms above nor the parenthesised ``_FOR_INLINE_RE`` match it.
_FOR_LET_RE = re.compile(r"FOR\s+(\w+)\s+IN\s+([A-Za-z_]\w*)\s*$")
_LIMIT_RE = re.compile(r"LIMIT\s+(?:(\d+)\s*,\s*)?(\d+)")
_SORT_RE = re.compile(r"SORT\s+(.+)$")
# COLLECT clause patterns the visitor emits today:
#   COLLECT WITH COUNT INTO <c>
#   COLLECT k1 = e1[, k2 = e2 …] WITH COUNT INTO <c>
#   COLLECT k1 = e1[, k2 = e2 …] AGGREGATE a = f(e)[, …]
#   COLLECT AGGREGATE a = f(e)[, …]
# Parsed by splitting on ``WITH COUNT INTO`` and ``AGGREGATE`` rather
# than a single regex — keeps the parser readable as the grammar grows.
_COLLECT_RE = re.compile(r"^COLLECT\b")
# WITH-clause prelude the RPT/sharded translator emits to declare the
# collections a traversal touches: ``WITH @@c1, @@c2 RETURN …`` — the
# interpreter treats it as a no-op (it does not need collection
# declarations to resolve ``FOR … IN @@coll`` bind vars).
_WITH_RE = re.compile(r"^WITH\b")
# Document/traversal attribute access. ``docN`` are FOR-loop documents
# and triples rows; ``vN`` / ``eN`` are the target vertex / edge bound
# by an OUTBOUND traversal; ``optrowN`` is the per-row dict bound by the
# cross-subject OPTIONAL FOR-inline loop (or ``null`` for the padded
# no-match row). All are accessed as ``<alias>.<attr>`` and rewritten to
# the flat ``<alias>__<attr>`` namespace key.
_DOC_ATTR_RE = re.compile(r"\b((?:doc|v|e|optrow|row)\d+)\.(\w+)\b")
# Post-rewrite namespace identifier shape — used by the eval namespace
# default-dict to recognise a missing-doc-attribute lookup as a
# null-binding (AQL parity) rather than a programmer error.
_DOC_ATTR_NAMESPACE_RE = re.compile(r"^(?:doc|v|e|optrow|row)\d+__\w+$")
# A backtick-quoted attribute read, ``doc1.`_2``` / ``row2.`filter```: same
# value as a bare read; on a ``null`` row (the OPTIONAL pad) it is null.
_QUOTED_ATTR_RE = re.compile(r"\b((?:doc|v|e|optrow|row)\d+)\.`([^`\\]*)`")
# What ArangoDB's lexer takes bare as a name: a leading underscore must be
# followed by a letter (``_key`` yes, ``_2`` no). Combined with the keyword
# lists below, the interpreter refuses exactly the bare spellings ArangoDB
# refuses (ERR 1501) instead of evaluating AQL the server would reject —
# the infidelity that hid issues #75 and #76 from every cross test.
_BARE_NAME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*|_+[A-Za-z][A-Za-z0-9_]*)$")
# Object keys ArangoDB rejects bare — a subset of ``AQL_RESERVED_NAMES``
# (``{limit: 1}`` parses, ``doc.limit`` does not). Validated live alongside it.
AQL_RESERVED_OBJECT_KEYS = frozenset(
    {
        "aggregate",
        "all_shortest_paths",
        "and",
        "asc",
        "collect",
        "desc",
        "distinct",
        "false",
        "filter",
        "for",
        "graph",
        "in",
        "insert",
        "k_paths",
        "k_shortest_paths",
        "let",
        "not",
        "null",
        "or",
        "remove",
        "replace",
        "return",
        "shortest_path",
        "sort",
        "true",
        "update",
        "upsert",
    }
)
_BIND_RE = re.compile(r"@(_\w+)")
# C-style ternary the visitor emits inside OPTIONAL+FILTER LETs and the
# cross-subject OPTIONAL FOR-inline source:
#   ``(<cond> ? <true> : <false>)``
# Python's conditional is ``<true> if <cond> else <false>``, so we
# textually rewrite. The false branch in our visitor output is a bare
# identifier, ``null``, or a list literal (``[null]`` — the padded
# no-match row of a left join); anything more complex would need a
# depth-aware parser.
_TERNARY_RE = re.compile(r"\((.+?) \? (.+?) : (\w+(?:\.\w+)?|null|\[[^\]]*\])\)")


def _regex_test(text: Any, pattern: str, case_insensitive: bool = False) -> bool:
    if text is None:
        return False
    flags = re.IGNORECASE if case_insensitive else 0
    return bool(re.search(pattern, str(text), flags))


def _contains(haystack: Any, needle: str) -> bool:
    if haystack is None:
        return False
    return needle in str(haystack)


def _starts_with(s: Any, prefix: str) -> bool:
    return s is not None and str(s).startswith(prefix)


def _right(s: Any, n: Any) -> str | None:
    """AQL ``RIGHT(value, n)`` — the rightmost *n* characters.

    Emitted by the STRENDS lowering (``RIGHT(x, LENGTH(y)) == y`` —
    AQL has no ``ENDS_WITH`` builtin, and this interpreter must only
    implement functions the real server has, or cross-validation
    passes AQL that a live ArangoDB rejects).
    """
    if s is None or n is None:
        return None
    count = int(n)
    if count <= 0:
        return ""
    text = str(s)
    return text[-count:]


def _to_string(v: Any) -> str | None:
    return None if v is None else str(v)


def _aql_attr(obj: Any, name: str) -> Any:
    """AQL attribute access: the value, or null when absent or *obj* is not
    an object (``null.x`` is null in AQL, never an error)."""
    return obj.get(name) if isinstance(obj, dict) else None


def _bare_attr(match: re.Match[str]) -> str:
    alias, name = match.group(1), match.group(2)
    if not _BARE_NAME_RE.match(name) or name.lower() in AQL_RESERVED_NAMES:
        raise AssertionError(
            f"ArangoDB rejects the bare attribute read {alias}.{name} (ERR 1501); it must be backtick-quoted"
        )
    return f"{alias}__{name}"


def _object_pair(pair: str) -> tuple[str, str]:
    """Split one ``key: expr`` pair of a ``RETURN {…}`` object the way AQL
    reads it: a backtick-quoted key is its inner text; a bare key must be one
    ArangoDB accepts bare."""
    pair = pair.strip()
    if pair.startswith("`"):
        end = pair.index("`", 1)
        key, rest = pair[1:end], pair[end + 1 :].lstrip()
        if not rest.startswith(":"):
            raise AssertionError(f"malformed RETURN pair: {pair!r}")
        return key, rest[1:].strip()
    key, _, value_expr = pair.partition(":")
    key = key.strip()
    if not _BARE_NAME_RE.match(key) or key.lower() in AQL_RESERVED_OBJECT_KEYS:
        raise AssertionError(f"ArangoDB rejects the bare object key {key!r} (ERR 1501); it must be quoted")
    return key, value_expr.strip()


def _not_null(*args: Any) -> Any:
    """AQL ``NOT_NULL`` — first non-null argument, else null.

    The RPT translator emits ``NOT_NULL(t.object_uri, t.object_value)``
    everywhere it reads a triple's object, because in a triples table a
    given object is stored in exactly one of the two columns (URI vs
    literal) and the other is null. (AQL has no ``COALESCE`` — this is
    its spelling of the same operation.)
    """
    for a in args:
        if a is not None:
            return a
    return None


# Whitelisted name → callable / value for the eval namespace. Anything
# not in this map and not a doc attribute / bind var triggers an
# explicit error so we notice when the visitor emits AQL the
# interpreter doesn't yet understand.
_AQL_BUILTINS: dict[str, Any] = {
    "_aql_attr": _aql_attr,
    "REGEX_TEST": _regex_test,
    "CONTAINS": _contains,
    "STARTS_WITH": _starts_with,
    "RIGHT": _right,
    "TO_STRING": _to_string,
    "NOT_NULL": _not_null,
    # One-level list flatten used by the multi-valued fan-out emission
    # (``FOR v IN FLATTEN([attr])`` and ``@v IN FLATTEN([attr])``):
    # a list attribute yields its elements, a scalar yields ``[scalar]``.
    "FLATTEN": lambda v: (
        [x for e in v for x in (e if isinstance(e, list) else [e])] if isinstance(v, list) else [v]
    ),
    # ``HAS(doc, "attr")`` — predicate-existence guard the visitor
    # emits for every variable-object BGP triple (``?s :p ?o``).
    # SPARQL §18.5 semantics: a required triple ``(s, p, o)`` only
    # matches when the predicate is actually present on the subject,
    # so missing-attribute documents must be excluded from the result.
    "HAS": lambda doc, attr: isinstance(doc, dict) and attr in doc,
    "LOWER": lambda v: None if v is None else str(v).lower(),
    "UPPER": lambda v: None if v is None else str(v).upper(),
    "LENGTH": lambda v: None if v is None else len(v),
    # ``REMOVE_VALUE(array, value)`` drops every element equal to ``value``;
    # the COUNT DISTINCT read (``LENGTH(REMOVE_VALUE(agg, null))``) uses it
    # to drop the null that UNIQUE keeps (issue #46).
    "REMOVE_VALUE": lambda arr, value: None if arr is None else [x for x in arr if x != value],
    "IS_STRING": lambda v: isinstance(v, str),
    "IS_NUMBER": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "IS_BOOL": lambda v: isinstance(v, bool),
    "true": True,
    "false": False,
    "null": None,
}


def _split_top_level_commas(s: str) -> list[str]:
    """Split *s* on commas at the top paren depth.

    Used to tear apart COLLECT key lists, AGGREGATE function lists, and
    RETURN projection bodies where a function argument may itself
    contain commas (``NOT_NULL(t.object_uri, t.object_value)``). Naive
    ``s.split(",")`` would mis-split those.
    """
    out: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(s[start:i].strip())
            start = i + 1
    out.append(s[start:].strip())
    return [p for p in out if p]


def _parse_collect_clause(
    rest: str,
) -> tuple[
    list[tuple[str, str]],  # keys: (alias, expression)
    list[tuple[str, str]],  # aggregates: (alias, "FUNC(arg)")
    str | None,  # count_into alias
]:
    """Parse the body of a ``COLLECT …`` line into structured pieces.

    Accepts (after the leading ``COLLECT`` keyword):
      * ``""``                                   → no-op COLLECT
      * ``"WITH COUNT INTO c"``                  → count_into = "c"
      * ``"k1 = e1, k2 = e2"``                   → keys list, no aggregates
      * ``"AGGREGATE a = f(e), b = g(e)"``       → aggregates list, no keys
      * combinations of the above.
    """
    keys: list[tuple[str, str]] = []
    aggregates: list[tuple[str, str]] = []
    count_into: str | None = None
    rest = rest.strip()
    if "AGGREGATE" in rest:
        head, _, agg_part = rest.partition("AGGREGATE")
        rest = head.strip()
        for chunk in _split_top_level_commas(agg_part):
            alias, _, expr = chunk.partition("=")
            aggregates.append((alias.strip(), expr.strip()))
    if "WITH COUNT INTO" in rest:
        head, _, count_alias = rest.partition("WITH COUNT INTO")
        rest = head.strip()
        count_into = count_alias.strip()
    if rest:
        for chunk in _split_top_level_commas(rest):
            alias, _, expr = chunk.partition("=")
            keys.append((alias.strip(), expr.strip()))
    return keys, aggregates, count_into


def _aggregate_apply(func: str, values: list[Any]) -> Any:
    """Apply an AQL aggregate function name to a list of row values.

    Mirrors ArangoDB, null handling included, and deliberately *not* SPARQL —
    the cross tests compare these results with pyoxigraph, so an interpreter
    that quietly applied SPARQL semantics would pass AQL that ArangoDB gets
    wrong (issue #46; #39 was the same blind spot). Checked on ArangoDB 3.12:

    * ``COUNT`` counts every value, null included; ``COUNT_DISTINCT`` counts
      null as one more distinct value; ``UNIQUE`` keeps null as a value.
    * ``MIN`` / ``MAX`` / ``AVG`` / ``SUM`` skip nulls; ``SUM`` over no rows
      is null (over only nulls it is 0).
    """
    non_null = [v for v in values if v is not None]
    if func == "COUNT":
        return len(values)
    if func == "COUNT_DISTINCT":
        return len(_unique(values))
    if func == "UNIQUE":
        return _unique(values)
    if func == "SUM":
        if not values:
            return None
        return sum(non_null) if non_null else 0
    if func == "AVG":
        return sum(non_null) / len(non_null) if non_null else None
    if func == "MIN":
        return min(non_null) if non_null else None
    if func == "MAX":
        return max(non_null) if non_null else None
    raise AssertionError(f"interpreter does not implement aggregate {func!r}")


def _aql_identity(value: Any) -> Any:
    """A hashable key that is equal exactly when AQL calls two values equal.

    Python's ``==`` makes ``True == 1``; AQL does not (``UNIQUE([1, true])`` is
    ``[1, true]`` on ArangoDB 3.12), while it does treat ``1`` and ``1.0`` as
    one number. Applied recursively, so arrays and objects compare the same way.
    """
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int | float):
        return ("number", float(value))
    if isinstance(value, list):
        return ("array", tuple(_aql_identity(v) for v in value))
    if isinstance(value, dict):
        return ("object", tuple(sorted((k, _aql_identity(v)) for k, v in value.items())))
    return (type(value).__name__, value)


def _unique(values: list[Any]) -> list[Any]:
    """Distinct values in first-seen order, by AQL equality (null is a value)."""
    seen: set[Any] = set()
    out: list[Any] = []
    for v in values:
        key = _aql_identity(v)
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


def _matching_delimiter(text: str, start: int, opening: str, closing: str) -> int:
    """Return the matching delimiter index, ignoring quoted contents."""

    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
    raise AssertionError(f"unterminated {opening!r} expression: {text!r}")


def _strip_outer_parentheses(expr: str) -> str:
    """Remove parentheses only when they enclose the complete expression."""

    stripped = expr.strip()
    while stripped.startswith("("):
        end = _matching_delimiter(stripped, 0, "(", ")")
        if end != len(stripped) - 1:
            break
        stripped = stripped[1:-1].strip()
    return stripped


def _split_top_level_order_comparison(expr: str) -> tuple[str, str, str] | None:
    """Split ``left OP right`` for an ordering OP (``<`` ``>`` ``<=`` ``>=``)
    at nesting depth 0, or return ``None``."""
    stripped = _strip_outer_parentheses(expr)
    depth = 0
    quote: str | None = None
    for index, char in enumerate(stripped):
        if quote is not None:
            if char == quote and stripped[index - 1] != "\\":
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif depth == 0 and char in "<>" and stripped[index - 1 : index] not in {"=", "!"}:
            op = stripped[index : index + 2] if stripped[index + 1 : index + 2] == "=" else char
            return stripped[:index].strip(), op, stripped[index + len(op) :].strip()
    return None


def _aql_order(left: Any, op: str, right: Any) -> bool:
    """AQL's ordering comparison: values of different types compare by type
    (null < bool < number < string < array < object), so ``null > 0`` is
    false and ``null < 0`` true, where Python raises TypeError."""

    def rank(value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, bool):
            return 1
        if isinstance(value, int | float):
            return 2
        if isinstance(value, str):
            return 3
        return 4 if isinstance(value, list) else 5

    if rank(left) != rank(right):
        cmp = (rank(left) > rank(right)) - (rank(left) < rank(right))
    else:
        cmp = (left > right) - (left < right)
    return {">": cmp > 0, "<": cmp < 0, ">=": cmp >= 0, "<=": cmp <= 0}[op]


def _split_top_level_ternary(expr: str) -> tuple[str, str, str] | None:
    """Split one AQL ``condition ? yes : no`` without touching nesting."""

    stripped = _strip_outer_parentheses(expr)
    depths = {"(": 0, "[": 0, "{": 0}
    pairs = {")": "(", "]": "[", "}": "{"}
    quote: str | None = None
    escaped = False
    question = -1
    nested_questions = 0
    for index, char in enumerate(stripped):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char in depths:
            depths[char] += 1
            continue
        if char in pairs:
            depths[pairs[char]] -= 1
            continue
        if any(depths.values()):
            continue
        if char == "?":
            if question < 0:
                question = index
            else:
                nested_questions += 1
        elif char == ":" and question >= 0:
            if nested_questions:
                nested_questions -= 1
            else:
                return (
                    stripped[:question].strip(),
                    stripped[question + 1 : index].strip(),
                    stripped[index + 1 :].strip(),
                )
    return None


def _eval_inline_scalar_subquery(
    body: str,
    env: dict[str, dict[str, Any]],
    bind_vars: dict[str, Any],
    let_env: dict[str, Any] | None,
) -> Any:
    """Evaluate the one-row ``FOR x IN [expr] … RETURN expr`` subset."""

    remaining = body.strip()
    local_let = dict(let_env or {})
    while remaining.startswith("FOR "):
        match = re.match(r"FOR\s+(\w+)\s+IN\s+\[", remaining)
        if match is None:
            raise AssertionError(f"unsupported scalar subquery FOR: {remaining!r}")
        alias = match.group(1)
        opening = match.end() - 1
        closing = _matching_delimiter(remaining, opening, "[", "]")
        source = remaining[opening + 1 : closing]
        local_let[alias] = _eval_expr(source, env, bind_vars, local_let)
        remaining = remaining[closing + 1 :].strip()
    if not remaining.startswith("RETURN "):
        raise AssertionError(f"scalar subquery has no RETURN: {body!r}")
    return _eval_expr(remaining[len("RETURN ") :], env, bind_vars, local_let)


def _replace_inline_scalar_subqueries(
    expr: str,
    env: dict[str, dict[str, Any]],
    bind_vars: dict[str, Any],
    let_env: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    """Replace emitted ``(FOR … RETURN …)[0]`` values with eval tokens."""

    rewritten = expr
    values: dict[str, Any] = {}
    search_from = 0
    while True:
        start = rewritten.find("(FOR ", search_from)
        if start < 0:
            break
        closing = _matching_delimiter(rewritten, start, "(", ")")
        if rewritten[closing + 1 : closing + 4] != "[0]":
            search_from = start + 1
            continue
        token = f"__scalar_subquery_{len(values)}"
        values[token] = _eval_inline_scalar_subquery(
            rewritten[start + 1 : closing],
            env,
            bind_vars,
            let_env,
        )
        rewritten = rewritten[:start] + token + rewritten[closing + 4 :]
        search_from = start + len(token)
    return rewritten, values


def _eval_expr(
    expr: str,
    env: dict[str, dict[str, Any]],
    bind_vars: dict[str, Any],
    let_env: dict[str, Any] | None = None,
) -> Any:
    """Evaluate an AQL value expression against the row environment.

    Performs a textual rewrite to a Python-compatible expression then
    ``eval``s it against a restricted namespace. Restrictions:

    - ``doc<N>.<attr>`` → ``doc<N>__<attr>`` (a flat namespace key).
    - ``@_p<N>`` → ``_p<N>`` (Python identifier).
    - ``&&`` → ``and``; ``||`` → ``or``; bare ``!`` → ``not ``.
    - ``null`` is mapped via the namespace to ``None``.

    LET-bound aliases (``bvN``) are valid Python identifiers and resolve
    via ``let_env`` without rewriting.

    Anything else (function calls, comparison operators, parentheses)
    is already valid Python. Returns the raw evaluated value — callers
    that need a boolean (FILTER) cast it themselves.
    """

    # ``_NullDefault`` makes missing ``doc<N>__<attr>`` lookups resolve
    # to ``None`` — that's AQL semantics: ``doc.foo`` on a doc without
    # ``foo`` returns null, never raises. Critical for OPTIONAL where
    # we project ``doc1.email`` against rows that have no ``email``.
    class _NullDefault(dict):
        def __missing__(self, key: str) -> Any:
            if _DOC_ATTR_NAMESPACE_RE.match(key):
                return None
            raise NameError(f"name {key!r} is not defined")

    expr, scalar_values = _replace_inline_scalar_subqueries(
        expr.strip(),
        env,
        bind_vars,
        let_env,
    )
    scoped_let = {**(let_env or {}), **scalar_values}
    if ternary := _split_top_level_ternary(expr):
        condition, when_true, when_false = ternary
        branch = when_true if _eval_expr(condition, env, bind_vars, scoped_let) else when_false
        return _eval_expr(branch, env, bind_vars, scoped_let)

    namespace: dict[str, Any] = _NullDefault(_AQL_BUILTINS)
    for alias, doc in env.items():
        # Register the doc dict under its bare alias so HAS(doc1, "attr")
        # and similar dict-accepting builtins resolve correctly. The
        # flat ``<alias>__<attr>`` shape stays the primary access path
        # because the rest of the rewriter expects it (``doc1.foo`` →
        # ``doc1__foo``).
        namespace[alias] = doc
        # The cross-subject OPTIONAL FOR-inline binds ``optrowN`` to
        # ``null`` for the padded no-match row; there are no attributes
        # to flatten, and any ``optrowN.<f>`` read falls through to the
        # ``_NullDefault`` (→ ``None``, SPARQL "unbound").
        if isinstance(doc, dict):
            for attr, value in doc.items():
                namespace[f"{alias}__{attr}"] = value
    namespace.update(bind_vars)
    namespace.update(scoped_let)
    py = _TERNARY_RE.sub(r"((\2) if (\1) else \3)", expr)
    py = _QUOTED_ATTR_RE.sub(lambda m: f"_aql_attr({m.group(1)}, {m.group(2)!r})", py)
    py = _DOC_ATTR_RE.sub(_bare_attr, py)
    py = _BIND_RE.sub(lambda m: m.group(1), py)
    py = py.replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"!(?=\()", " not ", py)
    # AQL membership operators (uppercase keywords) → Python's. Emitted
    # by the fan-out membership filter (``@v IN attr`` / value joins)
    # and the variable-predicate system-attribute skip (``k NOT IN @s``).
    py = re.sub(r"\bNOT IN\b", " not in ", py)
    py = re.sub(r"\bIN\b", " in ", py)
    try:
        return eval(py, {"__builtins__": {}}, namespace)  # noqa: S307 - test-only
    except TypeError:
        # Python cannot order None against a number; AQL can (null sorts
        # first). Emitted by the aggregate error read ``(nulls > 0 ? …)``,
        # whose count is null over no rows (issue #59).
        parts = _split_top_level_order_comparison(expr)
        if parts is None:
            raise
        left, op, right = parts
        return _aql_order(
            _eval_expr(left, env, bind_vars, scoped_let), op, _eval_expr(right, env, bind_vars, scoped_let)
        )


def _eval_filter(
    expr: str,
    env: dict[str, dict[str, Any]],
    bind_vars: dict[str, Any],
    let_env: dict[str, Any] | None = None,
) -> bool:
    return bool(_eval_expr(expr, env, bind_vars, let_env))


def _split_sort_keys(clause: str) -> list[tuple[str, str]]:
    """Split ``"a DESC, b ASC"`` into ``[("a", "DESC"), ("b", "ASC")]``.

    Top-level-comma splitting (not a naive ``split(",")``) so a sort key
    that is itself a function call with comma-separated arguments —
    ``NOT_NULL(t.object_uri, t.object_value) DESC`` under the RPT model
    — is kept whole. The trailing whitespace-delimited token is the
    direction; everything before it is the (possibly parenthesised)
    expression.
    """
    out: list[tuple[str, str]] = []
    for raw in _split_top_level_commas(clause):
        head, _, direction = raw.rpartition(" ")
        if direction not in ("ASC", "DESC"):
            raise AssertionError(f"sort key missing ASC/DESC: {raw!r}")
        out.append((head.strip(), direction))
    return out


def _read_block(
    lines: list[str], i: int, into: list[str], opener: re.Pattern[str], closer: re.Pattern[str]
) -> int:
    """Copy the lines of one subquery block into ``into``; return the index of
    its closing line (or ``len(lines)`` when unterminated).

    Depth-aware: a block of the same kind nested inside it — the OPTIONAL
    ``LET optsub = (…)`` inside a grouped query's ``LET results = (…)``
    (issue #48) — keeps its own closer instead of ending the outer block.
    """
    depth = 0
    while i < len(lines):
        line = lines[i]
        if opener.match(line):
            depth += 1
        elif closer.match(line):
            if depth == 0:
                return i
            depth -= 1
        into.append(line)
        i += 1
    return i


def run_aql_subset(
    aql: str,
    bind_vars: dict[str, Any],
    docs: dict[str, list[dict[str, Any]]],
    *,
    _outer_env: dict[str, Any] | None = None,
    _outer_let: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute the FOR/FILTER/LET/COLLECT/SORT/LIMIT/RETURN subset of
    AQL the visitor emits today against an in-memory document store.

    The pre-COLLECT region runs as a source-ordered pipeline: each FOR
    opens a loop and each FILTER / LET executes at its position, so a
    shared-variable equality FILTER prunes the loop level it sits under
    instead of materialising a full Cartesian product first. This is
    what keeps an RPT multi-triple self-join (one FOR per triple, joined
    on ``subject_uri``) tractable — a naive product would be
    O(rows ** triples).

    LET (BIND) bindings are evaluated in source order and appear as
    plain identifiers in subsequent FILTER / SORT / RETURN expressions —
    same scoping rules as AQL.

    Correlated subqueries (``LET p = LENGTH((<inner>))``, the MINUS /
    EXISTS probe shape) execute the inner query per outer row with the
    outer environment in scope; ``_outer_env`` / ``_outer_let`` carry
    that scope into the recursive call and are private to it.
    """
    # Strip each line so a nested (indented) probe subquery re-parses
    # cleanly — the line-anchored regexes below expect no leading space.
    lines = [line.strip() for line in aql.splitlines() if line.strip()]
    # The pre-COLLECT region is parsed into an ordered pipeline of
    # ``("FOR", alias, collection)`` / ``("FILTER", expr, None)`` /
    # ``("LET", expr, alias)`` ops *preserving source order*. Executing
    # it as a nested pipeline (rather than building the full FOR
    # Cartesian product and only then filtering) pushes each FILTER
    # down to the moment its referenced docs are bound — matching how a
    # real AQL plan prunes, and the only thing that keeps an RPT
    # multi-triple self-join (one FOR per triple) from exploding
    # combinatorially. The visitor always emits a FILTER after the FORs
    # that bind its variables, so source-order execution never
    # references an unbound alias.
    # Plan-op payloads are ``str | None`` for FOR/FILTER/LET and a
    # ``(v_alias, e_alias, start_alias)`` tuple for OUTBOUND, so the
    # element type is widened to ``Any``.
    pre_collect_plan: list[tuple[str, Any, Any]] = []
    collect_keys: list[tuple[str, str]] | None = None
    collect_aggregates: list[tuple[str, str]] | None = None
    collect_count_into: str | None = None
    post_collect_steps: list[tuple[str, str, str | None]] = []
    sort_keys: list[tuple[str, str]] = []
    limit: tuple[int, int] | None = None
    return_distinct = False
    return_pairs: list[tuple[str, str]] = []
    return_scalar_expr: str | None = None
    seen_collect = False

    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if _WITH_RE.match(line):
            # ``WITH @@c1, @@c2`` collection prelude — no-op for the
            # interpreter; the FOR lines that follow carry the bind
            # vars we actually resolve against.
            continue
        if m := _LEN_SUBQUERY_START_RE.match(line):
            # ``LET <alias> = LENGTH((`` … ``))`` — consume the inner
            # block up to the closing ``))`` and stash it as a correlated
            # row-count op evaluated per outer row at run time.
            alias = m.group(1)
            inner: list[str] = []
            i = _read_block(lines, i, inner, _LEN_SUBQUERY_START_RE, _SUBQUERY_END_RE)
            if i >= len(lines):  # pragma: no cover - malformed AQL
                raise AssertionError("unterminated LENGTH(( subquery in AQL")
            i += 1  # skip the closing ``))``
            sub_step: tuple[str, Any, Any] = ("SUBQUERY_LEN", alias, "\n".join(inner))
            (post_collect_steps if seen_collect else pre_collect_plan).append(sub_step)
            continue
        if m := _ROWS_SUBQUERY_START_RE.match(line):
            # ``LET <alias> = (`` … ``)`` — consume the inner block up to
            # the closing single ``)`` and stash it as a correlated
            # row-LIST op (the cross-subject OPTIONAL scan). At run time
            # the inner query executes against the outer row and the
            # alias binds to the resulting list of row dicts.
            alias = m.group(1)
            rows_inner: list[str] = []
            i = _read_block(lines, i, rows_inner, _ROWS_SUBQUERY_START_RE, _SUBQUERY_PAREN_END_RE)
            if i >= len(lines):  # pragma: no cover - malformed AQL
                raise AssertionError("unterminated LET = ( subquery in AQL")
            i += 1  # skip the closing ``)``
            rows_step: tuple[str, Any, Any] = (
                "SUBQUERY_ROWS",
                alias,
                "\n".join(rows_inner),
            )
            (post_collect_steps if seen_collect else pre_collect_plan).append(rows_step)
            continue
        if m := _FOR_TRAVERSAL_RE.match(line):
            if seen_collect:  # pragma: no cover
                raise AssertionError(f"FOR after COLLECT not supported: {line!r}")
            v_alias, e_alias, start_alias, edge_var = m.groups()
            pre_collect_plan.append(("OUTBOUND", (v_alias, e_alias, start_alias), bind_vars[f"@{edge_var}"]))
        elif m := _FOR_RE.match(line):
            if seen_collect:  # pragma: no cover
                raise AssertionError(f"FOR after COLLECT not supported: {line!r}")
            alias, coll_var = m.groups()
            pre_collect_plan.append(("FOR", alias, bind_vars[f"@{coll_var}"]))
        elif m := _FOR_VALUES_RE.match(line):
            if seen_collect:  # pragma: no cover
                raise AssertionError(f"FOR after COLLECT not supported: {line!r}")
            alias, values_var = m.groups()
            pre_collect_plan.append(("FOR_VALUES", alias, bind_vars[values_var]))
        elif m := _FOR_INLINE_RE.match(line) or _FOR_ARRAY_RE.match(line):
            if seen_collect:  # pragma: no cover
                raise AssertionError(f"FOR after COLLECT not supported: {line!r}")
            alias, inline_expr = m.groups()
            pre_collect_plan.append(("FOR_INLINE", alias, inline_expr))
        elif m := _FOR_CALL_RE.match(line):
            if seen_collect:  # pragma: no cover
                raise AssertionError(f"FOR after COLLECT not supported: {line!r}")
            alias, call_expr = m.groups()
            pre_collect_plan.append(("FOR_INLINE", alias, call_expr))
        elif m := _FOR_LET_RE.match(line):
            if seen_collect:  # pragma: no cover
                raise AssertionError(f"FOR after COLLECT not supported: {line!r}")
            # Iterates the list a preceding ``LET <alias> = (…)`` bound;
            # the identifier evaluates to that list like any LET alias.
            alias, let_alias = m.groups()
            pre_collect_plan.append(("FOR_INLINE", alias, let_alias))
        elif _COLLECT_RE.match(line):
            if seen_collect:  # pragma: no cover
                raise AssertionError(f"second COLLECT not supported: {line!r}")
            collect_keys, collect_aggregates, collect_count_into = _parse_collect_clause(
                line[len("COLLECT") :]
            )
            seen_collect = True
        elif m := _LET_RE.match(line):
            alias, expr = m.groups()
            step: tuple[str, str, str | None] = ("LET", expr.strip(), alias)
            (post_collect_steps if seen_collect else pre_collect_plan).append(step)
        elif m := _FILTER_RE.match(line):
            fstep: tuple[str, str, str | None] = ("FILTER", m.group(1).strip(), None)
            (post_collect_steps if seen_collect else pre_collect_plan).append(fstep)
        elif m := _SORT_RE.match(line):
            sort_keys.extend(_split_sort_keys(m.group(1).strip()))
        elif m := _LIMIT_RE.match(line):
            offset, count = m.groups()
            limit = (int(offset or "0"), int(count))
        elif m := _RETURN_RE.match(line):
            return_distinct = bool(m.group(1))
            # Projection values can be bare doc attributes
            # (``doc1.name``), LET aliases (``bv1``), or full
            # expressions (``NOT_NULL(doc2.object_uri,
            # doc2.object_value)`` under RPT). Split on top-level
            # commas so a function-call value's internal comma does not
            # tear the pair apart, then partition each pair on its
            # *first* colon (an expression never contains a top-level
            # colon in our emitted AQL — the only colon is the
            # key/value separator).
            for pair in _split_top_level_commas(m.group(2)):
                return_pairs.append(_object_pair(pair))
        elif m := _RETURN_SCALAR_RE.match(line):
            # ``RETURN 1`` (probe subquery) — a single scalar column.
            # Only the row *count* matters to the enclosing LENGTH((…)),
            # so the value is projected under a fixed key.
            return_scalar_expr = m.group(1).strip()
        else:  # pragma: no cover
            raise AssertionError(f"interpreter cannot handle AQL line: {line!r}")

    # ``_id`` → document index across every collection, so an OUTBOUND
    # edge's ``_to`` handle (``"Person/alice"``) resolves to the target
    # vertex regardless of which vertex collection holds it — exactly
    # how ArangoDB resolves a traversal endpoint by document handle.
    id_index: dict[Any, dict[str, Any]] = {
        d["_id"]: d for coll_docs in docs.values() for d in coll_docs if isinstance(d, dict) and "_id" in d
    }

    envs: list[tuple[dict[str, dict[str, Any]], dict[str, Any]]] = []

    def run_plan(idx: int, env: dict[str, dict[str, Any]], let_env: dict[str, Any]) -> None:
        if idx == len(pre_collect_plan):
            envs.append((env, dict(let_env)))
            return
        kind, a, b = pre_collect_plan[idx]
        if kind == "FOR":
            alias, collection = a, b
            assert collection is not None
            for doc in docs.get(collection, []):
                run_plan(idx + 1, {**env, alias: doc}, let_env)
        elif kind == "FOR_VALUES":
            # ``FOR <alias> IN @_pN_values`` — SPARQL VALUES rows (or
            # the federation entry point's seed bindings) bound as a
            # list of row dicts. Iterate them like a tiny collection.
            alias = a
            for row in b or []:
                run_plan(idx + 1, {**env, alias: row}, let_env)
        elif kind == "FOR_INLINE":
            # ``FOR <alias> IN (<expr>)`` — the expr evaluates to a list
            # (the ``[null]``-padded OPTIONAL subquery result). Iterate
            # it, binding each element (a row dict, or ``None`` for the
            # padded no-match row) under <alias>.
            alias = a
            sequence = _eval_expr(b, env, bind_vars, let_env)
            for element in sequence or []:
                run_plan(idx + 1, {**env, alias: element}, let_env)
        elif kind == "OUTBOUND":
            v_alias, e_alias, start_alias = a
            edge_collection = b
            start_doc = env.get(start_alias)
            start_id = start_doc.get("_id") if isinstance(start_doc, dict) else None
            if start_id is None:  # pragma: no cover - start always bound by a prior FOR
                return
            for edge in docs.get(edge_collection, []):
                if not isinstance(edge, dict) or edge.get("_from") != start_id:
                    continue
                target = id_index.get(edge.get("_to"))
                if target is None:  # dangling edge — no endpoint to bind
                    continue
                run_plan(idx + 1, {**env, v_alias: target, e_alias: edge}, let_env)
        elif kind == "FILTER":
            if _eval_filter(a, env, bind_vars, let_env):
                run_plan(idx + 1, env, let_env)
        elif kind == "SUBQUERY_LEN":
            # Correlated probe: run the inner query with this outer row's
            # environment in scope, bind <alias> to its row count.
            inner_rows = run_aql_subset(b, bind_vars, docs, _outer_env=env, _outer_let=let_env)
            run_plan(idx + 1, env, {**let_env, a: len(inner_rows)})
        elif kind == "SUBQUERY_ROWS":
            # Correlated row-LIST subquery (cross-subject OPTIONAL scan):
            # run the inner query with this outer row in scope and bind
            # <alias> to the resulting list of row dicts so the following
            # FOR-inline can iterate (and ``[null]``-pad) it.
            inner_rows = run_aql_subset(b, bind_vars, docs, _outer_env=env, _outer_let=let_env)
            run_plan(idx + 1, env, {**let_env, a: inner_rows})
        else:  # LET
            assert b is not None
            run_plan(idx + 1, env, {**let_env, b: _eval_expr(a, env, bind_vars, let_env)})

    run_plan(0, dict(_outer_env or {}), dict(_outer_let or {}))

    if seen_collect:
        keys = collect_keys or []
        aggregates = collect_aggregates or []
        groups: dict[tuple[Any, ...], list[tuple[dict, dict]]] = {}
        for env, let_env in envs:
            key_tuple = tuple(_eval_expr(expr, env, bind_vars, let_env) for _, expr in keys)
            groups.setdefault(key_tuple, []).append((env, let_env))
        if not keys and not groups:
            # A COLLECT with no group keys returns one row even over no input
            # (WITH COUNT INTO → 0, SUM → null), as ArangoDB does.
            groups[()] = []
        new_envs: list[tuple[dict, dict]] = []
        try:
            sorted_keys = sorted(groups.keys())
        except TypeError:
            sorted_keys = list(groups.keys())
        for key_tuple in sorted_keys:
            members = groups[key_tuple]
            new_let: dict[str, Any] = {}
            for (alias, _), value in zip(keys, key_tuple, strict=True):
                new_let[alias] = value
            for alias, agg_expr in aggregates:
                func, _, paren_arg = agg_expr.partition("(")
                # Drop exactly the aggregate's own closing paren: rstrip(")")
                # also ate a nested call's, so COUNT(NOT_NULL(a, b)) became the
                # unbalanced "NOT_NULL(a, b" (#18 subject identity).
                arg = paren_arg[:-1] if paren_arg.endswith(")") else paren_arg
                values = [_eval_expr(arg, env, bind_vars, let_env) for env, let_env in members]
                new_let[alias] = _aggregate_apply(func.strip(), values)
            if collect_count_into is not None:
                new_let[collect_count_into] = len(members)
            new_envs.append(({}, new_let))
        envs = new_envs

        filtered_envs: list[tuple[dict, dict]] = []
        for env, let_env in envs:
            keep = True
            for kind, field_a, field_b in post_collect_steps:
                if kind == "FILTER":
                    if not _eval_filter(field_a, env, bind_vars, let_env):
                        keep = False
                        break
                elif kind == "SUBQUERY_LEN":
                    # Stored as ("SUBQUERY_LEN", alias, inner_aql).
                    inner_rows = run_aql_subset(field_b, bind_vars, docs, _outer_env=env, _outer_let=let_env)
                    let_env[field_a] = len(inner_rows)
                elif kind == "SUBQUERY_ROWS":
                    # Stored as ("SUBQUERY_ROWS", alias, inner_aql) — bind
                    # the alias to the full row list (not a count).
                    let_env[field_a] = run_aql_subset(
                        field_b, bind_vars, docs, _outer_env=env, _outer_let=let_env
                    )
                else:  # LET — stored as ("LET", expr, alias)
                    assert field_b is not None
                    let_env[field_b] = _eval_expr(field_a, env, bind_vars, let_env)
            if keep:
                filtered_envs.append((env, let_env))
        envs = filtered_envs

    if sort_keys:
        for expr, direction in reversed(sort_keys):
            envs.sort(
                key=lambda pair, e=expr: _eval_expr(e, pair[0], bind_vars, pair[1]),
                reverse=(direction == "DESC"),
            )

    rows: list[dict[str, Any]] = []
    for env, let_env in envs:
        row: dict[str, Any] = {}
        if return_scalar_expr is not None:
            # Probe subquery: one scalar column per row. Only the row
            # count matters to the enclosing LENGTH((…)); object-valued
            # scalar returns are the grouped-empty fallback's passthrough
            # row and must retain their original projection keys.
            scalar = _eval_expr(return_scalar_expr, env, bind_vars, let_env)
            if isinstance(scalar, dict):
                row.update(scalar)
            else:
                row["_scalar"] = scalar
        else:
            for key, value_expr in return_pairs:
                row[key] = _eval_expr(value_expr, env, bind_vars, let_env)
        rows.append(row)

    # AQL order, not SPARQL order: a LIMIT at this level runs before the
    # RETURN that ends it, so ``LIMIT n RETURN DISTINCT`` slices first and
    # de-duplicates the slice. Applying DISTINCT first (as this did) made
    # the interpreter evaluate that AQL the way SPARQL would, so the cross
    # tests passed on exactly the translation that was wrong (issue #39).
    if limit is not None:
        offset, count = limit
        rows = rows[offset : offset + count]
    if return_distinct:
        seen: set[tuple] = set()
        deduped: list[dict[str, Any]] = []
        for r in rows:
            dedup_key = tuple(sorted(r.items()))
            if dedup_key not in seen:
                seen.add(dedup_key)
                deduped.append(r)
        rows = deduped
    return rows
