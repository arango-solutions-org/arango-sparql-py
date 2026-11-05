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

__all__ = [
    "CATEGORICAL_MAX",
    "PropertyValues",
    "ValueCheck",
    "ValueFlag",
    "by_property",
    "local_name",
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
#: Longest rendered value or name, in characters (query-core's label cap).
TEXT_MAX_LEN = 200
#: The kinds a caller may report for a property's raw values.
KINDS = frozenset({"str", "int", "float", "bool"})
_NON_TEXT_KINDS = frozenset({"int", "float", "bool"})

VALUES_HEADER = "## Values as the data stores them"
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

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
_WHITESPACE_RUN_RE = re.compile(r"\s+")
#: Literal datatypes that count as plain strings.
_STRING_TYPES = frozenset({None, XSD.string})
_MAX_DEPTH = 200


def _clean(text: str) -> str:
    """One line, control characters removed, at most :data:`TEXT_MAX_LEN`."""
    cleaned = _WHITESPACE_RUN_RE.sub(" ", _CONTROL_CHARS_RE.sub(" ", text)).strip()
    return cleaned[:TEXT_MAX_LEN]


def _quote(value: str) -> str:
    """JSON-quote a cleaned value. A run of ``##`` is escaped so a value can never
    write a ``## …`` heading (which would also move the prompt's cache split);
    a single ``#`` (``C#``) is left as stored."""
    quoted = json.dumps(_clean(value), ensure_ascii=False)
    return quoted.replace("##", "#\\u0023") if "##" in quoted else quoted


def local_name(iri: str) -> str:
    """The part of ``iri`` after its last ``#`` or ``/`` (the whole string when
    it has neither) — the key values are stored and looked up by."""
    return re.split(r"[#/]", iri.strip())[-1] if iri else ""


@dataclass(frozen=True)
class PropertyValues:
    """One property's values, as the caller read them from its data.

    ``values`` must be the property's **complete** distinct text forms: a
    sample would make the check call real values absent. ``kinds`` are the
    Python kinds of the raw values (:data:`KINDS`); ``rows`` is how many rows
    hold a value. ``property`` may be a local name or a full IRI.
    """

    entity: str
    property: str
    values: tuple[str, ...]
    kinds: tuple[str, ...] = ("str",)
    rows: int = 0

    def __post_init__(self) -> None:
        if not local_name(self.property):
            raise ValueError(f"PropertyValues needs a property name, got {self.property!r}")
        unknown = set(self.kinds) - KINDS
        if unknown:
            raise ValueError(
                f"unknown value kinds {sorted(unknown)} for {self.property!r}; known: {sorted(KINDS)}"
            )
        if self.rows < 0:
            raise ValueError(f"rows must be >= 0 for {self.property!r}, got {self.rows}")
        object.__setattr__(self, "values", tuple(str(v) for v in self.values))
        object.__setattr__(self, "kinds", tuple(self.kinds))

    @builtins.property  # the ``property`` field shadows the builtin here
    def key(self) -> str:
        return local_name(self.property)

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
    the order given, under a fixed instruction. ``""`` when none is
    categorical, so the prompt is unchanged."""
    lines = [
        f"- {_clean(pv.entity) + '.' if _clean(pv.entity) else ''}{_clean(pv.key)}: "
        + ", ".join(_quote(v) for v in pv.values)
        for pv in values
        if pv.categorical(categorical_max)
    ]
    if not lines:
        return ""
    return "\n".join([VALUES_HEADER, VALUES_INSTRUCTION, "", *lines])


# ---------------------------------------------------------------------------
# Finding the literals a query compares properties with
# ---------------------------------------------------------------------------


def _is_string_literal(term: object) -> bool:
    return isinstance(term, Literal) and term.datatype in _STRING_TYPES


def _walk(node: object, triples: list[tuple], equalities: list[tuple], depth: int = 0) -> None:
    """Collect BGP triples and ``=`` comparisons from an rdflib algebra tree."""
    if node is None or depth > _MAX_DEPTH or isinstance(node, (str, Literal, URIRef, Variable)):
        return
    if isinstance(node, (list, tuple, set)):
        for item in node:
            _walk(item, triples, equalities, depth + 1)
        return
    if not isinstance(node, dict):  # rdflib's CompValue is an OrderedDict
        return
    name = getattr(node, "name", None)
    if name == "BGP":
        triples.extend(t for t in node.get("triples") or () if isinstance(t, tuple) and len(t) == 3)
    elif name == "RelationalExpression" and node.get("op") == "=":
        other = node.get("other")
        right = other[0] if isinstance(other, list) and len(other) == 1 else other
        equalities.append((node.get("expr"), right))
    for key, child in node.items():
        if key != "_vars":
            _walk(child, triples, equalities, depth + 1)


def sparql_literal_uses(query: str) -> list[tuple[str, str]]:
    """``(property local name, literal)`` for each plain string literal ``query``
    compares a property with: the object of a triple (``ex:p "x"``), or an
    ``=`` in a ``FILTER`` with a variable that is the object of ``ex:p``,
    either side. ``CONTAINS``, ``!=``, ``IN`` and typed literals are not uses.
    A query that does not parse gives ``[]`` (``validate`` is the authority)."""
    try:
        algebra = parse_sparql(query).algebra
    except Exception:  # noqa: BLE001 - validate() is the authority on parseability
        return []
    triples: list[tuple] = []
    equalities: list[tuple] = []
    _walk(algebra, triples, equalities)

    uses: list[tuple[str, str]] = []
    bound: dict[Variable, list[str]] = {}
    for _s, p, o in triples:
        if not isinstance(p, URIRef):
            continue
        if _is_string_literal(o):
            uses.append((local_name(str(p)), str(o)))
        elif isinstance(o, Variable):
            bound.setdefault(o, []).append(local_name(str(p)))
    for left, right in equalities:
        for var, lit in ((left, right), (right, left)):
            if isinstance(var, Variable) and _is_string_literal(lit):
                uses.extend((prop, str(lit)) for prop in bound.get(var, ()))
    return list(dict.fromkeys(uses))


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
        if pv is None or not pv.values:
            continue
        kind = _membership(lit, pv)
        if kind != "exact":
            out.append(ValueFlag(prop, lit, kind))
    return out


def _shown(literal: str, pv: PropertyValues, all_max: int, nearest: int) -> tuple[bool, list[str]]:
    """``(all, values)`` a hint lists: every value up to ``all_max``, else the
    ``nearest`` by difflib ratio ignoring case — always at least one."""
    if len(pv.values) <= all_max:
        return True, list(pv.values)
    folded: dict[str, str] = {}
    for v in pv.values:
        folded.setdefault(v.casefold(), v)
    near = difflib.get_close_matches(literal.casefold(), list(folded), n=max(nearest, 1), cutoff=0.0)
    return False, [folded[v] for v in near]


class ValueCheck:
    """Soft postcondition: flag string literals a property never holds.

    On a flag it returns a **soft** violation whose hint names the property and
    its values, so the loop gets one chance to use the stored form; when the
    budget is spent, the engine returns the first candidate whose only
    objection was soft (``NLResult.postcondition``) — it never refuses. A
    failure inside the check is logged and the query accepted.

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
        lit, prop = json.dumps(_clean(f.literal), ensure_ascii=False), _clean(f.property)
        if f.kind == "type":
            kinds = "/".join(self.lookup[f.property].kinds)
            return f"The string {lit} is compared with {prop}, which holds only {kinds} values, so a quoted string never equals it."
        if f.kind == "case":
            return f"The literal {lit} is compared with {prop}, which holds it only in another case; the data is matched exactly."
        return f"The literal {lit} is compared with {prop}, which never holds that value."

    def _hint(self, f: ValueFlag) -> str:
        pv, prop = self.lookup[f.property], _clean(f.property)
        if f.kind == "type":
            return f"Compare {prop} with an unquoted {pv.kinds[0]} value, or drop the filter."
        every, shown = _shown(f.literal, pv, self.hint_all_max, self.hint_nearest)
        listed = ", ".join(_quote(v) for v in shown)
        lead = (
            f"The values {prop} holds are"
            if every
            else f"{prop} holds {len(pv.values)} values; the nearest are"
        )
        return f"{lead}: {listed}."

    def check(self, query: str, *, context: PostconditionContext) -> PostconditionViolation | None:
        try:
            found = value_flags(query, self.lookup)
        except Exception:  # noqa: BLE001 - a broken check must not refuse a valid query
            logger.warning("value check failed; query accepted", exc_info=True)
            return None
        if not found:
            return None
        return PostconditionViolation(
            reason=" ".join(self._reason(f) for f in found),
            suggested_hint=" ".join(self._hint(f) for f in found) + " " + KEEP_IF_ABSENT,
            code=self.code,
            soft=True,
        )
