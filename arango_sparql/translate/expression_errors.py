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


#: Functions that never raise, whatever their arguments: BOUND reads an
#: unbound variable as false (§17.4.1.1).
_NEVER_ERROR = frozenset({"Builtin_BOUND"})

#: Functions whose AQL translation is never null, whatever its input: an
#: error there cannot leave the target unbound in the emitted query (it is
#: not represented at all, #82), so marking the target possibly-unbound
#: would change no answer and only cost the join its index (review of #236).
#: Checked on ArangoDB 3.12: ``CONCAT(null)`` and ``TO_STRING(null)`` are
#: ``""``, ``LENGTH(null)`` and ``ABS(null)`` 0, ``MD5(null)`` a hash.
_AQL_NEVER_NULL = frozenset(
    {
        "Builtin_CONCAT",
        "Builtin_STR",
        "Builtin_IRI",
        "Builtin_UCASE",
        "Builtin_LCASE",
        "Builtin_STRAFTER",
        "Builtin_STRBEFORE",
        "Builtin_SUBSTR",
        "Builtin_STRLEN",
        "Builtin_ENCODE_FOR_URI",
        "Builtin_REPLACE",
        "Builtin_MD5",
        "Builtin_SHA1",
        "Builtin_SHA256",
        "Builtin_SHA384",
        "Builtin_SHA512",
        "Builtin_ABS",
        "Builtin_STRUUID",
        "Builtin_UUID",
        "Builtin_NOW",
    }
)
#: XSD constructor casts emitted as ``TO_NUMBER`` / ``TO_STRING`` / ``TO_BOOL``.
_XSD = "http://www.w3.org/2001/XMLSchema#"
_AQL_NEVER_NULL_CASTS = frozenset(
    _XSD + name for name in ("integer", "decimal", "double", "float", "string", "boolean", "dateTime")
)


def can_error(node: Any) -> bool:
    """Whether *node* can be an error in some solution that the emitted AQL
    represents as ``null``, leaving a BIND or projection target unbound
    there (§18.5, Extend; #226).

    Conservative: a constant and a plain variable never error, nor does
    ``BOUND``, nor a ``COALESCE`` whose last operand cannot. A function whose
    AQL is never null (``_AQL_NEVER_NULL``, the XSD casts) is not counted
    either. Anything else can: arithmetic (a division by zero, a non-numeric
    operand), an ``IF`` whose condition errors, ``ROUND`` / ``CEIL`` /
    ``FLOOR`` of a non-number. A target that can be unbound is joined
    compatibly (either side unbound, or equal) rather than by value.
    """
    if isinstance(node, (Variable, Identifier)):
        return False
    name = getattr(node, "name", None)
    if not isinstance(name, str):
        return False
    if name in _NEVER_ERROR or name in _ERROR_TOLERANT or name in _AQL_NEVER_NULL:
        return False
    if name == "Function" and str(node.get("iri")) in _AQL_NEVER_NULL_CASTS:
        return False
    if name == "Builtin_COALESCE":
        operands = list(_operands(node))
        return not operands or can_error(operands[-1])
    return True
