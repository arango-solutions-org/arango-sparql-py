"""Expressions that are certainly an error, decided at translation time.

Split out of :mod:`visitor` (over its size cap), as :mod:`filter_builtins` is.

A variable no pattern in scope binds is unbound in every solution, and
reading its value is an error in SPARQL (§17.2): ``FILTER`` drops the row,
``BIND`` leaves its target unbound. The translator emits such a variable as
AQL ``null``, but AQL does not propagate ``null`` the way SPARQL propagates
an error: ``null != "A"`` is true, ``null + 1`` is 1 and ``CONCAT(null, "!")``
is ``"!"``. So an expression that needs the value of such a variable is
recognised here and emitted as ``null`` as a whole (issue #125: an OPTIONAL
body's ``BIND(CONCAT(?n, "!") AS ?v)`` with ``?n`` bound only outside it).

Only what is certain is decided. ``BOUND``, ``COALESCE``, ``IF`` and
``EXISTS`` read an unbound argument without an error; ``&&`` and ``||`` are
an error only when every operand is (``error || true`` is true, ``error &&
false`` is false), so a partial one is left to the normal translation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rdflib import Variable
from rdflib.term import Identifier

#: Functions that read an unbound argument without raising (§17.4.1).
_ERROR_TOLERANT = frozenset({"Builtin_BOUND", "Builtin_EXISTS", "Builtin_NOTEXISTS"})
_ALL_OPERANDS = frozenset({"ConditionalAndExpression", "ConditionalOrExpression", "Builtin_COALESCE"})
_METADATA_KEYS = frozenset({"_vars", "datatype", "lang"})


def certainly_error(node: Any, in_scope: Callable[[str], bool]) -> bool:
    """Whether *node* is an error in every solution, because it needs the
    value of a variable for which ``in_scope`` is false."""
    if isinstance(node, Variable):
        return not in_scope(str(node))
    name = getattr(node, "name", None)
    if not isinstance(name, str):
        return False
    if name in _ERROR_TOLERANT:
        return False
    if name == "Builtin_IF":
        if certainly_error(node.get("arg1"), in_scope):
            return True
        return certainly_error(node.get("arg2"), in_scope) and certainly_error(node.get("arg3"), in_scope)
    operands = list(_operands(node))
    if name in _ALL_OPERANDS:
        return bool(operands) and all(certainly_error(o, in_scope) for o in operands)
    if name == "RelationalExpression" and node.get("op") in ("IN", "NOT IN"):
        # A list member that is an error only makes the result an error when
        # nothing else matches; the tested value always does.
        return certainly_error(node.get("expr"), in_scope)
    return any(certainly_error(o, in_scope) for o in operands)


def unbound_variables(node: Any, in_scope: Callable[[str], bool]) -> list[str]:
    """The variables *node* reads for which ``in_scope`` is false, in order."""
    if isinstance(node, Variable):
        return [] if in_scope(str(node)) else [str(node)]
    out: list[str] = []
    if isinstance(getattr(node, "name", None), str):
        for operand in _operands(node):
            out.extend(v for v in unbound_variables(operand, in_scope) if v not in out)
    return out


def _operands(node: Any) -> Any:
    """The sub-expressions of a rdflib expression node."""
    for key, value in node.items():
        if key in _METADATA_KEYS:
            continue
        for item in value if isinstance(value, list) else (value,):
            # Terms (a variable, an IRI, a literal) and sub-expressions; a
            # literal is an operand too: ``COALESCE(?z, "x")`` is no error.
            if isinstance(item, Identifier) or isinstance(getattr(item, "name", None), str):
                yield item
