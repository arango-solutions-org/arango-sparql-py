"""Persistent schema-mapping cache backed by an ArangoDB collection (PRD §6.3.3).

Ported from ``arango-cypher-py``'s ``arango_cypher/schema_cache.py``
(``ArangoSchemaCache``): one document, key ``"mapping"``, in a collection of
the analyzed database itself, so a restart or redeploy serves the last
analysis instead of re-running a multi-minute one. The in-process tier
(:class:`~arango_sparql.schema.cache.SchemaCache`) reads through this one on
a miss and writes through it on every store.

The cache document stores:

- ``shape_fingerprint`` / ``full_fingerprint`` — the analyzer's *live
  database* fingerprints (``schema/acquire.py::db_shape_fingerprint`` /
  ``db_counts_fingerprint``) taken when the bundle was built or last
  revalidated. Same field names as cypher; ``null`` when the analyzer is not
  installed (the heuristic tier has no live fingerprint).
- ``producer`` — what built the bundle (:func:`current_producer`). Deviation
  from cypher, which relies on a hand-bumped ``CACHE_SCHEMA_VERSION`` alone:
  a release that changes how bundles are built must not keep serving bundles
  the old code built just because the database did not change.
- ``bundle`` — the canonical wire dict (``translate/mapping.py``).
- ``schema_version`` — this document's format. A mismatch is a miss.
- ``updated_at`` — when the bundle was last built *or revalidated*; the TTL
  measures from here (cypher: same field, same meaning). ``created_at``.

Best-effort throughout, like cypher's: a read that fails for any reason (no
collection, no permission, a corrupt document) is a miss; a write that fails
(a read-only user cannot create the collection) leaves the service on its
in-process cache. Neither ever fails a request.

The collection is not a system collection, so schema analysis would treat it
as user data; ``schema/acquire.py::hide_service_collections`` keeps it
(and cypher's) out.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

from arango_sparql.translate.mapping import (
    MappingBundle,
    MappingError,
    mapping_from_wire_dict,
    mapping_to_wire_dict,
)

logger = logging.getLogger(__name__)

DEFAULT_CACHE_KEY: str = "mapping"

# Bump when the document layout below changes; older documents then read as
# misses and are overwritten by the next analysis.
CACHE_SCHEMA_VERSION: int = 1

# The modules whose code decides what a stored bundle contains. Their source
# is hashed into :func:`current_producer`, so a release that changes any of
# them retires every bundle the previous code built (a comment-only edit costs
# one background re-analysis — the price of not depending on someone
# remembering to bump a constant).
_PRODUCER_MODULES: tuple[str, ...] = (
    "arango_sparql.schema.acquire",
    "arango_sparql.schema.detect",
    "arango_sparql.translate.mapping",
)


@lru_cache(maxsize=1)
def current_producer() -> str:
    """Identify the code that builds bundles in this process.

    ``"<source digest>/analyzer=<version>"``. Constant for the life of the
    process; computed once.
    """

    import importlib

    digest = hashlib.sha256()
    for name in _PRODUCER_MODULES:
        module = importlib.import_module(name)
        try:
            digest.update(Path(module.__file__ or "").read_bytes())
        except OSError:
            # No readable source (e.g. a zipped install): fall back to the
            # module name so the digest is still stable for this build.
            digest.update(name.encode())
    try:
        analyzer = importlib_metadata.version("arangodb-schema-analyzer")
    except importlib_metadata.PackageNotFoundError:
        analyzer = "none"
    return f"{digest.hexdigest()[:16]}/analyzer={analyzer}"


@dataclass(frozen=True)
class PersistedEntry:
    """One cache document, decoded."""

    bundle: MappingBundle
    shape_fingerprint: str | None
    full_fingerprint: str | None
    producer: str | None
    updated_at: datetime


def bundle_to_doc(bundle: MappingBundle) -> dict[str, Any]:
    """Serialize *bundle* as the canonical wire dict (JSON-safe)."""

    return mapping_to_wire_dict(bundle)


def bundle_from_doc(doc: dict[str, Any]) -> MappingBundle:
    """Reverse of :func:`bundle_to_doc`. Raises ``MappingError`` when malformed."""

    return mapping_from_wire_dict(doc)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


class ArangoSchemaCache:
    """Collection-backed mapping cache: one document per ``cache_key``.

    Stateless apart from its names — the ``db`` handle is passed on every
    call, so one instance serves every database and session (cypher parity).
    """

    def __init__(
        self,
        *,
        collection_name: str,
        cache_key: str = DEFAULT_CACHE_KEY,
        writes: Callable[[], bool] | None = None,
    ) -> None:
        self.collection_name = collection_name
        self.cache_key = cache_key
        # Asked before every write: ``False`` keeps the cache read-only, so nothing is created,
        # stored or deleted in the database (e.g. a database the service may only read).
        self._writes = writes or (lambda: True)

    def _ensure_collection(self, db: Any) -> Any:
        """The cache collection, created on first write; ``None`` if impossible."""

        try:
            if db.has_collection(self.collection_name):
                return db.collection(self.collection_name)
        except Exception:  # noqa: BLE001 — best-effort cache; logged
            logger.debug("schema cache: existence check failed for %r", self.collection_name, exc_info=True)
            return None
        try:
            return db.create_collection(self.collection_name)
        except Exception:  # noqa: BLE001 — e.g. a read-only user; logged
            logger.warning(
                "schema cache: could not create collection %r in database %r; "
                "the analyzed schema will not survive a restart",
                self.collection_name,
                getattr(db, "name", "?"),
                exc_info=True,
            )
            return None

    def get(self, db: Any) -> PersistedEntry | None:
        """The cached entry for *db*, or ``None`` on a miss of any kind."""

        try:
            if not db.has_collection(self.collection_name):
                return None
            doc = db.collection(self.collection_name).get(self.cache_key)
        except Exception:  # noqa: BLE001 — a failed read is a miss
            logger.debug("schema cache: read failed for %r", self.collection_name, exc_info=True)
            return None
        if not isinstance(doc, dict):
            return None
        if doc.get("schema_version") != CACHE_SCHEMA_VERSION:
            logger.info(
                "schema cache: %s/%s is format %r, expected %r; ignoring it",
                self.collection_name,
                self.cache_key,
                doc.get("schema_version"),
                CACHE_SCHEMA_VERSION,
            )
            return None
        updated_at = _parse_timestamp(doc.get("updated_at"))
        bundle_raw = doc.get("bundle")
        if updated_at is None or not isinstance(bundle_raw, dict):
            logger.warning(
                "schema cache: %s/%s is incomplete; ignoring it", self.collection_name, self.cache_key
            )
            return None
        try:
            bundle = bundle_from_doc(bundle_raw)
        except (MappingError, KeyError, TypeError, ValueError):
            logger.warning(
                "schema cache: %s/%s holds an unreadable bundle; ignoring it",
                self.collection_name,
                self.cache_key,
                exc_info=True,
            )
            return None
        return PersistedEntry(
            bundle=bundle,
            shape_fingerprint=_optional_str(doc.get("shape_fingerprint")),
            full_fingerprint=_optional_str(doc.get("full_fingerprint")),
            producer=_optional_str(doc.get("producer")),
            updated_at=updated_at,
        )

    def set(
        self,
        db: Any,
        *,
        bundle: MappingBundle,
        shape_fingerprint: str | None,
        full_fingerprint: str | None,
        producer: str,
        updated_at: datetime,
    ) -> bool:
        """Store *bundle* and its fingerprints. ``True`` on success."""

        if not self._writes():
            return False
        col = self._ensure_collection(db)
        if col is None:
            return False
        stamp = updated_at.isoformat()
        doc = {
            "_key": self.cache_key,
            "schema_version": CACHE_SCHEMA_VERSION,
            "shape_fingerprint": shape_fingerprint,
            "full_fingerprint": full_fingerprint,
            "producer": producer,
            "bundle": bundle_to_doc(bundle),
            "updated_at": stamp,
        }
        try:
            existing = col.get(self.cache_key)
        except Exception:  # noqa: BLE001 — only used to carry created_at forward
            existing = None
        doc["created_at"] = (existing.get("created_at") if isinstance(existing, dict) else None) or stamp
        try:
            col.insert(doc, overwrite=True, silent=True)
        except Exception:  # noqa: BLE001 — best-effort; logged
            logger.warning(
                "schema cache: could not write %s/%s in database %r",
                self.collection_name,
                self.cache_key,
                getattr(db, "name", "?"),
                exc_info=True,
            )
            return False
        return True

    def touch(self, db: Any, *, full_fingerprint: str | None, updated_at: datetime) -> bool:
        """Record that the stored bundle was just revalidated, without
        rewriting it (the bundle is the bulk of the document)."""

        try:
            if not db.has_collection(self.collection_name):
                return False
            db.collection(self.collection_name).update(
                {
                    "_key": self.cache_key,
                    "full_fingerprint": full_fingerprint,
                    "updated_at": updated_at.isoformat(),
                },
                silent=True,
            )
        except Exception:  # noqa: BLE001 — best-effort; logged
            logger.warning(
                "schema cache: could not re-stamp %s/%s in database %r",
                self.collection_name,
                self.cache_key,
                getattr(db, "name", "?"),
                exc_info=True,
            )
            return False
        return True

    def invalidate(self, db: Any) -> bool:
        """Delete the cached document. ``True`` iff one was deleted."""

        if not self._writes():
            return False
        try:
            if not db.has_collection(self.collection_name):
                return False
            return bool(db.collection(self.collection_name).delete(self.cache_key, ignore_missing=True))
        except Exception:  # noqa: BLE001 — best-effort; logged
            logger.warning(
                "schema cache: could not delete %s/%s in database %r",
                self.collection_name,
                self.cache_key,
                getattr(db, "name", "?"),
                exc_info=True,
            )
            return False


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "DEFAULT_CACHE_KEY",
    "ArangoSchemaCache",
    "PersistedEntry",
    "bundle_from_doc",
    "bundle_to_doc",
    "current_producer",
]
