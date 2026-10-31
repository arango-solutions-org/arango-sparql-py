"""Replay LDBC's update stream on the oracle and check its published answers
(plan WP-LDBC-REPLAY).

LDBC publishes expected results for Interactive v1
(``validation_params-sf0.1.csv``): one operation per line,
``<parameters JSON>|<expected result JSON>``, with 18,626 inserts interleaved
among ~120 k reads. Each expected answer describes the database *after* the
inserts before it, which the Turtle snapshot does not contain — so the
inserts are applied, in file order, to a copy of the normalised oracle store,
and each sampled read runs at its own point in the stream.

That checks the **SPARQL text and the oracle** against LDBC's own answers
(plan §2.4: pyoxigraph ≠ LDBC means a wrong query or normalisation, not a
translator bug). Replaying the inserts on the ArangoDB layouts is a later
increment.

**Inserts are written here, not taken from the reference templates.** The
GraphDB ``interactive-update-*.rq`` files at the pinned commit attach a new
person to an *existing* ``studyAt`` / ``workAt`` node with the same
organisation and year instead of creating one, which is not the Turtle
model; each insert below creates the triples the Turtle would hold, in its
IRI and literal forms (zero-padded 20-digit IDs, ``…/genid/<kind>…`` nodes,
UTC ``xsd:dateTime``), and the normalisation's ``directKnows`` both ways.

Usage::

    python -m tests.ldbc.replay --sf 0.1 --per-op 25            # IS1–IS5, IS7
    python -m tests.ldbc.replay --sf 0.1 --op IS1 --op IS3 --per-op 200
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyoxigraph as ox

logger = logging.getLogger(__name__)

SN = "http://www.ldbc.eu/ldbc_socialnet/1.0/data/"
SNVOC = "http://www.ldbc.eu/ldbc_socialnet/1.0/vocabulary/"
GENID = "http://www.ldbc.eu/ldbc_socialnet/1.0/.well-known/genid/"
XSD = "http://www.w3.org/2001/XMLSchema#"
DBO = "http://dbpedia.org/ontology/"
TAG_PREFIX = "http://www.ldbc.eu/ldbc_socialnet/1.0/tag/"
QUERIES = Path(__file__).parent / "queries" / "interactive_v1"

_PREFIXES = (
    f"PREFIX sn: <{SN}>\nPREFIX snvoc: <{SNVOC}>\nPREFIX xsd: <{XSD}>\n"
    f"PREFIX dbo: <{DBO}>\nPREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"
)


# ---------------------------------------------------------------------------
# Classifying a line
# ---------------------------------------------------------------------------

#: Parameter keys that name a read, and the read they name.
_READ_KEYS = {
    "personIdSQ1": "IS1",
    "personIdSQ2": "IS2",
    "personIdSQ3": "IS3",
    "messageIdContent": "IS4",
    "messageIdCreator": "IS5",
    "messageForumId": "IS6",
    "messageRepliesId": "IS7",
    "person1IdQ13StartNode": "IC13",
    "person1IdQ14StartNode": "IC14",
}
_COMPLEX_READ = re.compile(r"^person(?:Id)?Q(\d+)$")

#: The key set of each insert (LDBC Interactive v1 IU1–IU8).
_UPDATE_KEYS: dict[frozenset[str], str] = {
    frozenset(
        {
            "personId",
            "personFirstName",
            "personLastName",
            "gender",
            "birthday",
            "creationDate",
            "locationIp",
            "browserUsed",
            "cityId",
            "languages",
            "emails",
            "tagIds",
            "studyAt",
            "workAt",
        }
    ): "IU1",
    frozenset({"personId", "postId", "creationDate"}): "IU2",
    frozenset({"personId", "commentId", "creationDate"}): "IU3",
    frozenset({"forumId", "forumTitle", "creationDate", "moderatorPersonId", "tagIds"}): "IU4",
    frozenset({"forumId", "personId", "joinDate"}): "IU5",
    frozenset(
        {
            "postId",
            "imageFile",
            "creationDate",
            "locationIp",
            "browserUsed",
            "language",
            "content",
            "length",
            "authorPersonId",
            "forumId",
            "countryId",
            "tagIds",
        }
    ): "IU6",
    frozenset(
        {
            "commentId",
            "creationDate",
            "locationIp",
            "browserUsed",
            "content",
            "length",
            "authorPersonId",
            "countryId",
            "replyToPostId",
            "replyToCommentId",
            "tagIds",
        }
    ): "IU7",
    frozenset({"person1Id", "person2Id", "creationDate"}): "IU8",
}


@dataclass(frozen=True)
class Operation:
    """One line of the validation file."""

    index: int
    name: str  # "IU1".."IU8", "IS1".."IS7", "IC1".."IC14"
    params: dict[str, Any]
    expected: Any

    @property
    def is_update(self) -> bool:
        return self.name.startswith("IU")


class ReplayError(RuntimeError):
    """A line could not be classified or applied."""


def classify(params: dict[str, Any]) -> str:
    """The operation a line's parameters describe."""
    for key, name in _READ_KEYS.items():
        if key in params:
            return name
    for key in params:
        if m := _COMPLEX_READ.match(key):
            return f"IC{m.group(1)}"
    name = _UPDATE_KEYS.get(frozenset(params))
    if name is None:
        raise ReplayError(f"unrecognised operation with keys {sorted(params)}")
    return name


