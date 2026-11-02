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
# ArangoDB's dates, as epoch milliseconds: 0000-01-01T00:00:00.000Z to
# 9999-12-31T23:59:59.999Z (outside them its date functions warn and fail).
_FIRST_INSTANT, _LAST_INSTANT = -62_167_219_200_000, 253_402_300_799_999


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
    a type error. Anything else yields null, SPARQL's "error → unbound": a
    string that is no date, a day the month does not have (``2020-02-30``,
    which ArangoDB would read as March 1), a result outside ArangoDB's years
    0–9999. ``24:00:00`` is the next day's midnight (XSD).
    """
    months, millis = duration_parts(duration)
    if op == "-":
        months, millis = -months, -millis
    b = builder.bind
    shiftable = b(_SHIFTABLE, hint="date_shape")
    zone_re, zone_ref = b(_ZONE, hint="zone_shape"), b("$1", hint="zone_group")
    midnight, utc = b("T00:00:00Z", hint="midnight"), b("Z", hint="utc")
    hour_24, on_the_hour = b("24", hint="hour_24"), b(":00:00", hint="on_the_hour")
    months_bind, millis_bind = b(months, hint="months"), b(millis, hint="millis")
    day_unit = b("d", hint="day_unit")
    first, last = b(_FIRST_INSTANT, hint="first_instant"), b(_LAST_INSTANT, hint="last_instant")
    dot, zeros, trailing, empty = (
        b(".", hint="dot"),
        b("00", hint="zeros"),
        b(_TRAILING_ZEROS, hint="trailing_zeros"),
        b("", hint="empty"),
    )
    dt, zone, local, is_24, clock = (
        builder.fresh_alias(prefix=p) for p in ("dt", "zone", "local", "h24", "clock")
    )
    month_index, year, month, day, moment, shifted, ms = (
        builder.fresh_alias(prefix=p) for p in ("mi", "year", "month", "day", "moment", "shifted", "ms")
    )
    is_date = f"LENGTH({local}) == {_DATE_LENGTH}"
    fraction = f"({ms} == 0 ? {empty} : CONCAT({dot}, REGEX_REPLACE(RIGHT(CONCAT({zeros}, {ms}), 3), {trailing}, {empty})))"
    # ``shifted`` is the ISO 8601 string of the local clock's result
    # (``"0999-02-01T00:00:00.000Z"``, the year zero-padded): its first 10 or
    # 19 characters are the result's date or date and time. The day-time
    # part is added as epoch milliseconds, checked against ArangoDB's range
    # before it is turned back into a date.
    return (
        f"(FOR {dt} IN [{date_expr}] "
        f"FILTER IS_STRING({dt}) && REGEX_TEST({dt}, {shiftable}) "
        f"LET {zone} = REGEX_REPLACE({dt}, {zone_re}, {zone_ref}) "
        f"LET {local} = LEFT({dt}, LENGTH({dt}) - LENGTH({zone})) "
        f"FILTER DATE_DAY(CONCAT(LEFT({local}, {_DATE_LENGTH}), {midnight})) == "
        f"TO_NUMBER(SUBSTRING({local}, 8, 2)) "
        f"LET {is_24} = SUBSTRING({local}, 11, 2) == {hour_24} "
        f"FILTER !{is_24} || SUBSTRING({local}, 13, 6) == {on_the_hour} "
        f"LET {clock} = {is_24} ? DATE_ADD(CONCAT(LEFT({local}, {_DATE_LENGTH}), {midnight}), 1, {day_unit}) "
        f": CONCAT({local}, {is_date} ? {midnight} : {utc}) "
        f"LET {month_index} = DATE_YEAR({clock}) * 12 + DATE_MONTH({clock}) - 1 + {months_bind} "
        f"LET {year} = FLOOR({month_index} / 12) "
        f"FILTER {year} >= 0 && {year} <= 9999 "
        f"LET {month} = {month_index} - {year} * 12 + 1 "
        f"LET {day} = MIN([DATE_DAY({clock}), DATE_DAYS_IN_MONTH(DATE_ISO8601({year}, {month}, 1))]) "
        f"LET {moment} = DATE_TIMESTAMP({year}, {month}, {day}, DATE_HOUR({clock}), "
        f"DATE_MINUTE({clock}), DATE_SECOND({clock}), DATE_MILLISECOND({clock})) + {millis_bind} "
        f"FILTER {moment} >= {first} && {moment} <= {last} "
        f"LET {shifted} = DATE_ISO8601({moment}) "
        f"LET {ms} = DATE_MILLISECOND({shifted}) "
        f"RETURN {is_date} ? CONCAT(LEFT({shifted}, {_DATE_LENGTH}), {zone}) "
        f": CONCAT(LEFT({shifted}, 19), {fraction}, {zone}))[0]"
    )


def instant(builder: AqlQueryBuilder, expr: str) -> str:
    """The epoch milliseconds a date or dateTime string names; null for
    anything else (a number, a non-date string, null), without the warning
    ``DATE_TIMESTAMP`` raises for those. A local (zoneless) value is read as
    UTC."""
    value = builder.fresh_alias(prefix="ts")
    shape = builder.bind(_SHIFTABLE, hint="date_shape")
    return (
        f"(FOR {value} IN [{expr}] RETURN IS_STRING({value}) && REGEX_TEST({value}, {shape}) "
        f"? DATE_TIMESTAMP({value}) : null)[0]"
    )


def compare_instants(
    builder: AqlQueryBuilder,
    left: str,
    right: str,
    operator: str,
    *,
    shifted: tuple[bool, bool],
    not_a_date: tuple[bool, bool] = (False, False),
) -> str:
    """``left operator right`` where at least one side is a shifted date.

    The instants are compared, not the strings: a shift's lexical form need
    not match the other side's (``"2020-02-01"`` against
    ``"2020-02-01T00:00:00Z"``, or two zones). *shifted* says which sides are
    shifts: one that failed (null) makes the comparison an error, false in a
    FILTER. *not_a_date* marks a side the query writes as a literal of
    another type (a plain string, a number): then the terms differ, so
    ``!=`` holds and every other operator is an error. A side known only at
    run time that is no date (``"abc"``) differs from the date too (``!=``
    holds; pyoxigraph agrees), unless it is unbound.
    """
    l_value, r_value = builder.fresh_alias(prefix="lhs"), builder.fresh_alias(prefix="rhs")
    l_time, r_time = builder.fresh_alias(prefix="lts"), builder.fresh_alias(prefix="rts")
    guards = [f"{t} != null" for t, is_shift in ((l_time, shifted[0]), (r_time, shifted[1])) if is_shift]
    if any(not_a_date):
        test = " && ".join(guards) if operator == "!=" and guards else "false"
    elif operator == "!=":
        test = " && ".join(
            [
                *guards,
                f"{l_value} != null",
                f"{r_value} != null",
                f"({l_time} == null || {r_time} == null || {l_time} != {r_time})",
            ]
        )
    else:
        test = f"{l_time} != null && {r_time} != null && {l_time} {operator} {r_time}"
    return (
        f"(FOR {l_value} IN [{left}] FOR {r_value} IN [{right}] "
        f"LET {l_time} = {instant(builder, l_value)} LET {r_time} = {instant(builder, r_value)} "
        f"RETURN {test})[0]"
    )
