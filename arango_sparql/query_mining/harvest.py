"""Read saved AQL queries from a database and refuse the ones that write.

Ported unchanged in behaviour from arango-cypher-py's
``arango_cypher/query_mining/harvest.py``.

Three system collections hold them (graph visualizer, ArangoDB 3.12):

``_queries``
    Visualizer saved queries: ``name``, ``description``, ``queryText``,
    ``bindVariables`` (an object), ``graphId``.
``_canvasActions``
    Visualizer right-click actions; same fields, and they run on the selected
    nodes through ``@nodes``.
``_editor_saved_queries``
    Query-editor saves: ``title``, ``content``, ``bindVariables`` and
    ``options`` (JSON strings), ``databaseName``.

Each collection is optional — a database that never used the visualizer has
none of them — and a document missing its query text is skipped with a reason
rather than guessed at.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .._privacy import tenant_name

logger = logging.getLogger(__name__)

SAVED_QUERIES = "_queries"
CANVAS_ACTIONS = "_canvasActions"
EDITOR_SAVED_QUERIES = "_editor_saved_queries"

#: Plan node types that modify data. A saved query whose plan contains one is
#: never executed here: mining runs other people's AQL against their data.
WRITE_NODE_TYPES = frozenset(
    {"InsertNode", "UpdateNode", "ReplaceNode", "RemoveNode", "UpsertNode", "MultipleRemoteModificationNode"}
)


@dataclass(frozen=True)
class SavedQuery:
    """One saved AQL query and the text that describes it."""

    source: str  # collection it came from
    key: str
    name: str
    description: str
    aql: str
    bind_vars: dict[str, Any]
    graph: str | None = None
    #: True for the visualizer's generic built-ins, e.g. "Fetch edges of type
    #: @@edgeType (default)" — valid, but they say nothing about the domain.
    builtin: bool = False

    @property
    def is_canvas_action(self) -> bool:
        return self.source == CANVAS_ACTIONS

    @property
    def fingerprint(self) -> str:
        """Stable identity of the query text + bind values (survives renames)."""
        payload = json.dumps({"aql": " ".join(self.aql.split()), "bind": self.bind_vars}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class HarvestReport:
    queries: list[SavedQuery] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (source/key, reason)


def _as_object(raw: Any) -> dict[str, Any] | None:
    """Bind variables arrive as an object (visualizer) or a JSON string (editor)."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return dict(parsed) if isinstance(parsed, dict) else None
    return None


def _visualizer_query(source: str, doc: dict[str, Any]) -> SavedQuery | str:
    aql = doc.get("queryText")
    if not isinstance(aql, str) or not aql.strip():
        return "no queryText"
    bind = _as_object(doc.get("bindVariables"))
    if bind is None:
        return "bindVariables is not an object"
    name = str(doc.get("name") or "").strip()
    return SavedQuery(
        source=source,
        key=str(doc.get("_key")),
        name=name,
        description=str(doc.get("description") or "").strip(),
        aql=aql.strip(),
        bind_vars=bind,
        graph=doc.get("graphId") or None,
        builtin=name.endswith("(default)"),
    )


def _editor_query(doc: dict[str, Any]) -> SavedQuery | str:
    aql = doc.get("content")
    if not isinstance(aql, str) or not aql.strip():
        return "no content"
    bind = _as_object(doc.get("bindVariables"))
    if bind is None:
        return "bindVariables is not a JSON object"
    title = str(doc.get("title") or "").strip()
    # The editor's placeholder titles ("Untitled-2") describe nothing.
    description = "" if title.lower().startswith("untitled") else title
    return SavedQuery(
        source=EDITOR_SAVED_QUERIES,
        key=str(doc.get("_key")),
        name=title,
        description=description,
        aql=aql.strip(),
        bind_vars=bind,
    )


def harvest_saved_queries(db: Any, *, graph: str | None = None) -> HarvestReport:
    """Every saved query in *db*, optionally only those attached to *graph*.

    Editor saves are not tied to a graph, so a *graph* filter keeps them.
    Exact duplicates (same text and bind values) are reported once.
    """
    report = HarvestReport()
    seen: set[str] = set()

    def _add(result: SavedQuery | str, where: str) -> None:
        if isinstance(result, str):
            report.skipped.append((where, result))
            return
        if graph is not None and result.graph is not None and result.graph != graph:
            return
        if result.fingerprint in seen:
            report.skipped.append((where, "duplicate of an earlier saved query"))
            return
        seen.add(result.fingerprint)
        report.queries.append(result)

    for source in (SAVED_QUERIES, CANVAS_ACTIONS):
        if not db.has_collection(source):
            continue
        for doc in db.collection(source).all():
            _add(_visualizer_query(source, doc), f"{source}/{doc.get('_key')}")

    if db.has_collection(EDITOR_SAVED_QUERIES):
        for doc in db.collection(EDITOR_SAVED_QUERIES).all():
            database = doc.get("databaseName")
            if database and database != db.name:
                continue
            _add(_editor_query(doc), f"{EDITOR_SAVED_QUERIES}/{doc.get('_key')}")

    logger.info(
        "harvested %d saved queries from %s (%d skipped)",
        len(report.queries),
        tenant_name(db.name),
        len(report.skipped),
    )
    return report


def plan_writes(db: Any, aql: str, bind_vars: dict[str, Any]) -> list[str]:
    """Write node types in the query's execution plan (empty for a read-only query).

    Decided from ``EXPLAIN`` rather than by scanning the text: a keyword in a
    string literal or attribute name is not a write, and a write hidden in a
    subquery still is.
    """
    plan = db.aql.explain(aql, bind_vars=bind_vars, all_plans=False)
    plans = plan if isinstance(plan, list) else [plan]
    found: list[str] = []
    for p in plans:
        for node in (p or {}).get("nodes") or []:
            if node.get("type") in WRITE_NODE_TYPES:
                found.append(str(node["type"]))
    return found
