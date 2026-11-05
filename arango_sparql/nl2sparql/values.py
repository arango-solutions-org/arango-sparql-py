"""Filter values the data never holds — an opt-in check and prompt section.

A generated query that compares a property with a string the property never
holds parses, translates and runs, then returns no rows or, under an
aggregate, a confident ``0``. Neither ``validate`` nor the guardrails see it.
This module gives a caller two opt-in halves, both fed with the property
values the caller read from its data:

* :func:`values_prompt_section` lists each categorical property's stored
  values, so the first candidate can use the stored form. Place it right
  after the grammar section (where it was measured), or let
  :class:`ValueCheck` render it via ``announce_values=True``;
* :class:`ValueCheck` is a **soft** postcondition (PRD §7.3.1): a literal
  the property does not hold draws one repair whose hint lists the values.
  It never refuses — a literal that matches nothing can be the question's own
  premise ("contracts with status X" when there are none).

**Values must be the asker's.** The section lists the values it is given, the
repair hint lists the nearest ones, and whether a literal is flagged at all
tells whether a value exists. A caller with row-level access control must
build values per call from rows the asker may read, or leave those
properties out. A list built over all rows discloses values that occur only
in rows the asker cannot see.

Imported explicitly (not re-exported from :mod:`arango_sparql.nl2sparql`) so
the package stays import-safe without the ``nl`` extra, like
:mod:`.postconditions`.
"""

from __future__ import annotations

import builtins
import difflib
import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from arango_query_core.nl import PostconditionContext, PostconditionViolation
from rdflib.namespace import XSD
from rdflib.term import Literal, URIRef, Variable

from ..translate.parser import parse_sparql
from ..translate.resolver import local_name

__all__ = [
    "CATEGORICAL_MAX",
    "PropertyValues",
    "ValueCheck",
    "ValueFlag",
    "by_property",
    "sparql_literal_uses",
    "value_flags",
    "values_prompt_section",
]

logger = logging.getLogger(__name__)

#: A property is categorical with at most this many distinct string values.
CATEGORICAL_MAX = 20
#: A hint lists every value of a property with at most this many; past it,
#: the nearest :data:`HINT_NEAREST`.
HINT_ALL_MAX = 20
HINT_NEAREST = 8
#: Longest value or name shown in a prompt, in characters (query-core's label cap).
TEXT_MAX_LEN = 200
#: The value kinds the check understands. A property reporting any other kind
#: (a date, a list, …) is never flagged and never listed: comparing a literal
#: with such values by their text would call real matches absent.
KINDS = frozenset({"str", "int", "float", "bool"})
_NON_TEXT_KINDS = frozenset({"int", "float", "bool"})

VALUES_HEADER = "## Values as the data stores them"
#: The measured wording. "Only these properties take quoted values" reads as
#: if unlisted string properties took none; measured over five runs it
#: unquoted no name or date, and it took quoted booleans from 4 to 0.
VALUES_INSTRUCTION = (
    "Each property below holds only the values listed. When a filter compares one of these "
    "properties with a value, write the listed value the question means exactly as shown: "
    "the data is matched exactly, including case. Only these properties take quoted values; "
    "compare numbers and booleans unquoted. If the question names a value that is not "
    "listed, keep the question's value; no record holds it."
)
KEEP_IF_ABSENT = (
    "Use the stored value the question means, written exactly as shown. If the question names "
    "a value that is not among them, return the same query: no record holds that value."
)

_CONTROL_CHARS_RE = re.compile("[\x00-\x1f\x7f-\x9f  ]")
_WHITESPACE_RUN_RE = re.compile(r"\s+")
#: Literal datatypes that count as plain strings.
_STRING_TYPES = frozenset({None, XSD.string})
_MAX_DEPTH = 200


def _clean(text: str) -> str:
    """One line, control characters removed, at most :data:`TEXT_MAX_LEN`."""
    cleaned = _WHITESPACE_RUN_RE.sub(" ", _CONTROL_CHARS_RE.sub(" ", text)).strip()
    return cleaned[:TEXT_MAX_LEN]


def _no_heading(text: str) -> str:
    """Escape a ``##`` run, so prompt text can never start a ``## …`` heading
    (which would also move the prompt's cache split). A single ``#`` stays."""
    return text.replace("##", "#\\u0023")


def _name(text: str) -> str:
    return _no_heading(_clean(text))


def _showable(value: str) -> bool:
    """A value the prompt can show exactly as stored: one line, no control
    characters or whitespace runs, within :data:`TEXT_MAX_LEN`. Any other
    value is left out of the section and hints — a cleaned copy would teach
    the model a form the data does not hold."""
    return value == _clean(value) and value != ""