def read_operations(path: Path) -> Iterator[Operation]:
    with path.open(encoding="utf-8") as fh:
        for index, line in enumerate(fh):
            raw_params, sep, raw_expected = line.rstrip("\n").partition("|")
            if not sep:
                raise ReplayError(f"line {index + 1}: no '|' between parameters and result")
            params = json.loads(raw_params)
            yield Operation(index, classify(params), params, json.loads(raw_expected))


# ---------------------------------------------------------------------------
# Inserts
# ---------------------------------------------------------------------------


def _iri(kind: str, ident: int) -> str:
    return f"<{SN}{kind}{int(ident):020d}>"


def _datetime(epoch_ms: int) -> str:
    stamp = datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)
    return f'"{stamp.strftime("%Y-%m-%dT%H:%M:%S")}.{stamp.microsecond // 1000:03d}Z"^^xsd:dateTime'


def _date(epoch_ms: int) -> str:
    return f'"{datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).date().isoformat()}"^^xsd:date'


def _string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _by_id(var: str, ident: int, rdf_class: str | None = None) -> str:
    """A WHERE pattern finding a tag, place or organisation by ``snvoc:id``.

    Their IDs are ``xsd:int`` and repeat across kinds (tag 0, country 0), so
    the typed literal is matched through the index and the class narrows it;
    a tag is typed by its tag class, so the IRI's ``tag/`` prefix does.
    """
    match = f'{var} snvoc:id "{int(ident)}"^^xsd:int .'
    if rdf_class:
        return f"{match} {var} rdf:type {rdf_class} ."
    return f'{match} FILTER(STRSTARTS(STR({var}), "{TAG_PREFIX}"))'


class _Nodes:
    """Fresh IRIs for reified relationship nodes, unlike the Turtle's."""

    def __init__(self) -> None:
        self.counter = Counter[str]()

    def __call__(self, kind: str) -> str:
        self.counter[kind] += 1
        return f"<{GENID}{kind}replay{self.counter[kind]:014d}>"


def _tags(subject: str, tag_ids: list[int]) -> list[str]:
    return [
        f"INSERT {{ {subject} snvoc:hasTag ?tag }} WHERE {{ {_by_id('?tag', tag_id)} }}" for tag_id in tag_ids
    ]


