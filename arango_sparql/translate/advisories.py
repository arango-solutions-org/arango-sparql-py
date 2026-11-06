"""The schema-mapping advisories a :class:`~.resolver.SchemaResolver` raises.

``W_SCHEMA_*`` advisories are raised while IRIs are resolved, and a
resolution is cached. A translation reports the advisories of every IRI it
resolves, cached ones included, and of nothing else (#157), so each cache
entry remembers what it raised (:meth:`AdvisoryLog._capture`) and reports it
again on a hit (:meth:`AdvisoryLog._replay`).

Not thread-safe: :meth:`AdvisoryLog.collecting_warnings` swaps the lists on
the instance, and captures share one stack. A resolver is built per request
or per pipeline (the service, the NL pipeline, the eval harness); share one
across threads only with your own lock.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class AdvisoryLog:
    """Advisory recording for :class:`~.resolver.SchemaResolver`, which
    declares the state it uses as dataclass fields: ``warnings`` (every
    advisory, deduplicated by ``(code, iri)``), ``_warned_keys``,
    ``_entry_warnings`` (by cache entry) and ``_captures`` (open captures)."""

    warnings: list[dict[str, Any]]
    _warned_keys: set[tuple[str, str]]
    _entry_warnings: dict[tuple[str, str], list[dict[str, Any]]]
    _captures: list[list[dict[str, Any]]]

    def _warn_schema(self, *, code: str, message: str, iri: str, **extra: Any) -> None:
        """Append a schema-mapping advisory, deduplicated by ``(code, iri)``.

        Visitors call into the resolver once per triple, so a query like
        ``?a :unknown ?b . ?c :unknown ?d`` would otherwise produce two
        identical warnings. The ``_warned_keys`` guard collapses them
        into one without losing information — operators see the
        unmapped IRI exactly once per translate.
        """
        entry = {"code": code, "message": message, "iri": iri, **extra}
        for captured in self._captures:
            captured.append(entry)
        self._record(entry)

    def _record(self, entry: dict[str, Any]) -> None:
        key = (entry["code"], entry["iri"])
        if key in self._warned_keys:
            return
        self._warned_keys.add(key)
        self.warnings.append(entry)

    @contextmanager
    def _capture(self, kind: str, key: str) -> Iterator[None]:
        """Remember the advisories a resolution raises with its cache entry."""
        captured: list[dict[str, Any]] = []
        self._captures.append(captured)
        try:
            yield
        finally:
            # Captures nest (a class resolving its properties): last in, first out.
            self._captures.pop()
            self._entry_warnings[(kind, key)] = captured

    def _replay(self, kind: str, key: str) -> None:
        """Report again the advisories a cached resolution raised."""
        for entry in self._entry_warnings.get((kind, key), ()):
            for captured in self._captures:
                captured.append(entry)
            self._record(entry)

    @contextmanager
    def _quietly(self) -> Iterator[None]:
        """Resolve without reporting: for a survey of the whole ontology
        (:meth:`~.resolver.SchemaResolver.triple_store_class`), whose
        advisories concern classes the query may never name. The cache
        entries still remember theirs, so a class the query names reports
        its own (review of #161)."""
        saved, saved_keys, saved_captures = list(self.warnings), set(self._warned_keys), self._captures
        self._captures = []
        try:
            yield
        finally:
            self._captures = saved_captures
            self.warnings[:] = saved
            self._warned_keys.clear()
            self._warned_keys.update(saved_keys)

    @contextmanager
    def collecting_warnings(self) -> Iterator[list[dict[str, Any]]]:
        """Collect the advisories of one translation (#157).

        Yields the list the translation's advisories go to, deduplicated
        within it: those its own resolutions raise, cached ones included
        (:meth:`_replay`). Before, a resolver reused across translations (a
        library caller, the NL repair loop) reported every earlier
        translation's advisories with each result, and none of a cached
        IRI's. On exit they are merged into :attr:`warnings`, which still
        accumulates for callers that read it directly.
        """
        outer, outer_keys = self.warnings, self._warned_keys
        self.warnings, self._warned_keys = [], set()
        try:
            yield self.warnings
        finally:
            collected = self.warnings
            self.warnings, self._warned_keys = outer, outer_keys
            for entry in collected:
                self._record(entry)