def _quote(value: str) -> str:
    return _no_heading(json.dumps(value, ensure_ascii=False))


def _key(prop: str) -> str:
    """The lookup key: the translator's own local name of ``prop``."""
    key = local_name(prop.strip())
    if not key or ":" in key or "/" in key:
        raise ValueError(
            f"PropertyValues.property must be a local name or a full IRI ending in one, got {prop!r}"
        )
    return key


@dataclass(frozen=True)
class PropertyValues:
    """One property's values, as the caller read them from its data.

    ``values`` must be the property's **complete** distinct text forms: a
    sample would make the check call real values absent. ``kinds`` are the
    Python type names of the raw values (only :data:`KINDS` are checked); ``rows`` is how many rows
    hold a value. ``property`` is a local name or a full IRI (a prefixed name
    such as ``ex:status`` is rejected: its prefix is unknown here).
    """

    entity: str
    property: str
    values: tuple[str, ...]
    kinds: tuple[str, ...]
    rows: int

    def __post_init__(self) -> None:
        _key(self.property)
        if self.rows < 0:
            raise ValueError(f"rows must be >= 0 for {self.property!r}, got {self.rows}")
        object.__setattr__(self, "values", tuple(str(v) for v in self.values))
        object.__setattr__(self, "kinds", tuple(self.kinds))

    @builtins.property  # the ``property`` field shadows the builtin in this class
    def key(self) -> str:
        return _key(self.property)

    @builtins.property
    def checkable(self) -> bool:
        """Every reported kind is one the check understands (:data:`KINDS`)."""
        return bool(self.kinds) and set(self.kinds) <= KINDS

    def categorical(self, maximum: int = CATEGORICAL_MAX) -> bool:
        """Strings only, 1..``maximum`` distinct values, and repeated (fewer
        distinct values than rows). Unique columns — names, ids — are not
        categories; label grounding covers those."""
        return (
            set(self.kinds) == {"str"} and 1 <= len(self.values) <= maximum and len(self.values) < self.rows
        )


def by_property(values: Iterable[PropertyValues]) -> dict[str, PropertyValues]:
    """The lookup: one entry per property local name, merged across entities.

    A query's literal names the property, not its class, so a value held by
    the property on any entity counts as held."""
    out: dict[str, PropertyValues] = {}
    for pv in values:
        prev = out.get(pv.key)
        if prev is None:
            out[pv.key] = pv
            continue
        out[pv.key] = PropertyValues(
            entity=prev.entity,
            property=pv.key,
            values=tuple(sorted(set(prev.values) | set(pv.values))),
            kinds=tuple(sorted(set(prev.kinds) | set(pv.kinds))),
            rows=prev.rows + pv.rows,
        )
    return out


def values_prompt_section(values: Sequence[PropertyValues], *, categorical_max: int = CATEGORICAL_MAX) -> str:
    """One line per categorical property, ``- Entity.property: "a", "b"``, in
    the order given, under a fixed instruction. Values that cannot be shown
    exactly (:func:`_showable`) are left out. ``""`` when no property
    qualifies, so the prompt is unchanged."""
    lines = []
    for pv in values:
        shown = [v for v in pv.values if _showable(v)]
        if not pv.categorical(categorical_max) or not shown:
            continue
        entity = _name(pv.entity)
        lines.append(
            f"- {entity + '.' if entity else ''}{_name(pv.key)}: " + ", ".join(_quote(v) for v in shown)
        )
    if not lines:
        return ""
    return "\n".join([VALUES_HEADER, VALUES_INSTRUCTION, "", *lines])


# ---------------------------------------------------------------------------
# Finding the literals a query compares properties with
# ---------------------------------------------------------------------------


def _is_string_literal(term: object) -> bool:
    return isinstance(term, Literal) and term.datatype in _STRING_TYPES


def _equalities(expr: object, depth: int = 0) -> list[tuple[Variable, Literal]]:
    """``(variable, literal)`` for each ``=`` between a variable and a plain
    string literal in a FILTER expression, either side."""
    out: list[tuple[Variable, Literal]] = []
    if expr is None or depth > _MAX_DEPTH:
        return out
    if isinstance(expr, (list, tuple)):
        for item in expr:
            out.extend(_equalities(item, depth + 1))
        return out
    if not isinstance(expr, dict):
        return out
    if getattr(expr, "name", None) == "RelationalExpression" and expr.get("op") == "=":
        other = expr.get("other")
        right = other[0] if isinstance(other, list) and len(other) == 1 else other
        for var, lit in ((expr.get("expr"), right), (right, expr.get("expr"))):
            if isinstance(var, Variable) and isinstance(lit, Literal) and _is_string_literal(lit):
                out.append((var, lit))
        return out
    for key, child in expr.items():
        if key != "_vars" and getattr(child, "name", None) not in ("Exists", "NotExists"):
            out.extend(_equalities(child, depth + 1))
    return out