def update_sparql(op: Operation, nodes: _Nodes) -> list[str]:
    """SPARQL Update requests applying insert *op* the way the Turtle models it."""
    p = op.params
    if op.name == "IU1":
        person = _iri("pers", p["personId"])
        data = [
            f'{person} rdf:type snvoc:Person ; snvoc:id "{p["personId"]}"^^xsd:long',
            f"snvoc:firstName {_string(p['personFirstName'])} ; snvoc:lastName {_string(p['personLastName'])}",
            f"snvoc:gender {_string(p['gender'])} ; snvoc:birthday {_date(p['birthday'])}",
            f"snvoc:creationDate {_datetime(p['creationDate'])} ; snvoc:locationIP {_string(p['locationIp'])}",
            f"snvoc:browserUsed {_string(p['browserUsed'])}",
        ]
        data += [f"snvoc:speaks {_string(lang)}" for lang in p["languages"]]
        data += [f"snvoc:email {_string(email)}" for email in p["emails"]]
        out = [f"INSERT DATA {{ {' ; '.join(data)} . }}"]
        out.append(
            f"INSERT {{ {person} snvoc:isLocatedIn ?city }} WHERE {{ {_by_id('?city', p['cityId'], 'dbo:City')} }}"
        )
        out += _tags(person, p["tagIds"])
        for org_id, year in p["studyAt"]:
            node = nodes("study")
            out.append(
                f"INSERT {{ {person} snvoc:studyAt {node} . {node} snvoc:hasOrganisation ?org ; "
                f'snvoc:classYear "{year}"^^xsd:integer }} WHERE {{ {_by_id("?org", org_id, "dbo:University")} }}'
            )
        for org_id, year in p["workAt"]:
            node = nodes("work")
            out.append(
                f"INSERT {{ {person} snvoc:workAt {node} . {node} snvoc:hasOrganisation ?org ; "
                f'snvoc:workFrom "{year}"^^xsd:integer }} WHERE {{ {_by_id("?org", org_id, "dbo:Company")} }}'
            )
        return out
    if op.name in ("IU2", "IU3"):
        node = nodes("like")
        target = _iri("post", p["postId"]) if op.name == "IU2" else _iri("comm", p["commentId"])
        exit_ = "hasPost" if op.name == "IU2" else "hasComment"
        return [
            f"INSERT DATA {{ {_iri('pers', p['personId'])} snvoc:likes {node} . {node} snvoc:{exit_} {target} ; "
            f"snvoc:creationDate {_datetime(p['creationDate'])} . }}"
        ]
    if op.name == "IU4":
        forum = _iri("forum", p["forumId"])
        return [
            f'INSERT DATA {{ {forum} rdf:type snvoc:Forum ; snvoc:id "{p["forumId"]}"^^xsd:long ; '
            f"snvoc:title {_string(p['forumTitle'])} ; snvoc:creationDate {_datetime(p['creationDate'])} ; "
            f"snvoc:hasModerator {_iri('pers', p['moderatorPersonId'])} . }}",
            *_tags(forum, p["tagIds"]),
        ]
    if op.name == "IU5":
        node = nodes("mbs")
        return [
            f"INSERT DATA {{ {_iri('forum', p['forumId'])} snvoc:hasMember {node} . {node} snvoc:hasPerson "
            f"{_iri('pers', p['personId'])} ; snvoc:joinDate {_datetime(p['joinDate'])} . }}"
        ]
    if op.name in ("IU6", "IU7"):
        is_post = op.name == "IU6"
        ident = p["postId"] if is_post else p["commentId"]
        message = _iri("post" if is_post else "comm", ident)
        data = [
            f'{message} rdf:type snvoc:{"Post" if is_post else "Comment"} ; snvoc:id "{ident}"^^xsd:long',
            f"snvoc:creationDate {_datetime(p['creationDate'])} ; snvoc:locationIP {_string(p['locationIp'])}",
            f'snvoc:browserUsed {_string(p["browserUsed"])} ; snvoc:length "{p["length"]}"^^xsd:int',
            f"snvoc:hasCreator {_iri('pers', p['authorPersonId'])}",
        ]
        if p.get("content"):
            data.append(f"snvoc:content {_string(p['content'])}")
        if is_post and p.get("imageFile"):
            data.append(f"snvoc:imageFile {_string(p['imageFile'])}")
        if is_post and p.get("language"):
            data.append(f"snvoc:language {_string(p['language'])}")
        out = [f"INSERT DATA {{ {' ; '.join(data)} . }}"]
        if is_post:
            out.append(f"INSERT DATA {{ {_iri('forum', p['forumId'])} snvoc:containerOf {message} . }}")
        else:
            parent = (
                _iri("post", p["replyToPostId"])
                if p["replyToPostId"] != -1
                else _iri("comm", p["replyToCommentId"])
            )
            out.append(f"INSERT DATA {{ {message} snvoc:replyOf {parent} . }}")
        out.append(
            f"INSERT {{ {message} snvoc:isLocatedIn ?country }} WHERE {{ {_by_id('?country', p['countryId'], 'dbo:Country')} }}"
        )
        out += _tags(message, p["tagIds"])
        return out
    if op.name == "IU8":
        node = nodes("knows")
        a, b = _iri("pers", p["person1Id"]), _iri("pers", p["person2Id"])
        # The normalisation materialises directKnows both ways (normalize.py).
        return [
            f"INSERT DATA {{ {a} snvoc:knows {node} . {node} snvoc:hasPerson {b} ; "
            f"snvoc:creationDate {_datetime(p['creationDate'])} . "
            f"{a} snvoc:directKnows {b} . {b} snvoc:directKnows {a} . }}"
        ]
    raise ReplayError(f"{op.name} is not an insert")


# ---------------------------------------------------------------------------
# Reads: run, and compare with LDBC's answer
# ---------------------------------------------------------------------------

