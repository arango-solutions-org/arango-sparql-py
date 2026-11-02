"""``date ± duration`` (SPARQL 1.1 §17.3 via XPath's ``op:add-*-to-dateTime``).

Split out of :mod:`visitor` (over its size cap), as :mod:`filter_builtins` is.

ArangoDB's ``DATE_ADD`` is not XPath's addition, in three ways that gave
wrong answers (#123):

- it answers in UTC with milliseconds (``"2020-01-01T23:00:00.000Z"``), so an
  ``xsd:date`` became a dateTime and a ``+02:00`` input lost its zone, and the
  result compared as text with a literal of the other form;
- it overflows a month end (``2020-01-31`` + ``P1M`` is ``2020-03-02``) where
  XPath clamps to the month's last day (``2020-02-29``);
- it reads a zoned input as an instant, where XPath adds to the local clock
  and keeps the zone.

So the shift runs on the input's local clock (read as if UTC, which has no
daylight saving to skip): months first, day clamped to the new month's length,
then the day-time part in milliseconds; and the result is written back in the
input's own form (a date stays a date, the zone is kept, the seconds are
canonical: no ``.000``). A comparison with a shifted date compares instants
(:func:`compare_instants`), never strings.

Every string the AQL uses is a bind parameter; the integers are structural.
"""

from __future__ import annotations

import decimal
import re
from typing import TYPE_CHECKING, Any

from rdflib import Literal

from ..errors import UnsupportedSparqlError

if TYPE_CHECKING:
    from .builder import AqlQueryBuilder

#: XSD's duration and its two totally ordered subtypes.
DURATION_TYPES = frozenset(
    {
        "http://www.w3.org/2001/XMLSchema#duration",
        "http://www.w3.org/2001/XMLSchema#dayTimeDuration",
        "http://www.w3.org/2001/XMLSchema#yearMonthDuration",
    }
)

_DURATION_RE = re.compile(
    r"^(?P<sign>-)?P(?:(?P<y>\d+)Y)?(?:(?P<mo>\d+)M)?(?:(?P<d>\d+)D)?"
    r"(?:T(?:(?P<h>\d+)H)?(?:(?P<mi>\d+)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$"
)

# What ArangoDB can shift: a date or dateTime of a four-digit year, optionally
# with fractional seconds and a zone. Anything else (another type's lexical
# form, a year ArangoDB cannot represent) is SPARQL's error, so unbound.
_SHIFTABLE = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?)?(Z|[+-][0-9]{2}:[0-9]{2})?$"
)
# The zone suffix (``Z``, ``+02:00``) or "" for a local value.
_ZONE = r"^.*?(Z|[+-][0-9]{2}:[0-9]{2})?$"
_TRAILING_ZEROS = "0+$"
_DATE_LENGTH = len("2020-01-31")


def is_duration_literal(term: Any) -> bool:
    return isinstance(term, Literal) and term.datatype is not None and str(term.datatype) in DURATION_TYPES


def is_date_shift(node: Any) -> bool:
    """Whether *node* is ``date ± duration``: an AdditiveExpression with a
    duration literal among its operands, whose value :func:`date_shift`
    computes."""
    if getattr(node, "name", None) != "AdditiveExpression":
        return False
    return any(is_duration_literal(o) for o in [node.expr, *(node.other or [])])


def duration_parts(duration: Literal) -> tuple[int, int]:
    """``(months, milliseconds)`` of an xsd:duration, each carrying its sign.

    A year-month part and a day-time part are not interchangeable (a month
    has no fixed length), so they are kept apart. A fraction of a second
    finer than a millisecond — ArangoDB's resolution — is refused rather than
    rounded.
    """
    lexical = str(duration)
    match = _DURATION_RE.match(lexical)
    if duration.ill_typed or match is None or lexical in ("P", "-P") or lexical.endswith("T"):
        raise UnsupportedSparqlError(f"{lexical!r} is not an xsd:duration")
    sign = -1 if match["sign"] else 1
    months = int(match["y"] or 0) * 12 + int(match["mo"] or 0)
    seconds = decimal.Decimal(match["s"] or 0)
    millis = seconds * 1000
    if millis != millis.to_integral_value():
        raise UnsupportedSparqlError(
            f"the duration {lexical!r} is finer than a millisecond, ArangoDB's date resolution"
        )
    minutes = (int(match["d"] or 0) * 24 + int(match["h"] or 0)) * 60 + int(match["mi"] or 0)
    return sign * months, sign * (minutes * 60_000 + int(millis))


