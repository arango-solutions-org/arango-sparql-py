"""Typed error hierarchy for ``arango-sparql-py``.

Every error carries a stable ``code`` string so the FastAPI layer can
surface it to clients without leaking internals. See
``.cursor/rules/100-backend-python.mdc`` §"Errors" for the contract.
"""

from __future__ import annotations


class SparqlError(Exception):
    """Base class for every domain error in ``arango-sparql-py``."""

    code: str = "E_SPARQL"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class SparqlParseError(SparqlError):
    """Raised when ``rdflib`` cannot parse the SPARQL query string."""

    code = "E_SPARQL_PARSE"


class UnsupportedSparqlError(SparqlError):
    """Raised when the visitor encounters an Algebra node we do not yet
    translate. Never silently degrade — we want callers to know."""

    code = "E_SPARQL_UNSUPPORTED"


class SchemaResolutionError(SparqlError):
    """Raised when a SPARQL IRI cannot be resolved against the loaded OWL
    ontology to a physical ArangoDB collection / property."""

    code = "E_SCHEMA_RESOLVE"


class AqlEmitError(SparqlError):
    """Raised when the AQL builder cannot emit a well-formed query
    (e.g. dangling alias, unbound projection variable)."""

    code = "E_AQL_EMIT"


class CrossTenantJoinError(SparqlError):
    """Raised when a single SPARQL query joins two entities scoped
    under different ``tenantEntity`` roots.

    A cross-tenant join would broadcast across customer boundaries —
    a privacy / compliance violation that PRD §6.5.1 (and the
    §8.6 T12 mitigation) explicitly forbids. The HTTP layer maps
    this to ``422 E_TRANSLATE_CROSS_TENANT_JOIN``.
    """

    code = "E_TRANSLATE_CROSS_TENANT_JOIN"


#: Code a translated query raises *at run time* when a ``:p*`` / ``:p+`` path
#: reaches a node more than ``SchemaResolver.property_path_max_depth``
#: repetitions away (issue #183). The AQL ends in ``FAIL(@msg)``, whose message
#: starts with this code; ArangoDB reports it as ERR 1569 ("AQL: <message>").
#: Anyone who runs the translated AQL, the service or a caller that only
#: translates, gets the refusal instead of an answer missing those pairs.
PATH_DEPTH_EXCEEDED_CODE = "E_SPARQL_PATH_DEPTH"

#: ArangoDB's error number for a query stopped by ``FAIL()``.
ARANGO_ERROR_QUERY_FAIL_CALLED = 1569