#: Per read: (the parameter key holding its argument, the query placeholder),
#: and the answer's fields paired with our projection's columns.
_READS: dict[str, tuple[str, str, list[tuple[str, str]]]] = {
    "IS1": (
        "personIdSQ1",
        "personId",
        [
            ("firstName", "firstName"),
            ("lastName", "lastName"),
            ("birthday", "birthday"),
            ("locationIp", "locationIP"),
            ("browserUsed", "usedBrowser"),
            ("cityId", "cityId"),
            ("gender", "gender"),
            ("creationDate", "creationDate"),
        ],
    ),
    "IS2": (
        "personIdSQ2",
        "personId",
        [
            ("messageId", "message"),
            ("messageContent", "content"),
            ("messageCreationDate", "creationDate"),
            ("originalPostId", "personMessage"),
            ("originalPostAuthorId", "originalPoster"),
            ("originalPostAuthorFirstName", "originalPosterFirstName"),
            ("originalPostAuthorLastName", "originalPosterLastName"),
        ],
    ),
    "IS3": (
        "personIdSQ3",
        "personId",
        [
            ("personId", "knownFriend"),
            ("firstName", "friendFirstName"),
            ("lastName", "friendLastName"),
            ("friendshipCreationDate", "creationDate"),
        ],
    ),
    "IS4": (
        "messageIdContent",
        "messageId",
        [("messageContent", "content"), ("messageCreationDate", "creationDate")],
    ),
    "IS5": (
        "messageIdCreator",
        "messageId",
        [("personId", "creatorId"), ("firstName", "creatorFirstName"), ("lastName", "creatorLastName")],
    ),
    "IS6": (
        "messageForumId",
        "messageId",
        [
            ("forumId", "forum"),
            ("forumTitle", "forumTitle"),
            ("moderatorId", "moderator"),
            ("moderatorFirstName", "moderatorFirstName"),
            ("moderatorLastName", "moderatorLastName"),
        ],
    ),
    "IS7": (
        "messageRepliesId",
        "messageId",
        [
            ("commentId", "repliedComment"),
            ("commentContent", "repliedCommentContent"),
            ("commentCreationDate", "repliedCommentCreationDate"),
            ("replyAuthorId", "replyAuthor"),
            ("replyAuthorFirstName", "replyAuthorFirstName"),
            ("replyAuthorLastName", "replyAuthorLastName"),
            ("isReplyAuthorKnowsOriginalMessageAuthor", "knowsAuthor"),
        ],
    ),
}
READS = tuple(_READS)
#: Checked by default. IS6 (``replyOf*`` up to the forum) is left out:
#: pyoxigraph does not answer it within 10 minutes (the oracle cache records
#: the same), it has no query timeout, and the replay's store changes as it
#: goes, so the read cannot be handed to a killable subprocess.
DEFAULT_READS = tuple(name for name in READS if name != "IS6")
_PADDED_IRI = re.compile(r"/(?:pers|post|comm|forum)(\d{20})$")


def _epoch_ms(value: datetime | date) -> int:
    if isinstance(value, datetime):
        return round(value.timestamp() * 1000)
    return round(datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp() * 1000)


def normalise(term: Any) -> Any:
    """One comparable value: an LDBC entity IRI → its numeric ID, a date →
    epoch milliseconds, ``"true"``/``"false"`` → bool, a number → int."""
    if isinstance(term, ox.NamedNode):
        if m := _PADDED_IRI.search(term.value):
            return int(m.group(1))
        return term.value
    if isinstance(term, ox.Literal):
        dt = term.datatype.value
        if dt == XSD + "dateTime":
            return _epoch_ms(datetime.fromisoformat(term.value.replace("Z", "+00:00")))
        if dt == XSD + "date":
            return _epoch_ms(date.fromisoformat(term.value))
        if dt in (XSD + "long", XSD + "int", XSD + "integer"):
            return int(term.value)
        if term.value in ("true", "false") and dt == XSD + "string":
            return term.value == "true"
        return term.value
    if isinstance(term, bool | int | str) or term is None:
        return term
    return str(term)


def _query(name: str, argument: int) -> str:
    from tests.ldbc import params

    text = (QUERIES / f"interactive-short-{name[2:]}.rq").read_text(encoding="utf-8")
    placeholder = _READS[name][1]
    return params.splice(text, {placeholder: params.format_value(placeholder, str(argument))})