class _Scope:
    """What one algebra subtree contributes: literal uses already resolved,
    variables bound by a property, and ``=`` / VALUES pairs not yet matched
    to a binding (resolved as soon as an enclosing scope binds the variable)."""

    def __init__(self) -> None:
        self.uses: list[tuple[str, str]] = []
        self.bound: dict[Variable, list[str]] = {}
        self.pending: list[tuple[Variable, Literal]] = []

    def absorb(self, other: _Scope) -> None:
        self.uses.extend(other.uses)
        for var, props in other.bound.items():
            self.bound.setdefault(var, []).extend(props)
        self.pending.extend(other.pending)

    def resolve(self) -> None:
        left: list[tuple[Variable, Literal]] = []
        for var, lit in self.pending:
            props = self.bound.get(var)
            if props:
                self.uses.extend((p, str(lit)) for p in props)
            else:
                left.append((var, lit))
        self.pending = left


def _scope(node: object, depth: int = 0) -> _Scope:
    """Walk an rdflib algebra tree, matching each FILTER ``=`` and VALUES row
    only to properties bound in the same group or an enclosing one — never to
    a sibling UNION branch or OPTIONAL that binds the same variable name."""
    scope = _Scope()
    if node is None or depth > _MAX_DEPTH or isinstance(node, (str, Literal, URIRef, Variable)):
        return scope
    if isinstance(node, (list, tuple)):
        for item in node:
            scope.absorb(_scope(item, depth + 1))
        return scope
    if not isinstance(node, dict):  # rdflib's CompValue is an OrderedDict
        return scope
    name = getattr(node, "name", None)
    if name == "BGP":
        for triple in node.get("triples") or ():
            if not (isinstance(triple, tuple) and len(triple) == 3) or not isinstance(triple[1], URIRef):
                continue
            prop, obj = local_name(triple[1]), triple[2]
            if _is_string_literal(obj):
                scope.uses.append((prop, str(obj)))
            elif isinstance(obj, Variable):
                scope.bound.setdefault(obj, []).append(prop)
        return scope
    if name == "values":
        for row in node.get("res") or ():
            scope.pending.extend((v, lit) for v, lit in row.items() if _is_string_literal(lit))
        return scope
    if name == "Union":
        # Each branch resolves its own filters first, so a filter inside one
        # branch never matches the other's binding; the branches' bindings then
        # go up for a filter written above the UNION, which applies to both.
        for side in ("p1", "p2"):
            branch = _scope(node.get(side), depth + 1)
            branch.resolve()
            scope.absorb(branch)
        return scope
    for key, child in node.items():
        if key in ("_vars", "expr"):
            continue
        scope.absorb(_scope(child, depth + 1))
    if name in ("Filter", "LeftJoin"):
        scope.pending.extend(_equalities(node.get("expr")))
    scope.resolve()
    return scope


def sparql_literal_uses(query: str) -> list[tuple[str, str]]:
    """``(property local name, literal)`` for each plain string literal
    ``query`` compares a property with: the object of a triple
    (``ex:p "x"``), an ``=`` in a ``FILTER`` with a variable that is the
    object of ``ex:p`` in the same or an enclosing group (either side), or a
    ``VALUES`` row for such a variable. ``CONTAINS``, ``!=``, ``IN``,
    ``EXISTS`` bodies and typed literals are not uses. A query that does not
    parse gives ``[]`` (``validate`` is the authority on parseability)."""
    try:
        algebra = parse_sparql(query).algebra
    except Exception:  # noqa: BLE001 - validate() is the authority on parseability
        return []
    return list(dict.fromkeys(_scope(algebra).uses))


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValueFlag:
    """A literal the property's values do not hold. ``kind`` is ``"absent"``
    (no match), ``"case"`` (matches only ignoring case and surrounding space)
    or ``"type"`` (the property holds only numbers or booleans)."""

    property: str
    literal: str
    kind: str


def _membership(literal: str, pv: PropertyValues) -> str:
    if pv.kinds and set(pv.kinds) <= _NON_TEXT_KINDS:
        return "type"
    if literal in pv.values:
        return "exact"
    folded = literal.strip().casefold()
    return "case" if any(v.strip().casefold() == folded for v in pv.values) else "absent"