def date_shift(builder: AqlQueryBuilder, op: str, date_expr: str, duration: Literal) -> str:
    """The AQL of ``date_expr op duration`` (``op`` is ``+`` or ``-``).

    Only a string is shifted: AQL reads a number as epoch milliseconds
    (``DATE_ADD(7, "P1D")`` is a date), where SPARQL's number + duration is
    a type error. Anything else — and a string that is no date — yields
    null, SPARQL's "error → unbound".
    """
    months, millis = duration_parts(duration)
    if op == "-":
        months, millis = -months, -millis
    b = builder.bind
    shiftable = b(_SHIFTABLE, hint="date_shape")
    zone_re, zone_ref = b(_ZONE, hint="zone_shape"), b("$1", hint="zone_group")
    midnight, utc = b("T00:00:00Z", hint="midnight"), b("Z", hint="utc")
    months_bind, millis_bind, unit = b(months, hint="months"), b(millis, hint="millis"), b("f", hint="unit")
    date_format = b("%yyyy-%mm-%dd", hint="date_format")
    time_format = b("%yyyy-%mm-%ddT%hh:%ii:%ss", hint="datetime_format")
    dot, zeros, trailing, empty = (
        b(".", hint="dot"),
        b("00", hint="zeros"),
        b(_TRAILING_ZEROS, hint="trailing_zeros"),
        b("", hint="empty"),
    )
    dt, zone, local, clock = (builder.fresh_alias(prefix=p) for p in ("dt", "zone", "local", "clock"))
    month_index, year, month, day, shifted, ms = (
        builder.fresh_alias(prefix=p) for p in ("mi", "year", "month", "day", "shifted", "ms")
    )
    fraction = f"({ms} == 0 ? {empty} : CONCAT({dot}, REGEX_REPLACE(RIGHT(CONCAT({zeros}, {ms}), 3), {trailing}, {empty})))"
    return (
        f"(FOR {dt} IN [{date_expr}] "
        f"FILTER IS_STRING({dt}) && REGEX_TEST({dt}, {shiftable}) "
        f"LET {zone} = REGEX_REPLACE({dt}, {zone_re}, {zone_ref}) "
        f"LET {local} = LEFT({dt}, LENGTH({dt}) - LENGTH({zone})) "
        f"LET {clock} = CONCAT({local}, LENGTH({local}) == {_DATE_LENGTH} ? {midnight} : {utc}) "
        f"LET {month_index} = DATE_YEAR({clock}) * 12 + DATE_MONTH({clock}) - 1 + {months_bind} "
        f"LET {year} = FLOOR({month_index} / 12) "
        f"LET {month} = {month_index} - {year} * 12 + 1 "
        f"LET {day} = MIN([DATE_DAY({clock}), DATE_DAYS_IN_MONTH(DATE_ISO8601({year}, {month}, 1))]) "
        f"LET {shifted} = DATE_ADD(DATE_ISO8601({year}, {month}, {day}, DATE_HOUR({clock}), "
        f"DATE_MINUTE({clock}), DATE_SECOND({clock}), DATE_MILLISECOND({clock})), {millis_bind}, {unit}) "
        f"LET {ms} = DATE_MILLISECOND({shifted}) "
        f"RETURN LENGTH({local}) == {_DATE_LENGTH} ? CONCAT(DATE_FORMAT({shifted}, {date_format}), {zone}) "
        f": CONCAT(DATE_FORMAT({shifted}, {time_format}), {fraction}, {zone}))[0]"
    )


def compare_instants(left: str, right: str, operator: str) -> str:
    """``left operator right`` on the instants two dates name.

    Used when either side is a shifted date: its lexical form need not match
    the other side's (``"2020-02-01"`` against ``"2020-02-01T00:00:00Z"``, or
    two zones). A side that is no date has no instant, and the comparison is
    then an error, false in a FILTER. A local (zoneless) value is read as UTC.
    """
    left, right = f"DATE_TIMESTAMP({left})", f"DATE_TIMESTAMP({right})"
    return f"({left} != null && {right} != null && {left} {operator} {right})"