def run_read(store: ox.Store, op: Operation) -> tuple[bool, list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """Run read *op* on *store*; ``(agrees, ours, LDBC's)`` as sorted tuples."""
    key, _placeholder, fields = _READS[op.name]
    rows = []
    for solution in store.query(_query(op.name, op.params[key])):
        rows.append(tuple(normalise(solution[column]) for _ldbc, column in fields))
    expected = op.expected if isinstance(op.expected, list) else [op.expected]
    theirs = []
    for item in expected:
        if not item:  # LDBC's answer when nothing matches
            continue
        # A misspelt field would read as null and disagree on every row.
        missing = sorted({ldbc for ldbc, _column in fields} - item.keys())
        if missing:
            raise ReplayError(
                f"line {op.index + 1}: {op.name} answer has no field {missing}; it has {sorted(item)}"
            )
        theirs.append(tuple(normalise(item[ldbc]) for ldbc, _column in fields))
    ours, theirs = sorted(rows, key=repr), sorted(theirs, key=repr)
    return ours == theirs, ours, theirs


# ---------------------------------------------------------------------------
# The replay
# ---------------------------------------------------------------------------


@dataclass
class ReplayReport:
    updates_applied: Counter[str] = field(default_factory=Counter)
    checked: Counter[str] = field(default_factory=Counter)
    agreed: Counter[str] = field(default_factory=Counter)
    disagreements: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0


#: Log progress after this many inserts (the stream has 18,626).
PROGRESS_EVERY = 1000


def replay(
    store: ox.Store, operations: Iterator[Operation], reads: tuple[str, ...], per_op: int, keep: int = 5
) -> ReplayReport:
    """Apply every insert in order; check the first *per_op* reads of each
    kind in *reads* at their point in the stream.

    Each checked read and every :data:`PROGRESS_EVERY` inserts is logged, so
    a read that hangs (pyoxigraph has no query timeout) shows by its line.
    """
    report = ReplayReport()
    nodes = _Nodes()
    started = time.monotonic()
    for op in operations:
        if op.is_update:
            for request in update_sparql(op, nodes):
                store.update(_PREFIXES + request)
            report.updates_applied[op.name] += 1
            applied = sum(report.updates_applied.values())
            if applied % PROGRESS_EVERY == 0:
                logger.info("line %d: %d inserts applied", op.index + 1, applied)
            continue
        if op.name not in reads or report.checked[op.name] >= per_op:
            continue
        logger.info("line %d: %s …", op.index + 1, op.name)
        read_started = time.monotonic()
        agrees, ours, theirs = run_read(store, op)
        logger.info(
            "line %d: %s %s in %.1fs",
            op.index + 1,
            op.name,
            "agrees" if agrees else "DISAGREES",
            time.monotonic() - read_started,
        )
        report.checked[op.name] += 1
        if agrees:
            report.agreed[op.name] += 1
        elif len(report.disagreements) < keep * len(reads):
            report.disagreements.append(
                {
                    "line": op.index + 1,
                    "op": op.name,
                    "params": op.params,
                    "ours": ours[:5],
                    "ldbc": theirs[:5],
                }
            )
        if all(report.checked[name] >= per_op for name in reads):
            break
    report.seconds = time.monotonic() - started
    return report


def load_store(sf: str) -> ox.Store:
    """A fresh in-memory copy of the normalised oracle data (never the
    oracle's own store, which the inserts would change)."""
    from tests.ldbc import datasets

    nt = datasets.cache_root() / f"sf{sf}-normalized" / "normalized.nt"
    if not nt.is_file():
        raise ReplayError(f"{nt} is missing: run the LDBC oracle or loader once to normalise the data")
    store = ox.Store()
    store.bulk_load(path=str(nt), format=ox.RdfFormat.N_TRIPLES)
    return store


def validation_file(sf: str) -> Path:
    from tests.ldbc import datasets

    path = datasets.cache_root() / "validation_params" / f"validation_params-sf{sf}.csv"
    if not path.is_file():
        raise ReplayError(f"{path} is missing: unpack LDBC's validation parameters there")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--sf", default="0.1")
    parser.add_argument(
        "--op", action="append", choices=READS, help="reads to check (default: IS1–IS5 and IS7; IS6 is slow)"
    )
    parser.add_argument("--per-op", type=int, default=25, help="reads of each kind to check")
    parser.add_argument("--json", type=Path, help="write the report here")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        store = load_store(args.sf)
        report = replay(
            store, read_operations(validation_file(args.sf)), tuple(args.op or DEFAULT_READS), args.per_op
        )
    except ReplayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for name in args.op or DEFAULT_READS:
        print(f"{name}: {report.agreed[name]}/{report.checked[name]} agree with LDBC")
    print(f"inserts applied: {dict(report.updates_applied)} in {report.seconds:.0f}s")
    for item in report.disagreements[:10]:
        print(json.dumps(item, default=str)[:600])
    if args.json:
        args.json.write_text(json.dumps(report.__dict__, default=str, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
