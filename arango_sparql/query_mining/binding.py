"""Complete a saved query's bind variables and run it read-only under caps.

Ported unchanged in behaviour from arango-cypher-py's
``arango_cypher/query_mining/binding.py``.

Saved values are used as stored. Canvas actions are the exception: they run on
the nodes a user selected (``@nodes``), which a saved query cannot carry, so
start vertices are sampled from the graph — first from collections the action's
own name and description point at ("From a selected IAM role/user" →
``aws_iam_role``, ``aws_iam_user``), then from the rest, taking only vertices
that have edges in the graph — and the first start that returns rows is kept.

Every run is bounded: :data:`MAX_RUNTIME_S` server-side, and at most
:data:`ROW_CAP` rows read. A query that returns more cannot be compared
reliably (the SPARQL side might return a different first ``ROW_CAP`` rows), so
it is rejected as too large rather than compared on a prefix.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from arango.exceptions import ArangoError

from .._privacy import tenant_name
from .harvest import SavedQuery, plan_writes
from .signature import ResultSignature, signature_of

logger = logging.getLogger(__name__)

ROW_CAP = 2000
MAX_RUNTIME_S = 30.0
#: Start vertices tried per candidate collection, and collections tried.
STARTS_PER_COLLECTION = 3
MAX_START_COLLECTIONS = 4

_TOKEN = re.compile(r"[a-z0-9]+")
#: Tokens too common in collection names to point at one ("aws_iam_role" and
#: "aws_s3_bucket" share "aws").
_WEAK_TOKENS = frozenset(
    {"aws", "the", "a", "an", "of", "to", "from", "all", "selected", "this", "node", "nodes"}
)


class SourceRejected(Exception):
    """The source AQL cannot serve as a reference; the message says why."""


class TooLarge(SourceRejected):
    """More than :data:`ROW_CAP` rows: not comparable, but another start may be."""


class TimedOut(SourceRejected):
    """Killed at :data:`MAX_RUNTIME_S`: not usable, but another start may be."""


#: ArangoDB's "query killed" — what ``max_runtime`` produces.
_ERR_QUERY_KILLED = 1500


@dataclass(frozen=True)
class SourceRun:
    bind_vars: dict[str, Any]
    rows: list[Any]
    signature: ResultSignature
    #: The sampled ``@nodes`` start for a canvas action, else ``None``.
    start: str | None = None


def run_read_only(db: Any, aql: str, bind_vars: dict[str, Any]) -> list[Any]:
    """Rows of *aql*, refusing writes and anything over :data:`ROW_CAP` rows."""
    try:
        writes = plan_writes(db, aql, bind_vars)
    except ArangoError as exc:
        raise SourceRejected(f"does not run: {_server_message(exc)}") from exc
    if writes:
        raise SourceRejected(f"the query writes ({', '.join(sorted(set(writes)))}); mining only runs reads")
    rows: list[Any] = []
    try:
        cursor = db.aql.execute(aql, bind_vars=bind_vars, max_runtime=MAX_RUNTIME_S, batch_size=ROW_CAP + 1)
        try:
            for row in cursor:
                rows.append(row)
                if len(rows) > ROW_CAP:
                    raise TooLarge(f"returns more than {ROW_CAP} rows; too large to compare")
        finally:
            try:
                cursor.close(ignore_missing=True)
            except ArangoError:
                pass  # an exhausted or expired cursor needs no closing
    except ArangoError as exc:
        if getattr(exc, "error_code", None) == _ERR_QUERY_KILLED:
            raise TimedOut(f"timed out after {MAX_RUNTIME_S:g}s") from exc
        raise SourceRejected(f"does not run: {_server_message(exc)}") from exc
    return rows


def _server_message(exc: ArangoError) -> str:
    return str(getattr(exc, "error_message", None) or exc)[:240]


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if t not in _WEAK_TOKENS}


#: Phrases that name where a canvas action starts ("From a selected IAM
#: role/user: …", "… this user can access"); their words outweigh the rest,
#: which mostly name the *target*.
_START_PHRASES = (
    re.compile(r"\bfrom (?:a |the )?(?:selected )?([^:.;]+)", re.IGNORECASE),
    re.compile(r"\bthis ([a-z0-9/ ]+?)(?: can| has| is|$)", re.IGNORECASE),
)
START_PHRASE_WEIGHT = 3


def rank_start_collections(query: SavedQuery, edge_definitions: list[dict[str, Any]]) -> list[str]:
    """The graph's vertex collections, most likely canvas-action start first.

    Score = words shared with the action's name and description, where words
    from a start-naming phrase (:data:`_START_PHRASES`) count
    :data:`START_PHRASE_WEIGHT` times. Ties go to collections on the *from*
    side of more edge definitions. Collections the text never names follow, so
    an action that names nothing is still tried.
    """
    text = f"{query.name}. {query.description}"
    start_words: set[str] = set()
    for pattern in _START_PHRASES:
        for m in pattern.finditer(text):
            start_words |= _tokens(m.group(1).replace("/", " "))
    words = _tokens(text)

    source_degree: dict[str, int] = {}
    order: list[str] = []
    for ed in edge_definitions:
        for c in ed.get("from_vertex_collections") or []:
            source_degree[c] = source_degree.get(c, 0) + 1
            if c not in order:
                order.append(c)
        for c in ed.get("to_vertex_collections") or []:
            source_degree.setdefault(c, 0)
            if c not in order:
                order.append(c)

    def score(c: str) -> int:
        ct = _tokens(c.replace("_", " "))
        return len(ct & words) + (START_PHRASE_WEIGHT - 1) * len(ct & start_words)

    return sorted(order, key=lambda c: (-score(c), -source_degree.get(c, 0), order.index(c)))


def _sample_connected_ids(
    db: Any, collection: str, edge_definitions: list[dict[str, Any]], n: int
) -> list[str]:
    """Up to *n* ids from *collection* that have an edge in the graph.

    Taken from edge documents, outbound side first: a collection's first
    documents usually have no edges, which made every traversal from them
    empty. ``LIMIT`` before de-duplication keeps each probe a bounded scan.
    """
    found: list[str] = []
    for side, key in (("_from", "from_vertex_collections"), ("_to", "to_vertex_collections")):
        for ed in edge_definitions:
            if collection not in (ed.get(key) or []):
                continue
            cursor = db.aql.execute(
                f"FOR e IN @@ec FILTER IS_SAME_COLLECTION(@c, e.{side}) LIMIT @probe RETURN e.{side}",
                bind_vars={"@ec": ed["edge_collection"], "c": collection, "probe": n * 20},
                max_runtime=MAX_RUNTIME_S,
            )
            for vid in cursor:
                if vid not in found:
                    found.append(str(vid))
                if len(found) >= n:
                    return found
    return found


def run_source(
    db: Any, query: SavedQuery, *, edge_definitions: list[dict[str, Any]] | None = None
) -> SourceRun:
    """Run *query* as the reference; raise :class:`SourceRejected` if it cannot be."""
    if not query.is_canvas_action:
        rows = run_read_only(db, query.aql, query.bind_vars)
        if not rows:
            raise SourceRejected("returns no rows with its saved bind values")
        return SourceRun(query.bind_vars, rows, signature_of(rows))

    if "nodes" not in query.bind_vars and "@nodes" not in query.aql:
        raise SourceRejected("canvas action without @nodes")
    defs = edge_definitions or []
    collections = rank_start_collections(query, defs)[:MAX_START_COLLECTIONS]
    if not collections:
        raise SourceRejected("canvas action, but its graph has no edge definitions to sample a start from")
    tried = 0
    too_large = 0
    timed_out = 0
    for collection in collections:
        try:
            starts = _sample_connected_ids(db, collection, defs, STARTS_PER_COLLECTION)
        except ArangoError as exc:
            logger.warning("start sampling failed in %s: %s", tenant_name(collection), _server_message(exc))
            continue
        for start in starts:
            tried += 1
            bind = {**query.bind_vars, "nodes": [start]}
            try:
                rows = run_read_only(db, query.aql, bind)
            except TooLarge:
                too_large += 1
                continue
            except TimedOut:
                timed_out += 1
                continue
            if rows:
                return SourceRun(bind, rows, signature_of(rows), start=start)
    detail = f"{tried} tried in {', '.join(collections)}"
    if too_large:
        detail += f"; {too_large} returned more than {ROW_CAP} rows"
    if timed_out:
        detail += f"; {timed_out} timed out"
    raise SourceRejected(f"no sampled start node returned a comparable result ({detail})")