def value_flags(query: str, lookup: Mapping[str, PropertyValues]) -> list[ValueFlag]:
    """Every literal use in ``query`` whose property's values do not hold it.
    ``lookup`` is :func:`by_property`'s result. A property with no values
    supplied is never flagged. Public, so a caller can examine the query that
    answered (e.g. to say "no record has status X" instead of stating 0)."""
    out: list[ValueFlag] = []
    for prop, lit in sparql_literal_uses(query):
        pv = lookup.get(prop)
        if pv is None or not pv.values or not pv.checkable:
            continue
        kind = _membership(lit, pv)
        if kind != "exact":
            out.append(ValueFlag(prop, lit, kind))
    return out


def _shown(literals: Sequence[str], pv: PropertyValues, all_max: int, nearest: int) -> tuple[bool, list[str]]:
    """``(all, values)`` a hint lists: every showable value up to ``all_max``,
    else the ``nearest`` to any of ``literals`` by difflib ratio ignoring case
    — at least one whenever any value is showable."""
    showable = [v for v in pv.values if _showable(v)]
    if len(pv.values) <= all_max:
        return True, showable
    folded: dict[str, str] = {}
    for v in showable:
        folded.setdefault(v.casefold(), v)
    near: list[str] = []
    for lit in literals:
        for v in difflib.get_close_matches(lit.casefold(), list(folded), n=max(nearest, 1), cutoff=0.0):
            if folded[v] not in near:
                near.append(folded[v])
    return False, near[: max(nearest, 1)]


class ValueCheck:
    """Soft postcondition: flag string literals a property never holds.

    On a flag it returns a **soft** violation whose hint names each flagged
    property once with its values, so the loop gets one chance to use the
    stored form; when the budget is spent, the engine returns the first
    candidate whose only objection was soft (``NLResult.postcondition``) — it
    never refuses. Any failure inside the check is logged and the query
    accepted.

    ``announce_values=True`` also renders :func:`values_prompt_section` through
    ``prompt_section()``. The engine places postcondition sections at the end of
    the system prompt, after the examples and grounding blocks; the measured
    placement is right after the grammar section, which a caller that owns its
    adapter should prefer. See the module docstring: values must be the asker's.
    """

    code = "value_check"

    def __init__(
        self,
        values: Iterable[PropertyValues],
        *,
        announce_values: bool = False,
        categorical_max: int = CATEGORICAL_MAX,
        hint_all_max: int = HINT_ALL_MAX,
        hint_nearest: int = HINT_NEAREST,
    ) -> None:
        self.values = tuple(values)
        self.lookup = by_property(self.values)
        self.announce_values = announce_values
        self.categorical_max = categorical_max
        self.hint_all_max = hint_all_max
        self.hint_nearest = hint_nearest

    def prompt_section(self) -> str:
        if not self.announce_values:
            return ""
        return values_prompt_section(self.values, categorical_max=self.categorical_max)

    def _reason(self, f: ValueFlag) -> str:
        lit, prop = _no_heading(json.dumps(_clean(f.literal), ensure_ascii=False)), _name(f.property)
        if f.kind == "type":
            kinds = "/".join(self.lookup[f.property].kinds)
            return f"The string {lit} is compared with {prop}, which holds only {kinds} values, so a quoted string never equals it."
        if f.kind == "case":
            return f"The literal {lit} is compared with {prop}, which holds it only in another case; the data is matched exactly."
        return f"The literal {lit} is compared with {prop}, which never holds that value."

    def _hint(self, prop: str, flags: Sequence[ValueFlag]) -> str:
        pv, name = self.lookup[prop], _name(prop)
        if all(f.kind == "type" for f in flags):
            return f"Compare {name} with an unquoted {pv.kinds[0]} value, or drop the filter."
        every, shown = _shown([f.literal for f in flags], pv, self.hint_all_max, self.hint_nearest)
        if not shown:
            return f"{name} holds {len(pv.values)} values, none of which can be shown here."
        listed = ", ".join(_quote(v) for v in shown)
        lead = (
            f"The values {name} holds are"
            if every
            else f"{name} holds {len(pv.values)} values; the nearest are"
        )
        return f"{lead}: {listed}."

    def _violation(self, query: str) -> PostconditionViolation | None:
        found = value_flags(query, self.lookup)
        if not found:
            return None
        by_prop: dict[str, list[ValueFlag]] = {}
        for f in found:
            by_prop.setdefault(f.property, []).append(f)
        hints = " ".join(self._hint(p, fs) for p, fs in by_prop.items())
        return PostconditionViolation(
            reason=" ".join(self._reason(f) for f in found),
            suggested_hint=f"{hints} {KEEP_IF_ABSENT}",
            code=self.code,
            soft=True,
        )

    def check(self, query: str, *, context: PostconditionContext) -> PostconditionViolation | None:
        try:
            return self._violation(query)
        except Exception:  # noqa: BLE001 - a broken check must not refuse a valid query
            logger.warning("value check failed; query accepted", exc_info=True)
            return None
