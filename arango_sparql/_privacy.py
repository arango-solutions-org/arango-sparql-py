"""Mark tenant identifiers in log arguments (PRD §17.1 class B, §17.2).

A database, graph or collection name says whose data a line is about, so the
service logs it only when tenant logging is on. Fields passed as ``extra=`` are
gated by key (``arango_sparql.service.observability.TENANT_IDENTIFIER_KEYS``);
a name formatted into the message itself cannot be found that way, so the call
site marks it::

    logger.warning("schema warm failed: db=%s", tenant_name(db_name))

The service's log filter replaces a marked argument with :data:`REDACTED` when
tenant logging is off. Anywhere else — the library used on its own, a test's
``caplog`` — the marked value is an ordinary ``str`` and logs as itself.

This lives at the package root, not under ``service``, so the library modules
that log names (``schema``, ``query_mining``) need no service import (§6.3.4).
"""

from __future__ import annotations

#: What a tenant identifier is replaced with when tenant logging is off.
REDACTED = "[tenant]"


class TenantName(str):
    """A ``str`` that names a tenant, database, graph or collection."""

    __slots__ = ()


def tenant_name(value: object) -> TenantName:
    """*value* as a :class:`TenantName`; ``None`` becomes ``"None"`` as ``%s`` would."""
    return TenantName(value)
