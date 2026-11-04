"""Schema-mapping cache (PRD §6.3.3).

Two-tier persistence per the spec:

* **L1 — In-process LRU** (``_mapping_cache`` module dict, keyed by
  ``db.name``). TTL ``SCHEMA_MAPPING_CACHE_TTL_SECONDS`` (env var,
  default 3600 s — Appendix A.5).
* **L2 — Persistent** (``arango_sparql_schema_cache`` collection in
  the customer's own DB, one document keyed ``"mapping"``; see
  :mod:`arango_sparql.schema.persistent_cache`, ported from
  arango-cypher-py). Used whenever the caller passes the ``db`` handle:
  an L1 miss reads through to L2 (so a restart or redeploy serves the
  last analysis) and every :meth:`SchemaCache.put` writes through.
  Callers without a handle (and tests) get the L1-only behaviour.

Drift detection uses Slice 3's bundle-side fingerprints
(:func:`compute_bundle_fingerprint`). When the analyzer is reachable,
the live ``schema_analyzer.fingerprint_physical_shape`` /
``fingerprint_physical_counts`` give a cheaper "should I re-acquire?"
probe; that probe lives in :mod:`arango_sparql.schema.acquire` since
it is part of the acquisition policy. This module owns only the
*storage* contract — the orchestration layer (Slice 5b ``acquire.py``,
Slice 7 routes) decides when to read or write.

Public surface (deliberately small):

* :class:`SchemaCache` — the cache itself.
* :class:`CachedEntry` — what's stored per ``db.name``.
* :data:`DEFAULT_TTL_SECONDS` — pinned default for sentinel tests.
* :data:`L2_COLLECTION_NAME` — the L2 collection name (kept as a
  constant so the route layer can also reference it).

The cache is intentionally *not* a singleton: every consumer that
needs caching gets a :class:`SchemaCache` instance from the FastAPI
app factory (Slice 6). Multiple instances coexist cleanly because
they each own their own L1 dict — required for tests that need
isolated cache state per case.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from arango_sparql.schema.fingerprint import (
    BundleFingerprint,
    FingerprintDrift,
    compute_bundle_fingerprint,
)
from arango_sparql.schema.persistent_cache import ArangoSchemaCache, current_producer
from arango_sparql.translate.mapping import MappingBundle

logger = logging.getLogger(__name__)

# PRD Appendix A.5 default. Held here as the single source of truth
# so a future env-var lookup mismatch surfaces as a sentinel-test
# failure rather than as silent cache flapping in production.
DEFAULT_TTL_SECONDS: int = 3600

# PRD §6.3.3 row 2 names the L2 collection. Constant exported so
# the (future) cache cleanup route, the metrics layer, and the
# fingerprint exclusion list (cache-collection-self-loop guard;
# see sister project `_shape_fingerprint`) can all reference one
# definition.
L2_COLLECTION_NAME: str = "arango_sparql_schema_cache"

# arango-cypher-py's persistent cache (``arango_cypher/schema_cache.py``
# ``DEFAULT_CACHE_COLLECTION``). Both transpilers analyze the same customer
# databases, so each one's cache collection sits next to the other's data.
SISTER_CACHE_COLLECTION_NAME: str = "arango_cypher_schema_cache"

# Collections that hold a transpiler's own cached schema, never user data.
# Schema analysis and the live fingerprints skip them: left in, each cache
# becomes a class in the analyzed schema (the analyzer even invents a
# ``SchemaCache`` superclass for the pair), and every cache write moves the
# fingerprints that decide whether the cache is stale.
SCHEMA_CACHE_COLLECTIONS: frozenset[str] = frozenset({L2_COLLECTION_NAME, SISTER_CACHE_COLLECTION_NAME})

# Env var keyword for the TTL override. Held as a constant so the
# Appendix A.5 row, the unit tests, and the runtime resolution all
# point at the same string.
TTL_ENV_VAR: str = "SCHEMA_MAPPING_CACHE_TTL_SECONDS"


@dataclass(frozen=True)
class CachedEntry:
    """One cache slot's payload.

    Carries the bundle, its fingerprint at acquisition time, and the
    UTC timestamp the entry was stored. ``acquired_at`` drives TTL
    eviction; ``fingerprint`` lets callers compare against a freshly-
    computed fingerprint to detect schema drift without re-reading
    the bundle.

    ``db_shape`` / ``db_counts`` are the *live database* fingerprints
    (``acquire.db_shape_fingerprint`` / ``db_counts_fingerprint``) taken
    when the bundle was built — a different hash domain from
    ``fingerprint``, which hashes the bundle itself; drift checks compare
    a live database against these. ``None`` when not taken (no analyzer,
    or a caller that stores a bundle it did not analyze).
    ``producer`` is :func:`~arango_sparql.schema.persistent_cache.current_producer`
    of the process that built the bundle; ``None`` only for entries a
    caller constructed without one.
    """

    bundle: MappingBundle
    fingerprint: BundleFingerprint
    acquired_at: datetime
    db_shape: str | None = None
    db_counts: str | None = None
    producer: str | None = None

    def age(self, *, now: datetime | None = None) -> timedelta:
        """Return how long ago this entry was acquired. Injectable
        clock for tests; defaults to UTC now.
        """

        when = now if now is not None else datetime.now(UTC)
        return when - self.acquired_at

    def is_expired(self, *, ttl_seconds: int, now: datetime | None = None) -> bool:
        """Return ``True`` iff this entry has aged past *ttl_seconds*.

        TTL=0 means *never expire by age* — the entry is only ever
        evicted by explicit invalidation or by fingerprint drift. This
        matches the L2 "until invalidated" semantics PRD §6.3.3 row 2
        promises and gives operators a clean way to disable the L1
        TTL when they have other invalidation guarantees.
        """

        if ttl_seconds <= 0:
            return False
        return self.age(now=now).total_seconds() >= ttl_seconds

    def is_from_current_code(self) -> bool:
        """``False`` when a different build of the acquisition code (or a
        different analyzer) produced this bundle — e.g. a bundle a previous
        release persisted. Such a bundle is still served, but replaced.
        """

        return self.producer is None or self.producer == current_producer()


class SchemaCache:
    """Two-tier schema-mapping cache (PRD §6.3.3).

    L1 is in-process; L2 is the persistent collection
    (:class:`~arango_sparql.schema.persistent_cache.ArangoSchemaCache`),
    consulted only when a call passes the ``db`` handle.

    Thread safety: the L1 dict is guarded by a per-instance
    ``threading.Lock``. FastAPI route handlers run on a single
    asyncio event loop in the default Uvicorn worker, so contention
    is negligible; the lock is here for the
    multi-thread / sync-thread-pool case where uvicorn workers may
    spawn worker threads for blocking analyzer calls. L2 round-trips run
    outside that lock; concurrent L1 misses for one database share a
    single L2 read (the UI fires several schema requests at once).
    """

    def __init__(
        self,
        *,
        ttl_seconds: int | None = None,
        l2_collection_name: str = L2_COLLECTION_NAME,
        l2_writes: Callable[[], bool] | None = None,
    ) -> None:
        self._ttl_seconds = ttl_seconds if ttl_seconds is not None else _resolve_ttl_from_env()
        self._l2_collection_name = l2_collection_name
        self._l2 = ArangoSchemaCache(collection_name=l2_collection_name, writes=l2_writes)
        self._l1: dict[str, CachedEntry] = {}
        self._lock = threading.Lock()
        self._hydration_locks: dict[str, threading.Lock] = {}

    def _lookup(self, db_name: str, db: Any | None) -> CachedEntry | None:
        """L1, else (with a *db* handle) L2 — hydrating L1 on an L2 hit."""

        with self._lock:
            entry = self._l1.get(db_name)
            if entry is not None or db is None:
                return entry
            hydration_lock = self._hydration_locks.setdefault(db_name, threading.Lock())
        with hydration_lock:
            with self._lock:
                entry = self._l1.get(db_name)
            if entry is not None:
                return entry  # a concurrent miss already hydrated it
            hydrated = self._read_from_l2(db_name, db)
            if hydrated is None:
                return None
            with self._lock:
                # A put that landed during the read is newer than the stored copy.
                return self._l1.setdefault(db_name, hydrated)

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------

    def get(
        self,
        db_name: str,
        *,
        db: Any | None = None,
        now: datetime | None = None,
    ) -> CachedEntry | None:
        """Return the cached entry for *db_name*, or ``None`` on miss
        or expiry.

        Expired entries are evicted from L1 on read so a stale slot does
        not linger across the next put. With *db*, an L1 miss reads
        through to L2.
        """

        entry = self._lookup(db_name, db)
        if entry is None:
            return None
        if entry.is_expired(ttl_seconds=self._ttl_seconds, now=now):
            with self._lock:
                if self._l1.get(db_name) is entry:
                    self._l1.pop(db_name, None)
            return None
        return entry

    def peek(
        self,
        db_name: str,
        *,
        db: Any | None = None,
        now: datetime | None = None,
    ) -> tuple[CachedEntry | None, bool]:
        """Return ``(entry, expired)`` for *db_name* WITHOUT evicting.

        The request path's stale-while-revalidate read: a schema older than
        the TTL is still a far better answer than making the user wait for a
        multi-minute re-analysis, so the caller serves it and schedules a
        background refresh (``service/schema_warm.py``) when ``expired`` is
        true. :meth:`get` keeps its evict-on-expiry contract for every
        existing caller. With *db*, an L1 miss reads through to L2 — the
        first read after a restart.
        """

        entry = self._lookup(db_name, db)
        if entry is None:
            return None, False
        return entry, entry.is_expired(ttl_seconds=self._ttl_seconds, now=now)

    def has_fresh_entry(
        self,
        db_name: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Convenience predicate — ``True`` iff :meth:`get` would
        return non-``None``. Useful for the route-layer "should I
        even probe live fingerprints?" gate.
        """

        return self.get(db_name, now=now) is not None

    def status(self, db_name: str) -> CacheStatus:
        """Return a snapshot of cache state for *db_name* without
        consuming or evicting the entry. Used by ``/schema/status``
        (PRD §6.4) to populate its drift-report payload before any
        re-acquisition decision.
        """

        with self._lock:
            entry = self._l1.get(db_name)
            return CacheStatus(
                db_name=db_name,
                has_entry=entry is not None,
                age_seconds=(entry.age().total_seconds() if entry is not None else None),
                is_expired=(entry.is_expired(ttl_seconds=self._ttl_seconds) if entry is not None else False),
                fingerprint=(entry.fingerprint if entry is not None else None),
                ttl_seconds=self._ttl_seconds,
            )

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------

    def put(
        self,
        db_name: str,
        bundle: MappingBundle,
        *,
        db: Any | None = None,
        db_shape: str | None = None,
        db_counts: str | None = None,
        fingerprint: BundleFingerprint | None = None,
        now: datetime | None = None,
    ) -> CachedEntry:
        """Store *bundle* under *db_name*. Returns the stored
        :class:`CachedEntry` for the caller's convenience.

        *fingerprint* is computed via :func:`compute_bundle_fingerprint`
        when not supplied — callers that already have a fingerprint
        in hand (e.g. from a freshness check) should pass it through
        to avoid redundant SHA-256 work. *db_shape* / *db_counts* are the
        live database fingerprints taken for this bundle. With *db*, the
        entry is also written to L2 (best-effort; a failure is logged and
        leaves the entry in L1 only).
        """

        when = now if now is not None else datetime.now(UTC)
        fp = fingerprint if fingerprint is not None else compute_bundle_fingerprint(bundle, now=when)
        entry = CachedEntry(
            bundle=bundle,
            fingerprint=fp,
            acquired_at=when,
            db_shape=db_shape,
            db_counts=db_counts,
            producer=current_producer(),
        )
        with self._lock:
            self._l1[db_name] = entry
        if db is not None:
            self._persist_to_l2(db_name, entry, db)
        return entry

    def revalidated(
        self,
        db_name: str,
        *,
        db: Any | None = None,
        db_counts: str | None,
        now: datetime | None = None,
    ) -> CachedEntry | None:
        """Record that *db_name*'s entry was just checked against the live
        database and still holds: restart its TTL (and, with *db*, re-stamp
        the stored copy without rewriting the bundle). Returns the re-stamped
        entry, or ``None`` when there is no L1 entry to re-stamp.
        """

        when = now if now is not None else datetime.now(UTC)
        with self._lock:
            entry = self._l1.get(db_name)
            if entry is None:
                return None
            entry = dataclasses.replace(entry, acquired_at=when, db_counts=db_counts)
            self._l1[db_name] = entry
        if db is not None:
            self._l2.touch(db, full_fingerprint=db_counts, updated_at=when)
        return entry

    def invalidate(self, db_name: str) -> bool:
        """Drop the L1 entry for *db_name*.

        Returns ``True`` iff an L1 entry was actually removed. The
        persistent copy is dropped separately by
        :meth:`invalidate_persistent`, which needs the database handle.
        """

        with self._lock:
            return self._l1.pop(db_name, None) is not None

    def invalidate_persistent(self, db: Any) -> bool:
        """Drop *db*'s L2 document. ``True`` iff one was deleted."""

        return self._l2.invalidate(db)

    def clear(self) -> int:
        """Drop *every* cached entry from L1. Returns the count of
        evicted entries. Used by the test suite and by the
        ``ops/warm-cache.py`` script (PRD §15.4) when prepping a
        clean cache state. L2 is untouched.
        """

        with self._lock:
            count = len(self._l1)
            self._l1.clear()
        return count

    # ------------------------------------------------------------------
    # Drift comparison
    # ------------------------------------------------------------------

    def drift(
        self,
        db_name: str,
        current_fingerprint: BundleFingerprint,
    ) -> FingerprintDrift | None:
        """Compare *current_fingerprint* against the cached one for
        *db_name*. Returns the drift category, or ``None`` when there
        is no cached entry to compare against.

        Wraps :meth:`BundleFingerprint.drift_from` so callers do not
        need to crack open :class:`CachedEntry` themselves.
        """

        entry = self.get(db_name)
        if entry is None:
            return None
        return current_fingerprint.drift_from(entry.fingerprint)

    # ------------------------------------------------------------------
    # L2 (persistent tier)
    # ------------------------------------------------------------------

    def _read_from_l2(self, db_name: str, db: Any) -> CachedEntry | None:
        """Decode *db*'s L2 document as a :class:`CachedEntry`.

        ``acquired_at`` is the document's ``updated_at`` (when the bundle
        was built or last revalidated), so the TTL keeps counting across a
        restart instead of restarting from zero. A document without a
        ``producer`` was written by unknown code and reads as outdated.
        """

        persisted = self._l2.get(db)
        if persisted is None:
            return None
        logger.info(
            "schema cache: loaded the stored schema for db=%r (built %s)", db_name, persisted.updated_at
        )
        return CachedEntry(
            bundle=persisted.bundle,
            fingerprint=compute_bundle_fingerprint(persisted.bundle, now=persisted.updated_at),
            acquired_at=persisted.updated_at,
            db_shape=persisted.shape_fingerprint,
            db_counts=persisted.full_fingerprint,
            producer=persisted.producer or "unknown",
        )

    def _persist_to_l2(self, db_name: str, entry: CachedEntry, db: Any) -> bool:
        """Write *entry* to *db*'s L2 document (best-effort)."""

        stored = self._l2.set(
            db,
            bundle=entry.bundle,
            shape_fingerprint=entry.db_shape,
            full_fingerprint=entry.db_counts,
            producer=entry.producer or current_producer(),
            updated_at=entry.acquired_at,
        )
        if stored:
            logger.info("schema cache: stored the analyzed schema for db=%r", db_name)
        return stored

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def ttl_seconds(self) -> int:
        """The effective TTL for L1 entries in this cache instance."""

        return self._ttl_seconds

    @property
    def l2_collection_name(self) -> str:
        """The name of the L2 collection. Exposed so the route layer
        can reference it without re-importing the constant.
        """

        return self._l2_collection_name

    def __len__(self) -> int:
        with self._lock:
            return len(self._l1)


@dataclass(frozen=True)
class CacheStatus:
    """Read-only snapshot of cache state for one ``db.name``.

    Returned by :meth:`SchemaCache.status` and consumed by the
    ``/schema/status`` route (PRD §6.4) to populate its drift report.
    """

    db_name: str
    has_entry: bool
    age_seconds: float | None
    is_expired: bool
    fingerprint: BundleFingerprint | None
    ttl_seconds: int


# ---------------------------------------------------------------------------
# Env-var resolution
# ---------------------------------------------------------------------------


def _resolve_ttl_from_env() -> int:
    """Read :data:`TTL_ENV_VAR` from the environment, falling back to
    :data:`DEFAULT_TTL_SECONDS` when unset or unparseable.

    Negative values are clamped to 0 (which means "no TTL eviction").
    Done here rather than at the dataclass level so env-var hijinks
    can never silently produce a negative-TTL cache.
    """

    raw = os.environ.get(TTL_ENV_VAR)
    if raw is None or raw == "":
        return DEFAULT_TTL_SECONDS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TTL_SECONDS
    return max(0, value)


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "L2_COLLECTION_NAME",
    "SCHEMA_CACHE_COLLECTIONS",
    "SISTER_CACHE_COLLECTION_NAME",
    "TTL_ENV_VAR",
    "CacheStatus",
    "CachedEntry",
    "SchemaCache",
]
