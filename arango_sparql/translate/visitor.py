"""Algebra visitor — one ``visit_<NodeType>`` method per rdflib Algebra
node. Unknown nodes raise :class:`UnsupportedSparqlError`.

See ``.cursor/skills/sparql-to-aql/SKILL.md`` for the porting recipe;
each method here corresponds to a function in the legacy
``references/arango-sparql/src/lib/*-translator.js`` files.

This module owns the per-query *binding state* (SPARQL variable →
AQL expression, SPARQL variable → physical document alias). The AQL
builder stays SPARQL-agnostic; everything SPARQL-specific lives here.
"""

from __future__ import annotations

import datetime
import decimal
import logging
from dataclasses import dataclass, field
from typing import Any

from rdflib import RDF, BNode, Literal, URIRef, Variable
from rdflib.paths import Path

from ..errors import (
    AqlEmitError,
    CrossTenantJoinError,
    SchemaResolutionError,
    UnsupportedSparqlError,
)
from .builder import (
    AqlQueryBuilder,
    attribute_ref,
    is_aql_identifier,
    subject_in,
    subject_matches,
    subject_ref,
    type_predicate,
)
from .filter_builtins import translate_builtin, translate_function
from .minus_exists import emit_minus
from .optional_crosssubject import (
    emit_rpt_cross_subject_optional,
    is_rpt_cross_subject_optional,
)
from .paths import emit_path_triple
from .resolver import ResolvedClass, ResolvedProperty, SchemaResolver
from .subselect import emit_to_multiset
from .union_paths import emit_union
from .variable_predicates import emit_variable_predicate_triple

logger = logging.getLogger(__name__)


@dataclass
class _BindingState:
    """Per-query SPARQL→AQL binding tables.

    Mirrors the ``variableMappings`` / ``documentMappings`` /
    ``fromClauses`` trio in the legacy
    ``references/arango-sparql/src/lib/pgt-translator.js`` so the port
    of each new visitor reads as a structural translation, not a
    rewrite.
    """

    var_to_expr: dict[str, str] = field(default_factory=dict)
    """SPARQL variable name → AQL expression that produces its value
    (e.g. ``?s`` → ``"doc1._uri"``, ``?n`` → ``"doc1.name"``)."""

    var_to_doc_alias: dict[str, str] = field(default_factory=dict)
    """SPARQL variable name → AQL FOR-loop alias whose document
    represents the SPARQL subject (only set for variables we ever bind
    to a physical document)."""

    doc_to_collection: dict[str, str] = field(default_factory=dict)
    """AQL alias → physical collection name. Used to detect duplicate
    FOR clauses and to drive future joins."""

    tenant_entity: str | None = None
    """The tenant root entity (e.g. ``"Org"``) the visitor has
    committed to for this query. Captured the first time a class
    with a ``tenant_field`` resolves; subsequent class resolutions
    that report a *different* ``tenant_entity`` raise
    :class:`~arango_sparql.errors.CrossTenantJoinError`. ``None``
    until the first tenant-scoped class is seen — single-tenant
    queries (no class declares ``phys:tenantEntity``) leave this
    untouched."""

    tenant_bind_placeholder: str | None = None
    """Cached ``@_pN_tenant`` bind placeholder so every tenant
    FILTER in the same query references the same bind variable.
    Without the cache each FOR would mint its own bind, multiplying
    the bind-vars dict and obscuring the AQL."""

    maybe_unbound: set[str] = field(default_factory=set)
    """SPARQL variables that can be unbound in a solution: bound by an
    OPTIONAL, a UNION arm that not every arm binds, a VALUES ``UNDEF``, a
    sub-select column that can be unbound, or an expression over one of
    these. A null in such a variable is SPARQL's *unbound*, which is an
    error inside SUM / AVG / MIN / MAX / GROUP_CONCAT (issue #59). A null
    anywhere else is a stored null on a required triple — no triple, which
    those aggregates already skip — and is left alone."""
    var_to_rpt_class: dict[str, ResolvedClass] = field(default_factory=dict)
    """SPARQL subject variable → :class:`ResolvedClass` for the RPT
    triples table that backs this subject's class.

    Populated by :meth:`AlgebraVisitor._emit_triple` when a type
    pattern resolves to ``style == "RPT"``. Subsequent property
    triples on the same variable use this entry to (a) know they
    must dispatch through the RPT triple-store reader (PRD §6.6 RPT
    row) and (b) read the per-class column overrides — different
    customer schemas rename the four ``subject_uri`` / ``predicate``
    / ``object_uri`` / ``object_value`` columns and the override
    must travel with the SUBJECT's class, not be re-derived per
    property triple. Variables bound by PG / LPG patterns are absent
    from this dict — that's how :meth:`_emit_triple` distinguishes a
    PG ``?s :p ?o`` from an RPT ``?s :p ?o``.
    """

    projection_vars: list[Variable] = field(default_factory=list)
    """The Project node's PV list, captured by ``visit_SelectQuery`` /
    ``visit_Project`` and consumed by ``_emit_projection``."""

    distinct: bool = False

    path_var_counter: int = 0
    bgp_counter: int = 0
    """Monotone counter for intermediate variables minted during
    property-path expansion (see :mod:`arango_sparql.translate.paths`).
    Lives on the binding state — not on the builder — because the
    expanded variable names (``?_path_<n>``) participate in the
    SPARQL variable namespace (they can be referenced from sibling
    triples in the same BGP after they're bound), even though the
    user can never refer to one explicitly. Reset implicitly on
    every new visitor instance, so per-query state is isolated."""

    graph_scope: list[Any] = field(default_factory=list)
    """Stack of active SPARQL named-graph scopes. Each entry is the
    ``GRAPH`` term — either an :class:`rdflib.URIRef` (constant graph
    IRI) or an :class:`rdflib.Variable` (graph variable bound by the
    pattern).

    Pushed by :meth:`AlgebraVisitor.visit_Graph` on entry, popped on
    exit, so nested ``GRAPH { GRAPH { … } }`` (legal but rare SPARQL)
    is handled by stack discipline. Every FOR-emitting site
    (:meth:`AlgebraVisitor._open_collection` today; extensible to
    property-path traversal and RPT readers later) consults the top of
    this stack via :meth:`AlgebraVisitor._apply_graph_scope` to attach
    the right ``FILTER`` (constant IRI) or variable binding
    (graph-variable).

    See ADR-0001 for the storage-model rationale; the field name on
    documents is :attr:`SchemaResolver.graph_field` (default
    ``"_graph"``). Empty list = no active GRAPH scope, which is the
    *default-graph* case handled per
    :attr:`SchemaResolver.default_graph_includes_named`."""

    optional_rebind_sink: list[tuple[str, str, str]] | None = None
    """Probe-mode collector for ``OPTIONAL`` triples that re-bind an
    already-bound variable. ``None`` outside a MINUS probe (the default),
    where such a re-bind is rejected as unsupported (ADR-0002 Problem 2).

    Inside a MINUS probe (set to ``[]`` by
    :func:`arango_sparql.translate.minus_exists._translate_probe`),
    ``visit_LeftJoin`` does **not** reject the re-bind: per SPARQL
    §18.2.5.2 an OPTIONAL over an in-scope variable is a *conditional
    add* (compatibility test, not a fresh binding). It emits the
    compatibility FILTER inline and appends
    ``(var_name, inner_value_expr, outer_bound_expr)`` here so
    ``emit_minus`` can build the disjoint-domain "overlap" guard
    (§8.3.4) the minuend semantics require."""


@dataclass
class AlgebraVisitor:
    """Walk an rdflib SPARQL Algebra tree and emit AQL via the builder."""

    builder: AqlQueryBuilder
    resolver: SchemaResolver
    explicit_projection: list[Variable] | None = None
    """Projection variables in their declared order, or ``None`` when the
    query used ``SELECT *``. Captured upstream by
    :func:`arango_sparql.translate.parser.parse_sparql` because the
    rdflib Algebra's ``Project.PV`` collapses into a non-deterministic
    set-iteration order. When ``None``, ``_emit_projection`` falls back
    to the visitor's own deterministic variable-binding order."""

    describe_resources: list[Any] | None = None
    """The DESCRIBE resource list in declared source order, or ``None``
    for non-DESCRIBE queries. Captured upstream by
    :func:`arango_sparql.translate.parser.parse_sparql` for the same
    PYTHONHASHSEED reason the projection list is captured: rdflib's
    Algebra ``DescribeQuery.PV`` is built from a set iteration. The
    visitor reads this in preference to ``node.PV`` so the AQL output
    is byte-for-byte stable across Python runs."""

    base_iri: str | None = None
    """Absolute IRI from the query's ``BASE`` directive (or ``None``).
    Threaded into ``IRI()`` / ``URI()`` so relative string arguments
    resolve per SPARQL §17.4.2.8."""

    tenant_id: str | None = None
    """Per-request tenant identifier sourced from the session's
    ``X-Tenant-Id`` header (or ``ARANGO_SPARQL_DEFAULT_TENANT`` env
    fallback). Visited entities whose :class:`ResolvedClass` carries
    a ``tenant_field`` get gated with
    ``FILTER doc.<tenant_field> == @tenant``. ``None`` means the
    caller has no tenant context — entities that *require* a tenant
    (those with ``tenant_field`` set) raise
    :class:`~arango_sparql.errors.CrossTenantJoinError` to refuse
    silently leaking data across tenants. Single-tenant deployments
    (no class declares ``tenant_field``) ignore this field entirely.
    See PRD §6.5.1."""

    extra_projection: list[str] | None = None
    """Variable names the caller needs in the RETURN object even when
    the partition's own SELECT list omits them — the federation entry
    point (:func:`arango_sparql.partition.translate_partition`) uses
    this to project canonical-key variables so the M5 engine can join
    legs without the planner rewriting the partition's projection.
    Appended after (and deduplicated against) the query's own
    projection; each var must be bound by the pattern or
    ``AqlEmitError`` is raised, exactly like a projected var. Post-
    COLLECT scoping caveat: a var that is neither a GROUP BY key nor
    an aggregate result is out of scope after a COLLECT, same as it
    would be if the user projected it themselves."""

    state: _BindingState = field(default_factory=_BindingState)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def visit(self, node: Any) -> Any:
        name = getattr(node, "name", None)
        if name is None:
            raise UnsupportedSparqlError(f"node has no .name attribute: {type(node).__name__}")
        method = getattr(self, f"visit_{name}", None)
        if method is None:
            return self.visit_unknown(node)
        return method(node)

    def visit_unknown(self, node: Any) -> Any:
        raise UnsupportedSparqlError(
            f"SPARQL Algebra node {node.name!r} is not implemented yet (see .cursor/skills/sparql-to-aql/SKILL.md)"
        )

    # ------------------------------------------------------------------
    # Top-level query nodes
    # ------------------------------------------------------------------
    def visit_SelectQuery(self, node: Any) -> Any:
        # rdflib wraps the body in Project (and possibly Distinct/Slice/etc.).
        # We just delegate; the inner visitors set ``state.projection_vars``
        # and emit the RETURN.
        self.visit(node.p)

    # ------------------------------------------------------------------
    # ASK — SELECT LIMIT 1 wrapped in LENGTH(...) > 0
    # ------------------------------------------------------------------
    def visit_AskQuery(self, node: Any) -> Any:
        # rdflib wraps the BGP in Project even for ASK (the projection
        # is just discarded). We deliberately bypass the Project so we
        # don't emit a stray RETURN { ... }, and instead drive the inner
        # pattern (BGP / FILTER / OPTIONAL / …) directly. The builder's
        # ASK mode then wraps the whole body in ``RETURN LENGTH(<inner>)
        # > 0`` at finalize() time — same recipe as the legacy
        # ``aql-translator.js#translateQuery`` ASK branch (``ASK is
        # essentially SELECT LIMIT 1``).
        inner = node.p
        if getattr(inner, "name", None) == "Project":
            inner = inner.p
        self.visit(inner)
        # LIMIT 1 short-circuits the cursor — we only need to know
        # whether *any* row matches, not how many.
        if self.builder._limit_clause is None:  # noqa: SLF001 - own builder
            self.builder.limit(1)
        self.builder.return_scalar("1")
        self.builder.set_ask_mode()

    # ------------------------------------------------------------------
    # CONSTRUCT — template-driven RDF output
    # ------------------------------------------------------------------
    def visit_ConstructQuery(self, node: Any) -> Any:
        """Translate ``CONSTRUCT { …template… } WHERE { …pattern… }``.

        rdflib shape::

            ConstructQuery(
                p = Project(BGP(...))           # optionally Distinct
                template = [(s, p, o), ...]      # the construct template
                datasetClause = None | [...]
            )

        Strategy (mirrors the legacy
        ``rpt-translator.js#translateConstructRPT`` /
        ``pgt-translator.js#translateConstructPGT`` shape):

        1. Drive the inner WHERE pattern (bypassing the synthetic
           ``Project`` rdflib stamps on top — same trick as
           :meth:`visit_AskQuery`) so every variable referenced by the
           template ends up bound in :attr:`_BindingState.var_to_expr`.
        2. For each template triple ``(s, p, o)`` build the AQL
           expression that yields its value at execution time, then
           ask the builder to emit a single ``RETURN
           [{subject,predicate,object}, …]`` clause. The route layer
           feeds each cursor row through the RDF renderer
           (:mod:`arango_sparql.service.protocol.results_rdf`), which
           dedupes triples via :class:`rdflib.Graph` set semantics
           and serialises into the negotiated RDF wire format.
        """

        template = list(getattr(node, "template", []) or [])

        inner = node.p
        # rdflib stamps a Project (and optionally Distinct/Slice) around
        # the WHERE; peel them off so we don't emit a stray RETURN { ... }
        # before our own ``RETURN [...]``. The Project's PV is the union
        # of vars referenced by the template — we don't need it because
        # we re-derive expressions directly from var_to_expr.
        while inner is not None and getattr(inner, "name", None) in (
            "Project",
            "Distinct",
        ):
            inner = inner.p
        if inner is None:
            # CONSTRUCT WHERE {} with no pattern is grammatically legal
            # but produces no bindings → no triples. Refuse so the
            # operator notices rather than emit unbound AQL.
            raise UnsupportedSparqlError("CONSTRUCT with an empty WHERE pattern is not supported")

        if not template:
            # ``CONSTRUCT WHERE { … }`` (template-less, SPARQL 1.1
            # §16.2.1 short-form) — synthesise the template from the
            # WHERE BGP's triples. Per the spec, the BGP itself
            # serves as both the matching pattern AND the output
            # template, so every matched binding row produces one
            # output triple per BGP triple. The inner BGP shape is
            # exactly the template shape; if WHERE is wrapped in
            # something more complex (Join, Filter, etc.) we collect
            # triples from every BGP descendant so a query like
            # ``CONSTRUCT WHERE { ?s :a ?o . FILTER (?o > 0) }``
            # still produces the right template.
            template = _collect_bgp_triples(inner)
            if not template:
                raise UnsupportedSparqlError(
                    "CONSTRUCT WHERE pattern has no BGP triples to synthesise a template from"
                )

        self.visit(inner)

        triple_exprs: list[tuple[str, str, str]] = []
        for s_term, p_term, o_term in template:
            triple_exprs.append(
                (
                    self._construct_term_to_aql(s_term, "subject"),
                    self._construct_term_to_aql(p_term, "predicate"),
                    self._construct_term_to_aql(o_term, "object"),
                )
            )

        self.builder.return_triples(triple_exprs)

    def _construct_term_to_aql(self, term: Any, position: str) -> str:
        """Render a CONSTRUCT/DESCRIBE template term as an AQL expression.

        * ``Variable``  → the variable's already-bound AQL expression
          (raises :class:`UnsupportedSparqlError` if the WHERE failed
          to bind it — that's a SPARQL contract violation by the user).
        * ``URIRef``    → bind as a ``@_pN_uri`` placeholder.
        * ``Literal``   → bind as a ``@_pN`` placeholder, with the
          Python-typed value preserved through ``Literal.toPython()``.
        * ``BNode``     → bind the lexical ``_:`` form so the renderer
          can rehydrate it. Identical blank-node labels in the same
          template re-bind to distinct ``@_pN`` placeholders because
          each :meth:`AqlQueryBuilder.bind` call mints a fresh
          placeholder; the renderer then uses the lexical form to
          re-link the labels across triples that share the same
          BNode.
        """

        if isinstance(term, Variable):
            mapped = self.state.var_to_expr.get(str(term))
            if mapped is None:
                raise UnsupportedSparqlError(
                    f"CONSTRUCT/DESCRIBE template {position} references "
                    f"unbound variable ?{term}; the WHERE pattern did "
                    f"not bind it"
                )
            return mapped
        if isinstance(term, URIRef):
            return self.builder.bind(str(term), hint="uri")
        if isinstance(term, Literal):
            return self.builder.bind(_term_to_python(term))
        if isinstance(term, BNode):
            return self.builder.bind(f"_:{term}", hint="bnode")
        raise UnsupportedSparqlError(
            f"CONSTRUCT/DESCRIBE template {position} term type {type(term).__name__!r} is not supported"
        )

    # ------------------------------------------------------------------
    # DESCRIBE — return all triples about a resource
    # ------------------------------------------------------------------
    def visit_DescribeQuery(self, node: Any) -> Any:
        """Translate ``DESCRIBE`` queries.

        Two rdflib shapes — distinguished by ``node.p``:

        * ``p is None``   → bare ``DESCRIBE <iri> [<iri> ...]``.
          PV is the explicit IRI list; no WHERE pattern to drive.
          v1.0 fallback (matches legacy ``describe-query-helper.js``):
          open a FOR over :attr:`SchemaResolver.default_collection`
          and FILTER on ``_uri``. Multi-IRI bare DESCRIBE is supported
          by ``IN [...]`` rather than emitting multiple FORs.

        * ``p`` is a SELECT-shaped subtree → ``DESCRIBE ?s WHERE { … }``.
          PV is the projection (typically one variable). We drive the
          inner WHERE pattern so every PV variable lands in
          :attr:`_BindingState.var_to_doc_alias` (PG/LPG) or
          :attr:`_BindingState.var_to_rpt_class` (RPT), then emit a
          per-resource ATTRIBUTES expansion or triple-store scan.

        The emitted ``RETURN`` produces *lists of triple dicts* (one
        list per WHERE binding); the route layer flattens them via the
        same renderer CONSTRUCT uses. Mirrors the legacy
        ``rpt-translator.js#translateDescribeRPT`` and
        ``pgt-translator.js#translateDescribePGT`` semantics, except
        we attach the inferred ``rdf:type`` triple on the fly for
        PG/LPG entities so a ``DESCRIBE ?p WHERE { ?p a :Person }``
        round-trips the class membership the renderer otherwise
        couldn't reconstruct from a property-graph row.
        """

        # Prefer the upstream-captured resource list (declared source
        # order) over the algebra's ``PV`` field, which rdflib builds
        # from a set iteration and is therefore PYTHONHASHSEED-unstable
        # — same trick as :attr:`explicit_projection`.
        if self.describe_resources is not None:
            pv = list(self.describe_resources)
        else:
            pv = list(getattr(node, "PV", []) or [])
        if not pv:
            raise UnsupportedSparqlError("DESCRIBE without a resource list is not supported")

        inner = node.p
        if inner is None:
            self._emit_describe_bare(pv)
            return

        # DESCRIBE ?var WHERE { ... } — peel off the SELECT-shaped
        # wrappers and drive the underlying pattern. Like CONSTRUCT,
        # we DON'T want the Project's ``RETURN { ... }`` so we strip
        # it before recursing.
        while getattr(inner, "name", None) in ("Distinct", "Project"):
            inner = inner.p
        self.visit(inner)

        described: list[str] = []
        for term in pv:
            if isinstance(term, Variable):
                described.append(self._describe_variable_subquery(term))
                continue
            if isinstance(term, URIRef):
                # ``DESCRIBE <iri> WHERE { … }`` is the legacy "describe
                # a constant in the context of a WHERE-derived alias
                # set". The inner pattern already opened a FOR; we
                # piggy-back its alias context.
                described.append(self._describe_uri_subquery(term))
                continue
            raise UnsupportedSparqlError(f"DESCRIBE term type {type(term).__name__!r} is not supported")

        if len(described) == 1:
            self.builder.return_triples_subquery(described[0])
        else:
            # Multiple described resources in one query: APPEND the
            # sub-lists so each binding row still produces a single
            # flat list of triples the route layer can hydrate.
            payload = "APPEND(" + ", ".join(described) + ")"
            self.builder.return_triples_subquery(payload)

    def _describe_variable_subquery(self, var: Variable) -> str:
        """Return the AQL sub-FOR that expands ``?var`` into triples.

        Dispatches on whether the variable was bound by a PG/LPG type
        pattern (we have a FOR alias and emit an ATTRIBUTES fan-out)
        or by an RPT type pattern (we re-scan the triples table
        filtered by ``subject_column == <subject_expr>``).
        """

        name = str(var)
        rpt_class = self.state.var_to_rpt_class.get(name)
        if rpt_class is not None:
            subj_expr = self.state.var_to_expr.get(name)
            if subj_expr is None:
                raise AqlEmitError(
                    f"DESCRIBE ?{name} on an RPT-bound variable is missing its subject expression"
                )
            return self._describe_rpt_subquery(subj_expr, rpt_class)
        alias = self.state.var_to_doc_alias.get(name)
        if alias is None:
            raise UnsupportedSparqlError(
                f"DESCRIBE ?{name} is unbound; the WHERE pattern must "
                f"bind every described variable to a physical document"
            )
        return self._describe_pg_attributes_subquery(alias)

    def _describe_uri_subquery(self, uri: URIRef) -> str:
        """Return the AQL sub-FOR that expands ``<iri>`` into triples
        in the context of an already-open WHERE-derived FOR.

        Opens a FOR over the default collection FILTERed by ``_uri ==
        <bind>`` so the URI is interpreted as a PG-style document. We
        deliberately reuse :meth:`_describe_pg_attributes_subquery` so
        the bind-vars / clause shape are identical to the variable
        path — easier to test and less code drift.
        """

        alias = self._open_collection(self.resolver.default_collection)
        bind = self.builder.bind(str(uri), hint="uri")
        self.builder.filter_raw(subject_matches(alias, bind))
        return self._describe_pg_attributes_subquery(alias)

    def _describe_pg_attributes_subquery(self, alias: str) -> str:
        """Return ``(FOR k IN ATTRIBUTES(<alias>) FILTER k NOT IN [<sys>]
        RETURN { subject: <alias>._uri, predicate: k, object: <alias>[k] })``.

        Mirrors the legacy ``describe-query-helper.js#buildPropertyExtraction``
        loop. The system-attribute exclusion list is the same five fields
        the legacy used — they're ArangoDB-internal metadata that has
        no RDF analogue.
        """

        # Match the legacy: hide ``_id``/``_key``/``_rev`` (ArangoDB
        # internal metadata) plus our resolver-synthesised ``_uri`` and
        # ``_type`` (covered separately by the ``rdf:type`` triple when
        # the inner BGP carries one). Keep the literal list inline —
        # binding it would force five extra ``@_pN`` placeholders for
        # zero readability gain.
        return (
            f"FOR k IN ATTRIBUTES({alias}) "
            f"FILTER k NOT IN ['_id', '_key', '_rev', '_uri', '_type'] "
            f"RETURN {{subject: {subject_ref(alias)}, predicate: k, "
            f"object: {alias}[k]}}"
        )

    def _describe_rpt_subquery(
        self,
        subject_expr: str,
        rpt_class: ResolvedClass,
    ) -> str:
        """Return the AQL sub-FOR that scans the RPT triples table for
        every row whose ``subject_column`` matches *subject_expr*.

        ``subject_expr`` is the AQL expression the outer WHERE already
        bound to the SPARQL subject — typically ``t1.subject_uri``
        from the type-pattern FOR. The sub-FOR opens a *second* alias
        over the same triples collection so it can scan every triple
        (not just the ``rdf:type`` row the type pattern keyed on).
        Mirrors ``rpt-translator.js#translateDescribeRPT``.
        """

        alias = self.builder.fresh_alias(prefix="d")
        coll_ref = self.builder.bind_collection(rpt_class.collection)
        coalesce = f"NOT_NULL({attribute_ref(alias, rpt_class.object_uri_column)}, {attribute_ref(alias, rpt_class.object_value_column)})"
        return (
            f"FOR {alias} IN {coll_ref} "
            f"FILTER {attribute_ref(alias, rpt_class.subject_column)} == {subject_expr} "
            f"RETURN {{subject: {attribute_ref(alias, rpt_class.subject_column)}, "
            f"predicate: {attribute_ref(alias, rpt_class.predicate_column)}, "
            f"object: {coalesce}}}"
        )

    def _emit_describe_bare(self, resources: list[Any]) -> None:
        """Emit AQL for ``DESCRIBE <iri> [<iri> ...]`` (no WHERE).

        v1 strategy: open ONE FOR over the resolver's
        :attr:`~SchemaResolver.default_collection` and FILTER ``_uri
        IN [...]`` against every described IRI. This favours PG/LPG
        deployments — the dominant case for Protégé-style
        ``DESCRIBE <Person123>`` queries.

        Pure-RPT deployments where the default collection is the
        triples store will currently produce a one-row-per-attribute
        result that the renderer flattens correctly *only when* the
        triples collection happens to be named via
        ``defaultCollection``. The cleaner RPT-bare-DESCRIBE path is
        tracked under PRD §6.6 as a v1.1 enhancement.
        """

        alias = self._open_collection(self.resolver.default_collection)
        iri_binds = [self.builder.bind(str(r), hint="uri") for r in resources]
        self.builder.filter_raw(subject_in(alias, f"[{', '.join(iri_binds)}]"))
        subquery = self._describe_pg_attributes_subquery(alias)
        self.builder.return_triples_subquery(subquery)

    def visit_Project(self, node: Any) -> Any:
        # Prefer the explicit declaration order captured upstream from
        # the parsed query. When the query was ``SELECT *``,
        # ``explicit_projection`` is ``None`` and we deliberately leave
        # ``projection_vars`` empty so ``_emit_projection`` falls into
        # the deterministic insertion-order branch — ``Project.PV`` for
        # ``SELECT *`` is a set-iteration order that varies across
        # Python runs (PYTHONHASHSEED randomization).
        if self.explicit_projection is not None:
            self.state.projection_vars = list(self.explicit_projection)
        self.visit(node.p)
        self._emit_projection()

    def visit_Distinct(self, node: Any) -> Any:
        self.state.distinct = True
        self.visit(node.p)

    def visit_Slice(self, node: Any) -> Any:
        # Visit the inner pattern first so FOR/FILTER/SORT come before LIMIT.
        # The Project wrapper (when present) will emit RETURN after we return.
        self.visit(node.p)
        start = int(getattr(node, "start", 0) or 0)
        length = getattr(node, "length", None)
        if length is None:
            # SPARQL 1.1 LIMIT/OFFSET is grammar-required to set both;
            # an offset-only query (no LIMIT) is rare but legal — punt
            # until we hit a real corpus example.
            raise UnsupportedSparqlError("SPARQL OFFSET without LIMIT is not yet supported")
        self.builder.limit(int(length), offset=start)

    # ------------------------------------------------------------------
    # FILTER — wraps an inner pattern with a boolean expression
    # ------------------------------------------------------------------
    def visit_Filter(self, node: Any) -> Any:
        # Visit the inner pattern first so all FORs are open and
        # ``var_to_expr`` is populated; rdflib places Filter ABOVE BGP
        # so this naturally emits FILTER after the FOR clauses, which
        # is the correct AQL evaluation order for cross-FOR filters.
        self.visit(node.p)
        # A Filter encountered after AggregateJoin is HAVING. The grouped
        # empty-input fallback belongs before HAVING; finalization cannot
        # safely move it across the filter yet, so disable it rather than
        # turn "all groups rejected" into one spurious empty binding.
        if self.builder._empty_result_fallback:  # noqa: SLF001 - own builder
            self.builder.clear_empty_result_fallback()
        aql_expr = self._translate_expr(node.expr)
        self.builder.filter_raw(aql_expr)

    # ------------------------------------------------------------------
    # OPTIONAL — LeftJoin(p1=required, p2=optional, expr=<inner FILTER>)
    # ------------------------------------------------------------------
    def visit_LeftJoin(self, node: Any) -> Any:
        # Visit the required side first so every alias / variable that
        # the optional side references is in scope.
        self.visit(node.p1)

        p2 = node.p2
        if getattr(p2, "name", None) != "BGP":
            # OPTIONAL { OPTIONAL { … } } and OPTIONAL { ?s :p ?o
            # FILTER(?o) UNION ?s :q ?o } both land here; defer until
            # we have a corpus example so we don't ship an
            # ad-hoc semantics for them.
            raise UnsupportedSparqlError(
                f"OPTIONAL whose body is {p2.name!r} (not a plain BGP) is not yet supported"
            )

        # RPT-native cross-subject OPTIONAL (ADR-0002 Problem 1, Option
        # A): the OPTIONAL subject is bound only as a *value* by the
        # required side, and we're in RPT mode, so the OPTIONAL is a
        # plain left-join scan of the triples table. Everything else
        # (PG/LPG/default cross-subject) still falls through to the
        # structured rejection below.
        if is_rpt_cross_subject_optional(self, p2, node):
            emit_rpt_cross_subject_optional(self, p2.triples[0], node)
            return

        # Walk the optional triples once to collect (var, source_expr)
        # pairs, *without* mutating ``var_to_expr`` yet — the inner
        # FILTER (if any) needs the optional vars in scope to translate
        # but we want to install the final (possibly conditional)
        # bindings after we've decided whether the OPTIONAL block needs
        # an all-or-nothing gate.
        #
        # ``new_bindings`` carries one entry per optional binding; the
        # source expression is either an attribute path
        # (``doc1.email``) for datatype properties or a LET alias
        # (``opt2``) for object properties. The downstream emitter does
        # not need to distinguish the two.
        new_bindings: list[tuple[str, str]] = []  # (sparql_var, source_expr)
        # Object-property bindings, emitted together below: an edge can match
        # more than once, so they need rows, not a scalar (issue #48).
        edge_bindings: list[
            tuple[str, ResolvedProperty, str, str]
        ] = []  # (var, prop, edge collection, subject alias)
        seen_vars: set[str] = set()
        for triple in getattr(p2, "triples", []) or []:
            s, p, o = triple
            # OPTIONAL semantics in AQL only stay simple when the
            # OPTIONAL block doesn't open a new FOR — the AQL "join" is
            # then either an attribute lookup on a doc we've already
            # opened (datatype property) or a single-step OUTBOUND
            # subquery from that doc (object property). Cross-subject
            # OPTIONAL (which would need a real subquery of the form
            # ``LET o = (FOR x IN coll FILTER … RETURN x)[0]``) is the
            # legacy ``aql-translator.js#processOptionalPatterns``
            # branch we'll port when there's a corpus need.
            if not isinstance(s, Variable) or str(s) not in self.state.var_to_doc_alias:
                raise UnsupportedSparqlError(
                    "OPTIONAL whose subject is not already bound by the required side "
                    "is not yet supported (cross-subject LEFT JOIN needs a subquery emitter)"
                )
            if not isinstance(p, URIRef):
                raise UnsupportedSparqlError("OPTIONAL with a variable predicate is not supported")
            if not isinstance(o, Variable):
                # OPTIONAL { ?s :p "literal" } would mean "test for
                # existence of this exact triple", which AQL can't
                # express without a subquery. Refuse for now.
                raise UnsupportedSparqlError("OPTIONAL with a non-variable object is not yet supported")
            o_name = str(o)
            is_rebind = o_name in self.state.var_to_expr
            if is_rebind and self.state.optional_rebind_sink is None:
                # The var was already bound by p1 and we are NOT inside a
                # MINUS probe — OPTIONAL re-binding an in-scope variable
                # would shift semantics from LEFT JOIN toward INNER JOIN.
                # Inside a probe (sink set) this is instead a valid
                # conditional-add per SPARQL §18.2.5.2 (handled below);
                # everywhere else we still reject (ADR-0002 Problem 2).
                raise UnsupportedSparqlError(
                    f"OPTIONAL re-binds variable ?{o_name} that's already bound by the required side"
                )
            prop = self.resolver.resolve_property(p)
            subject_alias = self.state.var_to_doc_alias[str(s)]
            if prop.is_object_property:
                if prop.edge_collection is None:
                    raise SchemaResolutionError(
                        f"object property {prop.iri!r} in OPTIONAL has no "
                        f"phys:edgeCollectionName annotation; the OWL ontology "
                        f"must declare which ArangoDB edge collection backs "
                        f"this relationship (PRD §6.2)"
                    )
                if not is_rebind:
                    edge_bindings.append((o_name, prop, prop.edge_collection, subject_alias))
                    seen_vars.add(o_name)
                    continue
                # Re-bound inside a MINUS / EXISTS probe: the compatibility test
                # below must hold for *some* matching edge (or there is none),
                # so the probe gets one row per edge target, padded with one
                # null row, and the per-row test is unchanged (issue #52; it
                # compared the first edge only).
                [(_, value_expr)] = self._emit_padded_rows(
                    [(o_name, prop, prop.edge_collection, subject_alias)], [], None
                )
            else:
                value_expr = attribute_ref(subject_alias, prop.attribute)

            if is_rebind:
                # Conditional-add inside a MINUS probe (SPARQL §18.2.5.2):
                # the optional triple does NOT introduce a fresh binding;
                # it tests compatibility with the value the variable
                # already carries. The optional "matches" iff its value
                # is absent (the optional didn't fire) or equals the
                # existing binding — a null on either side is vacuously
                # compatible. The disjoint-domain "overlap" guard
                # (§8.3.4) is assembled by ``emit_minus`` from the sink.
                bound_expr = self.state.var_to_expr[o_name]
                self.builder.filter_raw(
                    f"({value_expr} == null || {bound_expr} == null || {value_expr} == {bound_expr})"
                )
                assert self.state.optional_rebind_sink is not None
                self.state.optional_rebind_sink.append((o_name, value_expr, bound_expr))
                continue

            new_bindings.append((o_name, value_expr))
            seen_vars.add(o_name)

        if not new_bindings and not edge_bindings:
            # Empty OPTIONAL (e.g. only a FILTER, no triples) is a
            # no-op under our same-subject restriction — return cleanly
            # rather than emit dead AQL.
            return

        expr = getattr(node, "expr", None)
        has_filter = expr is not None and getattr(expr, "name", "") != "TrueFilter"

        # Rows, not scalars, whenever a binding can match more than once: an
        # edge (issue #48), or — under ``fan_out_list_values``, where a
        # list-valued attribute is one triple per element as in a required
        # triple — any attribute (issue #53).
        if edge_bindings or (new_bindings and self.resolver.fan_out_list_values):
            self._emit_optional_rows(edge_bindings, new_bindings, expr if has_filter else None)
            return

        # Fast path: a single new binding, no inner FILTER. For both
        # attribute-path bindings (``doc.attr`` returns null when the
        # attribute is missing — SPARQL's "unbound" already) and edge
        # LETs (the LET evaluates to null when the subquery returned no
        # rows), the source expression is itself the right semantics —
        # no extra null-coalescing needed.
        if not has_filter and len(new_bindings) == 1:
            var, source_expr = new_bindings[0]
            self.state.var_to_expr[var] = source_expr
            self.state.maybe_unbound.add(var)
            return

        # Multi-binding or filtered OPTIONAL needs the all-or-nothing
        # match condition. SPARQL semantics: the OPTIONAL block matches
        # as a unit, so if *any* triple in the group fails (or the
        # inner FILTER rejects the candidate), *every* var the block
        # would have bound becomes unbound.
        #
        # The null-check predicate works for both attribute paths and
        # edge LET aliases — both evaluate to ``null`` when the
        # underlying datum is missing, so ``<expr> != null`` is the
        # uniform "this binding matched" probe.
        null_checks = [f"{source_expr} != null" for _, source_expr in new_bindings]
        if has_filter:
            # Translate the FILTER with the optional vars resolving to
            # their source expressions (the FILTER references them by
            # SPARQL name); we'll rebind to the per-binding LET aliases
            # immediately after.
            for var, source_expr in new_bindings:
                self.state.var_to_expr[var] = source_expr
            aql_filter = self._translate_expr(expr)
            match_terms = [*null_checks, aql_filter]
        else:
            match_terms = null_checks
        match_expr = " && ".join(match_terms)
        if len(match_terms) > 1:
            match_expr = f"({match_expr})"
        for var, source_expr in new_bindings:
            alias = self.builder.fresh_alias(prefix="opt")
            self.builder.let(alias, f"({match_expr} ? {source_expr} : null)")
            self.state.var_to_expr[var] = alias
            self.state.maybe_unbound.add(var)

    def _emit_optional_rows(
        self,
        edge_bindings: list[tuple[str, ResolvedProperty, str, str]],
        attr_bindings: list[tuple[str, str]],
        filter_expr: Any,
    ) -> None:
        """An OPTIONAL block with object-property triples: emit its padded rows
        (:meth:`_emit_padded_rows`) and bind the block's variables to them."""
        for var, row_expr in self._emit_padded_rows(edge_bindings, attr_bindings, filter_expr):
            self.state.var_to_expr[var] = row_expr
            self.state.maybe_unbound.add(var)

    def _emit_padded_rows(
        self,
        edge_bindings: list[tuple[str, ResolvedProperty, str, str]],
        attr_bindings: list[tuple[str, str]],
        filter_expr: Any,
    ) -> list[tuple[str, str]]:
        """An OPTIONAL block with object-property triples, as a padded left join.

        Returns ``(sparql_var, row expression)`` per variable of the block and
        installs nothing: :meth:`_emit_optional_rows` binds the variables to
        them, and the MINUS / EXISTS re-bind test in :meth:`visit_LeftJoin`
        compares each row against the outer value instead (issue #52).

        SPARQL's LeftJoin keeps every compatible solution of the OPTIONAL
        block (SPARQL 1.1 §18.5), so a subject with two matching edges gives
        two solutions, and one that matches nothing keeps its single solution
        with the block's variables unbound. The block's matches become a
        correlated subquery and the outer query iterates them, padded with one
        ``null`` row when there are none — the shape the RPT cross-subject
        OPTIONAL already emits (``optional_crosssubject.py``)::

            LET <optsub> = (
              FOR <v>, <e> IN OUTBOUND <subject> @@<edges>   -- per edge triple
              [FILTER <e>.<type_field> == @<type>]          -- GENERIC_WITH_TYPE
              FILTER <subject>.<attr> != null               -- per attribute triple
            -- (under fan_out_list_values: FOR <lv> IN FLATTEN([<subject>.<attr>])
            --  FILTER <lv> != null, one row per list element; issue #53)
              [FILTER <inner filter>]
              RETURN {f0: <target identity>, f1: <subject>.<attr>, …}
            )
            FOR <optrow> IN (LENGTH(<optsub>) > 0 ? <optsub> : [null])

        The whole block is one subquery, so it still matches as a unit: an
        edge without the block's attribute, or a row the inner FILTER rejects,
        is not a match. It used to follow the first edge only
        (``LIMIT 1 … [0]``), losing a row per extra edge (issue #48). The
        legacy ``aql-translator.js#processOptionalPatterns`` did the same; this
        follows SPARQL, with pyoxigraph as the reference, instead.
        """
        child = self.builder.create_child()
        fields: list[tuple[str, str]] = []  # (sparql_var, source expression in the subquery)
        for var, prop, edge_collection, subject_alias in edge_bindings:
            v_alias = child.fresh_alias(prefix="v")
            e_alias = child.fresh_alias(prefix="e")
            child.for_traversal(v_alias, e_alias, subject_alias, edge_collection)
            # The same edge-type filter the required-side triple applies
            # (``_emit_edge_triple``).
            if prop.mapping_style == "GENERIC_WITH_TYPE" and prop.type_field and prop.type_value:
                bind = child.bind(prop.type_value, hint=prop.type_field)
                child.filter_eq(attribute_ref(e_alias, prop.type_field), bind)
            fields.append((var, subject_ref(v_alias)))
        for var, source_expr in attr_bindings:
            if self.resolver.fan_out_list_values:
                # One row per element, the fan-out a required triple gets
                # (``visit_BGP``): FLATTEN([attr]) is the list's elements, or
                # ``[scalar]``; a missing attribute is ``[null]``, no match.
                value_alias = child.fresh_alias(prefix="lv")
                child.for_inline(value_alias, self._fan_out_source(source_expr))
                child.filter_raw(f"{value_alias} != null")
                fields.append((var, value_alias))
            else:
                child.filter_raw(f"{source_expr} != null")
                fields.append((var, source_expr))
        if filter_expr is not None:
            # The inner FILTER reads the block's variables by SPARQL name, so
            # they resolve to their subquery expressions while it translates;
            # the visitor writes to the child meanwhile, so any bind variable
            # the FILTER needs is minted there and merged by absorb_child.
            for var, source_expr in fields:
                self.state.var_to_expr[var] = source_expr
            parent = self.builder
            self.builder = child
            try:
                aql_filter = self._translate_expr(filter_expr)
            finally:
                self.builder = parent
            child.filter_raw(aql_filter)
        names = [f"f{i}" for i in range(len(fields))]
        projection = ", ".join(f"{name}: {source}" for name, (_, source) in zip(names, fields, strict=True))
        child.return_scalar("{" + projection + "}")

        inner_aql = self.builder.absorb_child(child)
        indented = "\n".join("  " + line for line in inner_aql.splitlines())
        sub_alias = self.builder.fresh_alias(prefix="optsub")
        self.builder.let(sub_alias, f"(\n{indented}\n)")
        row_alias = self.builder.fresh_alias(prefix="optrow")
        self.builder.for_inline(row_alias, f"(LENGTH({sub_alias}) > 0 ? {sub_alias} : [null])")
        return [(var, f"{row_alias}.{name}") for name, (var, _source) in zip(names, fields, strict=True)]

    # ------------------------------------------------------------------
    # BIND / agg-result rename — Extend(p=inner, var=?v, expr=<sparql expr>)
    # ------------------------------------------------------------------
    def visit_Extend(self, node: Any) -> Any:
        # Visit the inner pattern first so every variable referenced by
        # the BIND expression is already in ``var_to_expr``. rdflib
        # always nests BIND beneath any pattern that produced its inputs
        # (the SPARQL grammar enforces this), so this ordering matches
        # the legal scope.
        self.visit(node.p)
        var = getattr(node, "var", None)
        if var is None:
            raise UnsupportedSparqlError("BIND node is missing its target variable")
        var_name = str(var)
        expr = node.expr

        # Pure rename short-circuit: ``Extend(expr=?other, var=?new)``.
        # rdflib emits this shape after every ``AggregateJoin`` to map
        # synthetic ``__agg_N__`` results onto the user's projection
        # aliases (e.g. ``__agg_1__`` → ``?c``), and SPARQL also allows
        # ``BIND(?other AS ?new)`` for plain aliasing. Re-pointing
        # ``var_to_expr`` is enough — emitting a LET would just shadow
        # the existing binding with no semantic change.
        #
        # The overwrite is intentional: rdflib's post-aggregation rename
        # of ``?d`` (group key) deliberately replaces the pre-COLLECT
        # ``?d → doc1.dept`` mapping with the new ``?d → grp1`` (the
        # COLLECT key alias). The pre-COLLECT alias is out of scope
        # after COLLECT, so the overwrite matches AQL scoping.
        if isinstance(expr, Variable) and str(expr) in self.state.var_to_expr:
            self.state.var_to_expr[var_name] = self.state.var_to_expr[str(expr)]
            if str(expr) in self.state.maybe_unbound:
                self.state.maybe_unbound.add(var_name)
            return

        if var_name in self.state.var_to_expr:
            # SPARQL forbids re-binding a variable already in scope —
            # rdflib should have raised, but defend in case the algebra
            # was hand-built (e.g. via the algebra module directly).
            raise UnsupportedSparqlError(
                f"BIND target ?{var_name} is already bound by the surrounding pattern"
            )
        aql_expr = self._translate_expr(expr)
        # Mint a fresh AQL identifier rather than reusing the SPARQL
        # name verbatim — ``?type`` and similar AQL-reserved words would
        # otherwise blow up at execution. The ``bv_`` (bind variable)
        # prefix keeps it visually distinct from FOR-loop aliases
        # (``doc1``, ``doc2``…) in the rendered query.
        alias = self.builder.fresh_alias(prefix="bv")
        self.builder.let(alias, aql_expr)
        self.state.var_to_expr[var_name] = alias
        if _expression_variable_names(expr) & self.state.maybe_unbound:
            self.state.maybe_unbound.add(var_name)

    # ------------------------------------------------------------------
    # GROUP BY + aggregates — AggregateJoin(A=[…aggs…], p=Group(p=BGP, expr=keys|None))
    # ------------------------------------------------------------------
    # Map rdflib ``Aggregate_<Name>_`` → AQL aggregate function. rdflib
    # synthesises a Sample for every GROUP BY variable the query projects
    # (the SPARQL spec's "any value within the group, since they're all
    # equal anyway"); those become COLLECT key aliases instead
    # (``_is_group_key_sample``). A SAMPLE the query writes over anything
    # else is a real aggregate: AQL's MAX returns one of the group's values
    # and skips null, as SPARQL's Sample skips an unbound value (#58).
    # The class names rdflib uses (e.g. ``Aggregate_Count``) — note no
    # trailing underscore, despite ``pprintAlgebra`` rendering them as
    # ``Aggregate_Count_{...}`` (the ``_{`` is a separator before the
    # attribute-dict repr, not part of the name).
    _SPARQL_AGG_TO_AQL = {
        "Aggregate_Count": "COUNT",
        "Aggregate_Sum": "SUM",
        "Aggregate_Avg": "AVG",
        "Aggregate_Min": "MIN",
        "Aggregate_Max": "MAX",
        "Aggregate_GroupConcat": "CONCAT_SEPARATOR",
        "Aggregate_Sample": "MAX",
    }

    @staticmethod
    def _is_group_key_sample(agg: Any, group_vars: set[str]) -> bool:
        """Whether *agg* is a Sample of a GROUP BY variable — a COLLECT key.

        rdflib synthesises one for each GROUP BY variable the query projects,
        and a ``SAMPLE(?d)`` the query writes over a grouping variable is the
        same value. Any other Sample — ``SAMPLE(?a)`` over a non-key variable,
        or any SAMPLE in an ungrouped query — picks a value from the group and
        must not group on it: treating it as a key returned one row per
        distinct value (issue #58).
        """
        return (
            agg.name == "Aggregate_Sample" and isinstance(agg.vars, Variable) and str(agg.vars) in group_vars
        )

    def visit_AggregateJoin(self, node: Any) -> Any:
        group = node.p
        if getattr(group, "name", None) != "Group":
            # rdflib always wraps an AggregateJoin in a Group (even
            # ungrouped queries get ``Group(expr=None)``); a different
            # inner node would mean a SPARQL shape we haven't seen.
            raise UnsupportedSparqlError(f"AggregateJoin inner is {group.name!r}, expected 'Group'")

        # Visit the BGP / OPTIONAL / FILTER chain so every variable the
        # aggregates and group keys reference is bound in var_to_expr.
        self.visit(group.p)
        # The pre-COLLECT scope, for aggregate arguments. Only variables bound
        # before the COLLECT are visible inside ``AGGREGATE`` — a COLLECT key
        # alias there is ArangoDB ERR 1512 ("use of unknown variable … in
        # AGGREGATE expression") — but the samples pass below rebinds each
        # GROUP BY variable to its key alias for the projection.
        pattern_scope = dict(self.state.var_to_expr)
        group_vars = {str(v) for v in (getattr(group, "expr", None) or []) if isinstance(v, Variable)}

        # Pre-allocate the COLLECT-key aliases for every Aggregate_Sample_
        # (one per GROUP BY variable), and rebind each GROUP BY variable to
        # its key alias — what the projection, HAVING and ORDER BY read after
        # the COLLECT. Aggregate *arguments* read ``pattern_scope`` instead:
        # ``COUNT(?d)`` over a grouping variable ``?d`` must count ``doc.d``,
        # since the key alias is not visible inside AGGREGATE.
        keys: list[tuple[str, str]] = []  # (alias, expression) for COLLECT
        sample_remap: dict[str, str] = {}  # __agg_N__ for samples → key alias
        key_alias_by_var: dict[str, str] = {}
        for agg in node.A:
            if not self._is_group_key_sample(agg, group_vars):
                continue
            sparql_var = str(agg.vars)
            # One COLLECT key per GROUP BY variable. A second Sample of it (a
            # ``SAMPLE(?d)`` the query writes beside the projected ``?d``)
            # reuses the key; it used to read the first key's alias as its
            # expression (``grp3 = grp2``), which a COLLECT key cannot see.
            key_alias = key_alias_by_var.get(sparql_var)
            if key_alias is None:
                attr_expr = pattern_scope.get(sparql_var)
                if attr_expr is None:
                    raise UnsupportedSparqlError(f"GROUP BY references unbound variable ?{sparql_var}")
                key_alias = self.builder.fresh_alias(prefix="grp")
                keys.append((key_alias, attr_expr))
                key_alias_by_var[sparql_var] = key_alias
            sample_remap[str(agg.res)] = key_alias
            # Also rebind the user-facing var: post-COLLECT it must
            # reference the COLLECT key alias, not the (now out-of-scope)
            # FOR alias's attribute.
            self.state.var_to_expr[sparql_var] = key_alias
            self.state.var_to_expr[str(agg.res)] = key_alias

        # Real aggregates: everything but the group-key Samples.
        aggregates: list[tuple[str, str]] = []  # (alias, "AGG(expr)")
        null_counts: dict[str, str] = {}  # argument expression → alias counting its nulls
        count_into: str | None = None
        non_sample_aggs = [a for a in node.A if not self._is_group_key_sample(a, group_vars)]

        # Fast-path: a single non-DISTINCT COUNT collapses to the AQL idiom
        # ``WITH COUNT INTO <c>``. That counts rows, while SPARQL's COUNT
        # counts the rows where its argument has a value (§18.5.1.2) and an
        # unbound variable is null here (issue #46), so the idiom stands in
        # only when every row binds the argument (:meth:`_counts_every_row`).
        is_count_shorthand = (
            len(non_sample_aggs) == 1
            and non_sample_aggs[0].name == "Aggregate_Count"
            and non_sample_aggs[0].distinct != "DISTINCT"
            and self._counts_every_row(non_sample_aggs[0], pattern_scope)
        )

        for agg in non_sample_aggs:
            agg_var = str(agg.res)
            distinct = agg.distinct == "DISTINCT"
            agg_alias = self.builder.fresh_alias(prefix="agg")

            if is_count_shorthand:
                count_into = agg_alias
                self.state.var_to_expr[agg_var] = agg_alias
                continue

            aql_func = self._SPARQL_AGG_TO_AQL.get(agg.name)
            if aql_func is None:
                raise UnsupportedSparqlError(f"SPARQL aggregate {agg.name!r} is not yet supported")

            if agg.name == "Aggregate_Count":
                if distinct and agg.vars == "*":
                    # Counting distinct *solutions* needs every in-scope
                    # variable as one value; ``COUNT_DISTINCT(1)`` (what this
                    # used to emit) is 1 whatever the data. Refuse until it is
                    # built and cross-validated.
                    raise UnsupportedSparqlError(
                        "COUNT(DISTINCT *) is not supported yet; count DISTINCT over named variables"
                    )
                # A variable the pattern never binds is unbound in every
                # solution, so it counts nothing: ``null`` below.
                arg_expr = self._aggregate_arg_expr(agg, pattern_scope, unbound="null")
                # AQL's COUNT and COUNT_DISTINCT both count null, and an
                # unbound variable is null here (issue #46), so count only
                # the rows where the argument has a value — except where
                # every row has one, and the cheaper COUNT is the same.
                if distinct:
                    # UNIQUE keeps null as one of the values; it is dropped
                    # where the count is read (projection / HAVING / ORDER
                    # BY), as GROUP_CONCAT applies its separator below.
                    aggregates.append((agg_alias, f"UNIQUE({arg_expr})"))
                    self.state.var_to_expr[agg_var] = f"LENGTH(REMOVE_VALUE({agg_alias}, null))"
                    continue
                if self._counts_every_row(agg, pattern_scope):
                    aggregates.append((agg_alias, f"COUNT({arg_expr})"))
                else:
                    aggregates.append((agg_alias, f"SUM({arg_expr} != null ? 1 : 0)"))
                    if not keys:
                        # Ungrouped over no rows, SUM is null and the
                        # SPARQL count is 0.
                        self.state.var_to_expr[agg_var] = f"NOT_NULL({agg_alias}, 0)"
                        continue
            elif agg.name == "Aggregate_GroupConcat":
                # SPARQL GROUP_CONCAT defaults to a single-space
                # separator; the user can override via ``SEPARATOR=…``,
                # which rdflib stores as an rdflib ``Literal`` on
                # ``agg.separator``. Push the separator through
                # ``_term_to_python`` so it goes into the bind-vars
                # dict as a plain string (rdflib Literals don't
                # JSON-encode cleanly).
                #
                # ``CONCAT_SEPARATOR`` is NOT a legal ``COLLECT
                # AGGREGATE`` function (ArangoDB ERR 1574 "invalid
                # aggregate expression" at runtime) — the AGGREGATE
                # clause accepts only the fixed builtin set. So we
                # aggregate the group's values with ``PUSH`` (``UNIQUE``
                # under DISTINCT) and apply CONCAT_SEPARATOR wherever
                # the aggregate variable is *read* (projection / HAVING),
                # via the var_to_expr rebinding below.
                # ``CompValue.get`` is not ``dict.get``: its second
                # positional argument controls variable substitution. Calling
                # ``agg.get("separator", " ")`` therefore returned the key
                # name ``"separator"`` when the clause omitted SEPARATOR.
                arg_expr = self._aggregate_arg_expr(agg, pattern_scope)
                raw_sep = getattr(agg, "separator", None)
                separator = _term_to_python(raw_sep) if raw_sep is not None else " "
                sep_bind = self.builder.bind(separator, hint="sep")
                collect_func = "UNIQUE" if distinct else "PUSH"
                aggregates.append((agg_alias, f"{collect_func}({arg_expr})"))
                read = f"CONCAT_SEPARATOR({sep_bind}, {agg_alias})"
                if self._argument_may_be_unbound(agg):
                    read = self._unbound_is_error(read, arg_expr, aggregates, null_counts)
                    self.state.maybe_unbound.add(agg_var)
                self.state.var_to_expr[agg_var] = read
                continue
            else:
                if distinct and agg.name != "Aggregate_Sample":
                    # (SAMPLE(DISTINCT x) is SAMPLE(x): any value either way.)
                    # SUM / AVG / MIN / MAX with DISTINCT: AQL doesn't
                    # have native DISTINCT-aggregates, so we'd need to
                    # rewrite into a COLLECT … INTO subquery. Refuse
                    # for now so the operator notices.
                    raise UnsupportedSparqlError(
                        f"DISTINCT is only supported on COUNT aggregates; got DISTINCT inside {agg.name!r}"
                    )
                arg_expr = self._aggregate_arg_expr(agg, pattern_scope)
                aggregates.append((agg_alias, f"{aql_func}({arg_expr})"))
                read = agg_alias
                if not keys and agg.name in ("Aggregate_Sum", "Aggregate_Avg"):
                    # Ungrouped over no rows, AQL's SUM / AVG are null; SPARQL's
                    # Sum and Avg of an empty group are 0 (SPARQL 1.1
                    # §18.5.1.3-4) — the read COUNT already uses (issue #49).
                    # MIN / MAX of an empty group are unbound in both.
                    read = f"NOT_NULL({agg_alias}, 0)"
                if agg.name in ("Aggregate_Min", "Aggregate_Max", "Aggregate_Sample"):
                    self.state.maybe_unbound.add(agg_var)
                # SAMPLE skips an unbound value, as COUNT does; the others fail.
                if agg.name != "Aggregate_Sample" and self._argument_may_be_unbound(agg):
                    read = self._unbound_is_error(read, arg_expr, aggregates, null_counts)
                    self.state.maybe_unbound.add(agg_var)
                self.state.var_to_expr[agg_var] = read
                continue

            self.state.var_to_expr[agg_var] = agg_alias

        # Ungrouped queries with no aggregates would just be a COLLECT
        # producing one row of nothing — that's a SPARQL shape we don't
        # see in practice (``SELECT ?x WHERE { … } GROUP BY ?x`` always
        # has at least the Aggregate_Sample_ for ?x). Defer until we
        # have a real corpus example.
        if not keys and not aggregates and count_into is None:
            raise UnsupportedSparqlError(
                "AggregateJoin with neither GROUP BY keys nor aggregates is not supported"
            )

        self.builder.collect(keys=keys, aggregates=aggregates, count_into=count_into)
        if keys:
            self.builder.set_empty_result_fallback()

    def _aggregate_arg_expr(self, agg: Any, scope: dict[str, str], *, unbound: str | None = None) -> str:
        """Translate the argument of a SPARQL aggregate to an AQL expression.

        Handles three input shapes rdflib produces:
          * ``vars == '*'`` (only legal for COUNT(*)) → AQL ``1``
          * ``vars`` is a Variable → look up in ``scope``, the pre-COLLECT
            bindings (a GROUP BY variable's key alias is not visible inside
            ``AGGREGATE``). A variable the pattern never binds returns
            ``unbound`` when the caller gives one (COUNT: ``null``, which
            counts nothing) and is refused otherwise.
          * ``vars`` is a richer expression node → fall through to the
            FILTER expression translator.
        """
        v = agg.vars
        if v == "*":
            return "1"
        if isinstance(v, Variable):
            mapped = scope.get(str(v))
            if mapped is None:
                if unbound is not None:
                    return unbound
                raise UnsupportedSparqlError(f"aggregate references unbound variable ?{v}")
            return mapped
        return self._translate_expr(v)

    def _counts_every_row(self, agg: Any, scope: dict[str, str]) -> bool:
        """Whether ``COUNT(<agg's argument>)`` equals the number of rows.

        True for ``COUNT(*)``, and for a variable bound to the identity of a
        document this builder scans at the top level
        (:meth:`AqlQueryBuilder.scanned_aliases`): that document exists in
        every row, and ``subject_ref`` falls back to its ``_id``, so the value
        is never null. Anything else — an attribute, an OPTIONAL variable, a
        UNION arm's column, an expression — may be null in some rows.
        """
        if agg.vars == "*":
            return True
        if not isinstance(agg.vars, Variable):
            return False
        expr = scope.get(str(agg.vars))
        return expr is not None and any(
            expr == subject_ref(alias) for alias in self.builder.scanned_aliases()
        )

    def _argument_may_be_unbound(self, agg: Any) -> bool:
        """Whether the aggregate's argument can be unbound in some solution
        (it is, or mentions, a variable in ``state.maybe_unbound``)."""
        v = agg.vars
        if v == "*":
            return False
        names = {str(v)} if isinstance(v, Variable) else _expression_variable_names(v)
        return bool(names & self.state.maybe_unbound)

    def _unbound_is_error(
        self, read: str, arg_expr: str, aggregates: list[tuple[str, str]], null_counts: dict[str, str]
    ) -> str:
        """Wrap an aggregate's read so an unbound value in its group makes it unbound.

        SPARQL evaluates SUM / AVG / MIN / MAX / GROUP_CONCAT over the
        group's values, and an unbound one is an error that makes the whole
        result an error, which projects as unbound (SPARQL 1.1 §18.5.1;
        pyoxigraph agrees). AQL's SUM / AVG / MIN / MAX skip null and
        CONCAT_SEPARATOR drops it, so the group's nulls are counted beside the
        aggregate (one count per argument) and the read is null when there
        are any (issue #59). Over no rows the count is null, ``null > 0`` is
        false, and the read stands — SUM / AVG still give 0 (#49).
        """
        nulls = null_counts.get(arg_expr)
        if nulls is None:
            nulls = self.builder.fresh_alias(prefix="nulls")
            aggregates.append((nulls, f"SUM({arg_expr} == null ? 1 : 0)"))
            null_counts[arg_expr] = nulls
        return f"({nulls} > 0 ? null : {read})"

    # ------------------------------------------------------------------
    # ORDER BY — list of OrderConditions, each with expr + direction
    # ------------------------------------------------------------------
    def visit_OrderBy(self, node: Any) -> Any:
        # Visit the inner pattern first so every variable referenced by
        # the order conditions is already bound in ``var_to_expr``. The
        # builder buffers SORT clauses separately from body clauses and
        # finalize() always renders them in the canonical body→SORT
        # →LIMIT→RETURN order, so we don't need to think about whether
        # rdflib placed OrderBy below Project (above LIMIT) or above
        # Project (below LIMIT).
        self.visit(node.p)
        conditions = list(getattr(node, "expr", []) or [])
        if not conditions:
            return
        for cond in conditions:
            inner = getattr(cond, "expr", None)
            if inner is None:
                raise UnsupportedSparqlError("ORDER BY condition is missing its expression")
            aql_expr = self._translate_expr(inner)
            order = (getattr(cond, "order", None) or "ASC").upper()
            if order not in ("ASC", "DESC"):
                raise UnsupportedSparqlError(
                    f"ORDER BY direction {order!r} is not supported (expected ASC or DESC)"
                )
            self.builder.sort(aql_expr, descending=(order == "DESC"))

    # ------------------------------------------------------------------
    # JOIN — Join(p1, p2). Visit both sides; the BGP emitter's
    # already-bound-variable detection (in ``_emit_triple`` /
    # ``_bind_subject``) turns shared variables into AQL equality
    # FILTERs, so the AQL plan ends up with one FOR per FOR-eligible
    # pattern and the cross-product gets pruned to the SPARQL join.
    # ------------------------------------------------------------------
    def visit_Join(self, node: Any) -> Any:
        # rdflib's algebra often pairs a Builtin_EXISTS / probe
        # subquery's inner pattern as ``Join(BGP[], BGP[triples])``
        # where the empty-BGP appears as p1. Per SPARQL 1.1 §18.5
        # the empty BGP is join's identity, but our ``visit_BGP``
        # has to manufacture a degenerate ``FOR _ IN [1]`` opener
        # when no other FOR has been emitted yet (otherwise an
        # empty WHERE / BIND-only query has nowhere to attach its
        # LETs and projection). Walking p2 first when p1 is the
        # empty BGP lets the non-empty side open its FOR first;
        # the empty BGP's visit then becomes a true no-op
        # (``has_for_clause()`` is true) and we keep the same
        # AQL shape as if the Join hadn't been there.
        p1, p2 = node.p1, node.p2
        if _is_empty_bgp(p1) and not _is_empty_bgp(p2):
            p1, p2 = p2, p1
        self.visit(p1)
        self.visit(p2)

    # ------------------------------------------------------------------
    # MINUS — Minus(p1, p2). Outer rows whose shared variables are
    # incompatible with every inner row are kept; matched ones drop.
    # Delegated to ``arango_sparql.translate.minus_exists`` for the
    # shared probe-and-FILTER recipe with EXISTS / NOT EXISTS.
    # PRD §6.6 Minus row.
    # ------------------------------------------------------------------
    def visit_Minus(self, node: Any) -> Any:
        emit_minus(self, node)

    # ------------------------------------------------------------------
    # UNION — Union(p1, p2). Bag-union of two pattern's binding sets.
    # Delegated to ``arango_sparql.translate.union_paths`` which
    # shares its two-phase emitter with AlternativePath. PRD §6.6.
    # ------------------------------------------------------------------
    def visit_Union(self, node: Any) -> Any:
        emit_union(self, node)

    # ------------------------------------------------------------------
    # GRAPH <iri> { … } / GRAPH ?g { … } — SPARQL 1.1 §8.3.
    # Push the named-graph term onto the binding state's
    # ``graph_scope`` stack, visit the inner pattern (which may open
    # any number of FORs), then pop. Each FOR opened while the stack
    # is non-empty consults :meth:`_apply_graph_scope` to attach the
    # right filter or variable binding against the document's
    # ``<resolver.graph_field>`` attribute.
    # See ADR-0001 for the storage-model rationale.
    # ------------------------------------------------------------------
    def visit_Graph(self, node: Any) -> Any:
        term = node.term
        if not isinstance(term, URIRef | Variable):
            # SPARQL grammar permits constant IRIs and variables in
            # the GRAPH slot. rdflib will never hand us anything else
            # (literal / BNode in that slot is a parse error), but
            # guard explicitly so a future rdflib refactor surfaces
            # as a typed error rather than a silent miscompile.
            raise UnsupportedSparqlError(
                f"GRAPH term must be a URIRef or Variable, got "
                f"{type(term).__name__}; check the SPARQL grammar"
            )
        self.state.graph_scope.append(term)
        try:
            self.visit(node.p)
        finally:
            # Pop in finally so an exception in the inner pattern
            # doesn't leave the scope stack corrupted for any
            # try/except higher up the call chain.
            self.state.graph_scope.pop()

    # ------------------------------------------------------------------
    # ToMultiSet — SPARQL sub-SELECT and VALUES. Delegated to
    # ``arango_sparql.translate.subselect`` so this file stays under
    # the 1500-line cap from
    # ``.cursor/rules/modularity-and-structure.mdc``. See
    # :func:`emit_to_multiset` for the per-branch AQL shape.
    # PRD §6.6 ToMultiSet row.
    # ------------------------------------------------------------------
    def visit_ToMultiSet(self, node: Any) -> Any:
        emit_to_multiset(self, node)

    # ------------------------------------------------------------------
    # BGP — the heart of every SELECT
    # ------------------------------------------------------------------
    def visit_BGP(self, node: Any) -> Any:
        triples = list(getattr(node, "triples", []) or [])

        # SPARQL §17.4.1.10 / §18.5: a blank node in a query
        # pattern is an *existential variable scoped to the BGP*.
        # rdflib does not auto-substitute these — they survive
        # into the algebra as ``BNode`` terms in the subject /
        # object slot. Treat each unique BNode label within this
        # BGP as a freshly-minted internal Variable so the
        # downstream emitters see the standard variable
        # machinery (anchor a FOR, bind into ``var_to_expr``,
        # equality-join when the same label appears twice).
        #
        # Scope is per-BGP: if the same ``_:b0`` label appears
        # in a sibling BGP (e.g. another arm of UNION), each BGP
        # gets its own substitution — that's what
        # ``_bn_<bgp_counter>_<label>`` encodes. The
        # underscore-sigil naming follows the same convention as
        # property-path intermediate variables (``_path_<n>``),
        # so user-supplied SPARQL variable names can't collide.
        if triples:
            triples = self._substitute_bnode_existentials(triples)

        if not triples:
            # SPARQL 1.1 §18.5: the empty BGP is the *identity* for
            # join — it produces a single solution mapping with no
            # bindings. AQL has no native concept of an "empty
            # iteration"; every FILTER / LET / RETURN must attach to
            # at least one FOR.
            #
            # Two cases to honour the empty-BGP semantic correctly:
            #
            # * If no FOR has been emitted yet, this empty BGP is
            #   the *whole pattern* (or at least the only
            #   binding-producing side seen so far). Open a
            #   degenerate FOR over ``[1]`` so downstream Extend /
            #   Filter / Project clauses have somewhere to attach.
            # * Otherwise a sibling pattern (Join's other arm,
            #   the outer pattern when this BGP is under MINUS /
            #   EXISTS, etc.) has already opened a FOR. The empty
            #   BGP is join's identity in that case — emit
            #   nothing, since adding a degenerate ``FOR x IN [1]``
            #   would Cartesian-product against the existing FORs
            #   and inflate the AQL plan (correct results, but
            #   wasted work and visible regressions in the pinned
            #   AQL of MINUS / EXISTS / Union goldens).
            #
            # Concretely, this unblocks ``BIND(<expr> AS ?x)``-only
            # and ``SELECT (<expr> AS ?x) WHERE { }`` shapes where
            # the projection consists entirely of constant or
            # BIND-derived expressions with no triple matching, while
            # staying a no-op when an empty BGP appears under a Join,
            # MINUS, EXISTS, or Union arm with a non-empty sibling.
            if not self.builder.has_for_clause():
                empty_alias = self.builder.fresh_alias(prefix="empty")
                self.builder.for_inline(empty_alias, "[1]")
            return

        # Order matters here: a type pattern (``?s a :Person``) carries
        # the strongest hint about which physical collection ``?s`` lives
        # in, so we want to visit those first and bind ``?s`` to the
        # right alias before any sibling property triple opens a fallback
        # FOR over the default collection. ``rdflib.algebra.translateQuery``
        # may reorder triples for join optimization, undoing the user's
        # declaration order; this re-sort is the visitor's defense.
        for triple in sorted(triples, key=_triple_priority):
            self._emit_triple(triple)

    # ------------------------------------------------------------------
    # Internal: property-path intermediate-variable minting
    # ------------------------------------------------------------------
    def _substitute_bnode_existentials(
        self, triples: list[tuple[Any, Any, Any]]
    ) -> list[tuple[Any, Any, Any]]:
        """Replace every ``BNode`` term in *triples* with a freshly-
        minted internal ``Variable`` per the SPARQL §17.4.1.10 /
        §18.5 rule that blank nodes in query patterns are
        existentially-quantified variables scoped to the BGP.

        Scope is THIS BGP only — each call gets its own
        ``label → Variable`` mapping. The same ``_:b0`` label
        appearing across two triples of the same BGP maps to the
        ONE fresh variable (so the implicit join fires); the same
        label appearing in a sibling BGP gets a different fresh
        variable (so cross-BGP joins do NOT fire).

        The sigil ``_bn_<bgp_id>_<label>`` is private by
        construction — user SPARQL variable names can't legally
        start with ``_bn_`` and the BGP counter advances per
        call so a label that appears in two BGPs gets two
        distinct internal names.

        Predicate-position BNodes are deliberately left alone:
        they're invalid SPARQL syntactically (rdflib's parser
        rejects them) and we'd rather surface that as a
        downstream "predicate type unsupported" error than
        silently rewrite to a variable predicate.
        """
        self.state.bgp_counter += 1
        bgp_id = self.state.bgp_counter
        label_to_var: dict[str, Variable] = {}

        def _convert(term: Any) -> Any:
            if not isinstance(term, BNode):
                return term
            label = str(term)
            if label not in label_to_var:
                label_to_var[label] = Variable(f"_bn_{bgp_id}_{label}")
            return label_to_var[label]

        out: list[tuple[Any, Any, Any]] = []
        for s, p, o in triples:
            # Only subject + object slots get the substitution;
            # predicate-position BNodes fall through to the
            # downstream "unsupported predicate" error path.
            new_s = _convert(s)
            new_o = _convert(o)
            out.append((new_s, p, new_o))
        return out

    def _fresh_path_var(self) -> Variable:
        """Mint a fresh ``Variable`` for property-path intermediate joins.

        Called from :func:`arango_sparql.translate.paths._emit_sequence_path`
        when desugaring ``?s :p1/:p2/.../:pN ?o`` — every inner join
        point gets one of these (N-1 per sequence). The sigil prefix
        ``_path_`` is private by construction: the existing visitor
        never minted underscore-prefixed variables before property
        paths landed, and user SPARQL variables in the projection are
        captured from the pre-translation parsed query (so an
        ``?_path_3`` typed by a user would still be visible there and
        never collide with what we generate here).
        """

        self.state.path_var_counter += 1
        return Variable(f"_path_{self.state.path_var_counter}")

    # ------------------------------------------------------------------
    # Internal: triple-pattern emission
    # ------------------------------------------------------------------
    def _emit_triple(self, triple: tuple[Any, Any, Any]) -> None:
        s, p, o = triple

        # Case 0 — property paths (SequencePath / InvPath / ...). Dispatch
        # to the dedicated path expander module, which recursively desugars
        # the path and re-enters this method for each sub-fragment. Done
        # BEFORE Case 1 (rdf:type) because rdflib's ``Path`` instances are
        # NOT URIRef subclasses (verified at module-import time), so a
        # path predicate cleanly skips Case 1 — but doing the check first
        # makes the dispatch order explicit and immune to a future rdflib
        # refactor that might change the type relationship.
        if isinstance(p, Path):
            emit_path_triple(self, s, p, o)
            return

        # Case 1 — type pattern: ``?s a :Person`` (or ``<uri> a :Person``).
        # Mirrors PGTTranslator.isTypePattern in pgt-translator.js: open a
        # FOR over the class's physical collection and bind ?s to <alias>._uri.
        # RPT-style classes branch into the triple-store reader (PRD §6.6).
        if isinstance(p, URIRef) and p == RDF.type and isinstance(o, URIRef):
            resolved = self.resolver.resolve_class(o)
            if resolved.style == "RPT":
                self._emit_rpt_type_pattern(s, o, resolved)
                return
            alias = self._open_collection(resolved.collection, resolved=resolved)
            if resolved.type_field and resolved.type_value:
                # Hybrid (multi-class) collection: the mapper emits a
                # discriminator field; gate the FOR with it so we don't
                # bleed unrelated documents into the result set.
                bind = self.builder.bind(
                    resolved.type_value,
                    hint=resolved.type_field if is_aql_identifier(resolved.type_field) else None,
                )
                self.builder.filter_raw(
                    type_predicate(alias, resolved.type_field, bind, is_array=resolved.type_field_is_array)
                )
            self._enforce_tenant_scope(alias, resolved)
            self._bind_subject(s, alias)
            return

        # Case 2 — predicate is a fixed IRI (the common ``?s :name ?n`` shape).
        if isinstance(p, URIRef):
            # RPT subjects route through the triple-store reader. We
            # check ``var_to_rpt_class`` BEFORE resolving the property
            # because RPT property triples don't open a per-property
            # FOR — every read is from the same triples table — and
            # we don't want to materialise an UNMAPPED-IRI warning
            # for predicates that exist only as triple-store
            # ``predicate`` column values.
            if isinstance(s, Variable) and str(s) in self.state.var_to_rpt_class:
                self._emit_rpt_property_triple(s, p, o, triple)
                return
            prop = self.resolver.resolve_property(p)
            if prop.is_object_property:
                self._emit_edge_triple(s, prop, o, triple)
                return
            alias = self._ensure_subject_alias(s)
            attr_path = attribute_ref(alias, prop.attribute)
            if isinstance(o, Variable):
                # Predicate-existence filter (SPARQL 1.1 §18.5): a
                # required BGP triple ``?s :p ?o`` only matches when
                # the triple ``(s, p, o)`` actually exists in the
                # graph. In our flattened doc model that means the
                # attribute ``alias.<prop.attribute>`` must be present
                # on the document — otherwise the subject contributes
                # zero solutions and must be excluded from the result.
                # ``HAS(alias, "attr")`` distinguishes "missing" from
                # "explicit JSON null" cleanly, mirroring the legacy
                # ``pgt-translator.js#L598`` HAS()-guarded property
                # access. OPTIONAL doesn't enter here — ``visit_LeftJoin``
                # has its own emission path that bypasses ``_emit_triple``
                # so optional bindings still get the SPARQL-spec "leave
                # unbound when missing" semantics.
                if is_aql_identifier(prop.attribute):
                    self.builder.filter_raw(f'HAS({alias}, "{prop.attribute}")')
                else:
                    # A non-identifier name travels as a bind variable, never
                    # spliced into the AQL text (attribute_ref above has
                    # already refused names AQL cannot quote).
                    attr_bind = self.builder.bind(prop.attribute, hint="attr")
                    self.builder.filter_raw(f"HAS({alias}, {attr_bind})")
                existing = self.state.var_to_expr.get(str(o))
                if existing is None:
                    if self.resolver.fan_out_list_values:
                        # RDF multi-valued semantics: a list-valued
                        # attribute is N triples, so ``?o`` must bind
                        # once per element. Scalars wrap into a
                        # one-element list so the loop is uniform
                        # (see SchemaResolver.fan_out_list_values).
                        value_alias = self.builder.fresh_alias(prefix="lv")
                        self.builder.for_inline(value_alias, self._fan_out_source(attr_path))
                        self._record_var_expr(o, value_alias)
                    else:
                        self._record_var_expr(o, attr_path)
                elif existing != attr_path:
                    # The variable is already bound by an earlier
                    # triple to a different AQL expression — turn the
                    # implicit SPARQL join into an explicit AQL
                    # equality FILTER so the cross-product gets pruned.
                    # This is what makes multi-subject BGPs and ``Join``
                    # nodes correct: without the FILTER the engine
                    # would happily return the full Cartesian product.
                    # Under fan-out semantics the join is a membership
                    # test: the bound value must be ONE OF the
                    # attribute's values.
                    self.builder.filter_raw(self._value_match_expr(attr_path, existing))
                # else: the same expression is already bound — the
                # triple just re-states what we already knew, no-op.
            elif isinstance(o, (Literal, URIRef)):
                bind = self.builder.bind(
                    _term_to_python(o),
                    # The hint only decorates the placeholder name; skip it
                    # for a name that is not an identifier (it used to raise).
                    hint=prop.attribute if is_aql_identifier(prop.attribute) else None,
                )
                if self.resolver.fan_out_list_values:
                    self.builder.filter_raw(self._value_match_expr(attr_path, bind))
                else:
                    self.builder.filter_eq(attr_path, bind)
            else:
                raise UnsupportedSparqlError(
                    f"object term type {type(o).__name__!r} is not supported in triple {triple!r}"
                )
            return

        # Case 3 — variable predicate (``?s ?p ?o``). Delegated to
        # ``arango_sparql.translate.variable_predicates`` so this
        # file stays under the 1500-line cap from
        # ``.cursor/rules/modularity-and-structure.mdc``. The emitter
        # dispatches on the SUBJECT's binding state (RPT vs PG / LPG
        # / default-collection); see :func:`emit_variable_predicate_triple`
        # for the per-branch AQL shape + the W3C-spec carve-out.
        # Legacy reference: ``references/arango-sparql/src/lib/
        # pgt-translator.js`` lines 244-261, which hard-coded a
        # four-collection UNION rather than driving the fan-out off
        # the resolver. See PRD §6.6 Variable-predicates row.
        if isinstance(p, Variable):
            emit_variable_predicate_triple(self, s, p, o, triple)
            return

        raise UnsupportedSparqlError(
            f"unsupported triple shape: subject={type(s).__name__}, "
            f"predicate={type(p).__name__}, object={type(o).__name__}"
        )

    # ------------------------------------------------------------------
    # Internal: alias / FOR-clause management
    # ------------------------------------------------------------------
    def _open_collection(
        self,
        collection: str,
        *,
        resolved: ResolvedClass | None = None,
    ) -> str:
        """Mint a fresh alias and emit a FOR clause over *collection*.

        Plain case: emits ``FOR <alias> IN <collection>``. The legacy
        code dedupes by ``fromClauses`` set; we mint a fresh alias per
        call for now (the optimizer in ArangoDB collapses identical
        FORs in the common case). Deduplication can land alongside
        multi-triple BGP join optimization.

        Sharded case (PRD §6.5.3): when *resolved* is supplied and its
        :attr:`ResolvedClass.shard_family` is non-``None``, the FOR is
        replaced by a ``FOR <alias> IN UNION_DISTINCT((FOR a IN
        @@shard1 RETURN a), …)`` fan-out spanning every family member.
        Downstream FILTERs / RETURNs reference ``alias`` exactly the
        way they would for a plain FOR — the union row exposes the
        same columns as a row from any single shard. The builder also
        prepends ``WITH @@shard1, @@shard2, …`` to the rendered query
        so the cluster optimiser locks the family at parse time.

        ``resolved`` is optional because some FOR sites (notably the
        default-collection fallback in :meth:`_ensure_subject_alias`)
        have no ``ResolvedClass`` in hand — the default collection
        cannot, by definition, belong to a customer-declared shard
        family, so plain FOR is the only correct emission there.
        """
        alias = self.builder.fresh_alias()
        if resolved is not None and resolved.shard_family is not None:
            # Sharded class: fan out over every member of the family.
            # The visitor still tracks the alias under the *base*
            # collection (the one the resolver selected) so subsequent
            # joins / tenant scoping match exactly the same way they
            # would for a single-shard deployment — downstream code
            # never has to special-case shard fan-out.
            self.builder.for_sharded(alias, resolved.shard_family)
        else:
            self.builder.for_(alias, collection)
        self.state.doc_to_collection[alias] = collection
        # Named-graph scoping: when a SPARQL GRAPH wrapper is active
        # (visit_Graph pushed onto state.graph_scope), every FOR
        # opened inside it gets a filter on the document's graph
        # field. Outside any GRAPH wrapper this is also where
        # strict default-graph mode (default_graph_includes_named=False)
        # injects ``FILTER alias.<graph_field> == null`` — kept in
        # one place so PG / LPG / RPT collections all share the same
        # scoping recipe. See ADR-0001.
        self._apply_graph_scope(alias)
        return alias

    def _apply_graph_scope(self, alias: str) -> None:
        """Attach the active named-graph scope to a freshly-opened FOR.

        Called from :meth:`_open_collection` immediately after the FOR
        is emitted (and after :meth:`_enforce_tenant_scope` runs, so the
        graph filter sits adjacent to the tenant filter when both
        apply). Three cases:

        1. **No active GRAPH scope, lax default (the v0.9 default).**
           ``default_graph_includes_named=True`` — every document is
           visible regardless of its ``graph_field`` value. Emit
           nothing; preserves the existing translation goldens'
           AQL shape.
        2. **No active GRAPH scope, strict mode.**
           ``default_graph_includes_named=False`` — restrict to
           documents in the default graph. Emit
           ``FILTER alias.<graph_field> == null`` so docs without
           the attribute (or with it explicitly null) match, named
           graphs do not.
        3. **Active GRAPH scope.** The scope term is either a
           constant ``URIRef`` (one bind variable, one ``FILTER ==``)
           or a ``Variable``. For a variable: if it's already bound
           in ``var_to_expr`` (e.g. the GRAPH variable also appears
           outside the scope), emit an equality filter against the
           prior binding so the join fires; otherwise this FOR is
           the canonical binding and we record
           ``var_to_expr[?g] = alias.<graph_field>`` for downstream
           SELECT / FILTER / additional FORs to reuse.
        """
        graph_field = self.resolver.graph_field
        lhs = attribute_ref(alias, graph_field)
        if not self.state.graph_scope:
            if not self.resolver.default_graph_includes_named:
                # Strict default-graph mode — match SPARQL §8.3
                # semantics literally. AQL treats a missing attribute
                # as null, so this also matches docs that pre-date
                # the graph_field convention.
                self.builder.filter_raw(f"{lhs} == null")
            return

        graph_term = self.state.graph_scope[-1]
        if isinstance(graph_term, URIRef):
            bind = self.builder.bind(str(graph_term), hint="graph")
            self.builder.filter_eq(lhs, bind)
            return

        # graph_term is a Variable (validated in visit_Graph).
        var_name = str(graph_term)
        existing = self.state.var_to_expr.get(var_name)
        if existing is None:
            # First FOR inside this scope: this alias's graph_field
            # is the canonical binding for ?g. Downstream sibling FORs
            # in the same scope will fall into the ``existing`` branch
            # and emit an equality FILTER, which is how SPARQL's
            # implicit-join semantics fire for the graph variable.
            self.state.var_to_expr[var_name] = lhs
        else:
            # ?g is already bound — either by a prior FOR in this
            # same GRAPH scope, or by a sibling pattern outside the
            # GRAPH wrapper that binds ?g from a different source.
            # Either way the join is the same shape: equality between
            # this alias's graph_field and the existing expression.
            self.builder.filter_raw(f"{lhs} == {existing}")

    def _enforce_tenant_scope(self, alias: str, resolved: ResolvedClass) -> None:
        """Emit the per-entity tenant FILTER for an opened FOR alias.

        PRD §6.5.1: every read of a tenant-scoped entity gets a
        ``FILTER doc.<tenant_field> == @tenant_id`` predicate so the
        result set never crosses tenant boundaries. The bind value
        comes from the visitor's ``tenant_id`` (typically populated
        from the session's ``X-Tenant-Id`` header) and is cached on
        the binding state so every FOR in the same query references
        the same bind variable.

        Cross-tenant joins are rejected here too: when a second
        class in the same BGP reports a ``tenant_entity`` that
        differs from the one already committed to, the visitor
        raises :class:`CrossTenantJoinError` rather than emit AQL
        that joins across tenant roots. Two classes that share the
        same ``tenant_entity`` value (e.g. ``Person`` and ``Doc``
        both rooted at ``"Org"``) compose freely.
        """
        if resolved.tenant_field is None:
            return
        if resolved.tenant_entity is not None:
            committed = self.state.tenant_entity
            if committed is None:
                self.state.tenant_entity = resolved.tenant_entity
            elif committed != resolved.tenant_entity:
                raise CrossTenantJoinError(
                    f"SPARQL query joins entities across tenant roots "
                    f"{committed!r} and {resolved.tenant_entity!r}; "
                    f"cross-tenant joins are forbidden (PRD §6.5.1)"
                )
        if self.tenant_id is None:
            # A tenant-scoped class with no tenant context would
            # silently leak rows across tenants — refuse rather than
            # emit unbound AQL.
            raise CrossTenantJoinError(
                f"class {resolved.iri!r} is tenant-scoped under "
                f"{resolved.tenant_entity or '<unspecified>'!r} but no "
                f"tenant context was supplied to the translator "
                f"(missing X-Tenant-Id header or "
                f"ARANGO_SPARQL_DEFAULT_TENANT env)"
            )
        if self.state.tenant_bind_placeholder is None:
            self.state.tenant_bind_placeholder = self.builder.bind(self.tenant_id, hint="tenant")
        self.builder.filter_eq(
            attribute_ref(alias, resolved.tenant_field),
            self.state.tenant_bind_placeholder,
        )

    def _emit_rpt_type_pattern(
        self,
        subject: Any,
        class_iri: URIRef,
        resolved: ResolvedClass,
    ) -> None:
        """Emit the AQL for ``?s a :C`` against an RPT-style class.

        RPT (RDF Property Triples, PRD §6.1 / §6.6) stores every
        triple as a row in a denormalised ``_triples``-style
        collection with ``subject_uri`` / ``predicate`` /
        ``object_uri`` / ``object_value`` columns (overridable per
        class via ``phys:*Column``). A type pattern ``?s a :C``
        becomes a row scan with two equality FILTERs — one on the
        ``rdf:type`` predicate IRI and one on the class IRI in the
        ``object_uri`` column — and binds ``?s`` to the row's
        ``subject_uri`` value so subsequent property triples on the
        same variable can read the same triples table.

        Mirrors the legacy ``rpt-translator.js`` ``translateSelectRPT``
        loop's first branch.
        """
        triples_alias = self._open_collection(resolved.collection, resolved=resolved)
        if resolved.tenant_field is not None:
            # RPT rows live in a denormalised triples table whose
            # columns are fixed (subject_uri / predicate / object_uri
            # / object_value); a per-document ``tenant_field`` would
            # not exist as a column. Refuse rather than silently
            # skip — a misconfigured tenant-scoped RPT class would
            # otherwise leak rows across tenants.
            raise CrossTenantJoinError(
                f"RPT-style class {resolved.iri!r} declares "
                f"phys:tenantField {resolved.tenant_field!r} but RPT "
                f"triples rows have fixed columns and no tenant "
                f"discriminator; tenant scoping for RPT is not "
                f"supported in v1.0 (PRD §6.5.1)"
            )
        rdf_type_bind = self.builder.bind(str(RDF.type), hint="rdftype")
        class_bind = self.builder.bind(str(class_iri), hint="cls")
        self.builder.filter_eq(attribute_ref(triples_alias, resolved.predicate_column), rdf_type_bind)
        self.builder.filter_eq(attribute_ref(triples_alias, resolved.object_uri_column), class_bind)
        if isinstance(subject, Variable):
            name = str(subject)
            subj_expr = attribute_ref(triples_alias, resolved.subject_column)
            existing_alias = self.state.var_to_doc_alias.get(name)
            existing_expr = self.state.var_to_expr.get(name)
            if existing_alias is None and existing_expr is None:
                # First binding — record the RPT context AND the
                # subject-URI expression so a follow-up property
                # triple knows it's RPT-bound.
                self.state.var_to_doc_alias[name] = triples_alias
                self.state.var_to_expr[name] = subj_expr
                self.state.var_to_rpt_class[name] = resolved
                return
            # ``?s`` was already bound by an earlier triple — could
            # be a sibling type pattern (multi-class subject) or a
            # PG/RPT mixed binding. Emit an equality FILTER so the
            # cross-product collapses to the SPARQL join.
            existing_uri_expr = (
                subject_ref(existing_alias)
                if existing_alias and name not in self.state.var_to_rpt_class
                else existing_expr
            )
            if existing_uri_expr != subj_expr:
                self.builder.filter_raw(f"{subj_expr} == {existing_uri_expr}")
            # If the prior binding was PG (no rpt_class entry) and
            # this one is RPT, record the RPT class too so subsequent
            # property triples on this var dispatch through the RPT
            # reader. The PG side already has its own FOR open and
            # the equality FILTER joins them — exactly the §3.4
            # mixed-model BGP join.
            self.state.var_to_rpt_class.setdefault(name, resolved)
            return
        if isinstance(subject, URIRef):
            uri_bind = self.builder.bind(str(subject), hint="uri")
            self.builder.filter_eq(attribute_ref(triples_alias, resolved.subject_column), uri_bind)
            return
        raise UnsupportedSparqlError(
            f"RPT type pattern subject term type {type(subject).__name__!r} is not supported"
        )

    def _emit_rpt_property_triple(
        self,
        subject: Variable,
        predicate: URIRef,
        obj: Any,
        triple: tuple[Any, Any, Any],
    ) -> None:
        """Emit the AQL for ``?s :p ?o`` where ``?s`` is RPT-bound.

        Opens a fresh FOR over the same triples table, FILTERs by the
        predicate IRI and joins on the subject URI captured in
        :attr:`_BindingState.var_to_expr`. Object binding follows the
        legacy ``rpt-translator.js`` shape:

        * Variable object → bind to
          ``NOT_NULL(t.object_uri, t.object_value)`` so the same var
          can later be joined against either a URI or a literal
          column from another triple.
        * IRI object → equality FILTER on either ``object_uri`` or
          ``object_value`` (legacy permissively matched both columns
          to handle datasets where the loader stored URIs in the
          value column).
        * Literal object → equality FILTER on ``object_value`` only,
          since RDF literals never live in the URI column.
        """
        rpt_class = self.state.var_to_rpt_class[str(subject)]
        triples_alias = self._open_collection(rpt_class.collection, resolved=rpt_class)
        # Record the per-class metadata so the OBJECT variable, if
        # bound here, joins through the same column overrides the
        # subject's class declared.
        pred_bind = self.builder.bind(str(predicate), hint="pred")
        self.builder.filter_eq(attribute_ref(triples_alias, rpt_class.predicate_column), pred_bind)
        # Join on subject URI: the subject's value lives in
        # ``var_to_expr`` already (set by the type pattern emitter).
        subj_expr = self.state.var_to_expr.get(str(subject))
        if subj_expr is None:
            raise AqlEmitError(f"RPT property triple references unbound subject ?{subject}")
        new_subj_expr = attribute_ref(triples_alias, rpt_class.subject_column)
        if subj_expr != new_subj_expr:
            self.builder.filter_raw(f"{new_subj_expr} == {subj_expr}")
        # OBJECT — variable / IRI / literal each take a different shape.
        coalesce_expr = (
            f"NOT_NULL({attribute_ref(triples_alias, rpt_class.object_uri_column)}, "
            f"{attribute_ref(triples_alias, rpt_class.object_value_column)})"
        )
        if isinstance(obj, Variable):
            o_name = str(obj)
            existing = self.state.var_to_expr.get(o_name)
            if existing is None:
                self.state.var_to_expr[o_name] = coalesce_expr
            elif existing != coalesce_expr:
                # Object var was bound by a prior triple to a
                # different expression (PG ``doc._uri``, another RPT
                # COALESCE on a different alias, or an attribute
                # lookup); emit equality FILTER so the cross-product
                # gets pruned to the SPARQL join — same recipe as
                # the PG path.
                self.builder.filter_raw(f"{coalesce_expr} == {existing}")
            return
        if isinstance(obj, URIRef):
            uri_bind = self.builder.bind(str(obj), hint="obj")
            # Match either column — datasets that loaded URIs into
            # ``object_value`` (rare but observed in legacy fixtures)
            # still bind correctly. Mirrors the legacy
            # ``rpt-translator.js`` OR-filter.
            self.builder.filter_raw(
                f"({attribute_ref(triples_alias, rpt_class.object_uri_column)} == {uri_bind} "
                f"|| {attribute_ref(triples_alias, rpt_class.object_value_column)} == {uri_bind})"
            )
            return
        if isinstance(obj, Literal):
            val_bind = self.builder.bind(_term_to_python(obj), hint="obj")
            self.builder.filter_eq(attribute_ref(triples_alias, rpt_class.object_value_column), val_bind)
            return
        raise UnsupportedSparqlError(
            f"RPT property triple object term type {type(obj).__name__!r} "
            f"is not supported (triple {triple!r})"
        )

    def _emit_edge_triple(
        self,
        subject: Any,
        prop: Any,
        obj: Any,
        triple: tuple[Any, Any, Any],
    ) -> None:
        """Emit an AQL traversal for an object-property triple.

        Implements the PRD §6.1 relationship styles:

        * ``DEDICATED_COLLECTION`` (PG-typed edge) — one edge collection
          per relationship type → ``FOR v, e IN OUTBOUND <s> @@edgeColl``.
        * ``GENERIC_WITH_TYPE`` (LPG-typed edge) — shared edge collection
          discriminated by ``phys:typeField`` / ``phys:typeValue`` →
          the same traversal plus ``FILTER e.<typeField> == @<typeValue>``.

        ``RPT_EDGE`` (RDF triple-store object property) is NOT routed
        here — it goes through the ``_triples`` reader once the RPT
        emitter lands (PRD §6.6 RPT row, tracked separately).

        The traversal target vertex's ``_uri`` is bound to the SPARQL
        object the same way the BGP entity reader binds subject ``_uri``,
        so a chain like ``?a :knows ?b . ?b a :Person ; :name ?n``
        joins on ``?b._uri`` automatically via the existing
        :meth:`_bind_subject` machinery — no new join logic needed.
        """
        if prop.mapping_style == "RPT_EDGE":
            # Reached only when the subject is NOT RPT-bound (an RPT-bound
            # subject takes _emit_rpt_property_triple). An RPT_EDGE's
            # "edge collection" is the triples table — a document
            # collection — so a traversal over it fails at execution with
            # ERR 1218. Refuse at translate time instead of emitting AQL
            # that cannot run. Found by the schema_live tier: LPG ``Doc``
            # subject + RPT ``TAGGED_WITH`` predicate.
            raise UnsupportedSparqlError(
                f"object property {prop.iri!r} is stored as RDF triples "
                f"(RPT_EDGE) but its subject is bound to a non-RPT "
                f"collection; joining a PG/LPG subject onto triples rows is "
                f"not supported yet (PRD §3.4 / §6.6 mixed-model row). Bind "
                f"the subject through its RPT class, or map the relationship "
                f"to an edge collection"
            )
        if prop.edge_collection is None:
            raise SchemaResolutionError(
                f"object property {prop.iri!r} has no phys:edgeCollectionName "
                f"annotation; the OWL ontology must declare which ArangoDB "
                f"edge collection backs this relationship (PRD §6.2)"
            )

        subject_alias = self._ensure_subject_alias(subject)
        v_alias = self.builder.fresh_alias(prefix="v")
        e_alias = self.builder.fresh_alias(prefix="e")
        self.builder.for_traversal(v_alias, e_alias, subject_alias, prop.edge_collection)
        # ``GENERIC_WITH_TYPE`` shares one edge collection across many
        # relationship types; the discriminator FILTER is what keeps an
        # ``?a :knows ?b`` traversal from also returning ``:worksAt``
        # / ``:livesIn`` rows that happen to ride the same collection.
        if prop.mapping_style == "GENERIC_WITH_TYPE" and prop.type_field and prop.type_value:
            bind = self.builder.bind(prop.type_value, hint=prop.type_field)
            self.builder.filter_eq(attribute_ref(e_alias, prop.type_field), bind)
        # We track the edge alias on the builder for the rare query that
        # references the edge document itself; ``v_alias`` is the
        # traversal vertex and is what binds to the SPARQL object.
        self.state.doc_to_collection[v_alias] = prop.edge_collection
        target_uri_expr = subject_ref(v_alias)

        if isinstance(obj, Variable):
            o_name = str(obj)
            existing_alias = self.state.var_to_doc_alias.get(o_name)
            existing_expr = self.state.var_to_expr.get(o_name)
            if existing_alias is None and existing_expr is None:
                # First time we see ``?o`` — treat ``v_alias`` as ``?o``'s
                # subject document so a follow-up ``?o a :Person`` /
                # ``?o :name ?n`` reuses this alias instead of opening a
                # new (unrelated) FOR over the default collection.
                self._bind_subject(obj, v_alias)
                return
            # Object var already bound (typically by a prior type
            # pattern like ``?b a :Person`` placed BEFORE the edge
            # triple, or by another edge that landed on ``?b``). Emit
            # an equality filter on ``_uri`` so the cross-product gets
            # pruned to the SPARQL join semantics, mirroring the
            # ``_bind_subject`` branch for repeat type patterns.
            existing_uri_expr = subject_ref(existing_alias) if existing_alias else existing_expr
            if existing_uri_expr != target_uri_expr:
                self.builder.filter_raw(f"{target_uri_expr} == {existing_uri_expr}")
            return

        if isinstance(obj, URIRef):
            bind = self.builder.bind(str(obj), hint="uri")
            self.builder.filter_raw(subject_matches(v_alias, bind))
            return

        # SPARQL technically allows literal objects on object properties
        # (``?s :rel "foo"``), but RDF semantics make the triple match
        # iff the literal IS the IRI — vanishingly rare in practice and
        # ill-defined for our document model. Refuse for now.
        raise UnsupportedSparqlError(
            f"object property {prop.iri!r} with non-IRI object is not supported (triple {triple!r})"
        )

    def _ensure_subject_alias(self, subject: Any) -> str:
        """Return the AQL alias whose document is *subject*, opening a
        fresh FOR over the default collection if we haven't seen this
        subject before.

        For a URI subject (``<http://...>``) we open a default-collection
        FOR and add an ``_uri`` filter — same shape as the legacy
        ``pattern.subject.termType === 'NamedNode'`` branch.

        Path-aware join enforcement: a Variable that has a prior
        ``var_to_expr`` binding but no ``var_to_doc_alias`` binding
        arises when an earlier triple bound the variable to an
        attribute expression (``doc1.p``) without ever materialising
        it as a document. Property-path sequence expansion produces
        exactly this shape: ``?s :p/:q ?o`` desugars to
        ``?s :p ?_path_1 . ?_path_1 :q ?o``, and the second triple's
        ``_ensure_subject_alias(?_path_1)`` call needs to join the
        fresh FOR's ``_uri`` to the prior expression so the
        intermediate is not a free Cartesian-product variable. Object
        triples already handle this on the right-hand side
        (``_emit_triple`` Case 2's Variable branch); we mirror that
        on the subject side here.
        """
        if isinstance(subject, Variable):
            existing = self.state.var_to_doc_alias.get(str(subject))
            if existing is not None:
                return existing
            if self.resolver.strict_subject_resolution:
                raise SchemaResolutionError(
                    f"cannot route subject variable {str(subject)!r} to a "
                    f"collection: it has no rdf:type constraint to map it to a "
                    f"class, and strict subject resolution is enabled (no "
                    f"default-collection fallback). Add a type pattern such as "
                    f"'{str(subject)} a :YourClass' so the translator knows "
                    f"which collection to scan."
                )
            alias = self._open_collection(self.resolver.default_collection)
            # ``_bind_subject`` joins the new FOR to any prior binding of
            # the variable (the implicit join the SPARQL spec demands).
            self._bind_subject(subject, alias)
            return alias
        if isinstance(subject, URIRef):
            if self.resolver.strict_subject_resolution:
                raise SchemaResolutionError(
                    f"cannot route subject IRI {str(subject)!r} to a collection: "
                    f"no owl:Class with a phys:collectionName covers it, and "
                    f"strict subject resolution is enabled (no default-collection "
                    f"fallback). Give the subject a type pattern ('{str(subject)} "
                    f"a :YourClass'), or map its collection in the ontology."
                )
            alias = self._open_collection(self.resolver.default_collection)
            bind = self.builder.bind(str(subject), hint="uri")
            self.builder.filter_raw(subject_matches(alias, bind))
            return alias
        raise UnsupportedSparqlError(f"subject term type {type(subject).__name__!r} is not supported")

    def _bind_subject(self, subject: Any, alias: str) -> None:
        """Record that *alias*'s document represents the SPARQL *subject*.

        If the subject Variable is already bound to a *different* alias
        (which happens when two type patterns hit the same variable —
        e.g. ``?s a :Person . ?s a :Employee``), emit an equality
        FILTER to enforce the multi-class constraint rather than
        silently dropping the second alias's relationship to the
        variable. Otherwise the second FOR would float free of the
        first and the engine would emit the Cartesian product.

        The same holds when the variable already has a *value* but no
        document: a VALUES row, a BIND, an earlier triple's object, a
        sub-select column, or — inside a MINUS / EXISTS probe — the outer
        row's identity, which ``minus_exists`` pre-seeds so that "shared
        variables turn into equality FILTERs". The new FOR's identity must
        equal that value. Without the FILTER the FOR ranges over every
        document of the class: a cross product, or a probe that ignores the
        outer row (issue #51). This was enforced only on the
        default-collection path (``_ensure_subject_alias``); a type pattern
        reaches here directly.
        """
        if not isinstance(subject, Variable):
            return
        name = str(subject)
        existing_alias = self.state.var_to_doc_alias.get(name)
        if existing_alias is None:
            self.state.var_to_doc_alias[name] = alias
            prior_expr = self.state.var_to_expr.get(name)
            new_expr = subject_ref(alias)
            if prior_expr is None:
                self.state.var_to_expr[name] = new_expr
            elif prior_expr != new_expr:
                self.builder.filter_raw(f"{new_expr} == {prior_expr}")
            return
        if existing_alias == alias:
            return
        self.builder.filter_raw(f"{subject_ref(alias)} == {subject_ref(existing_alias)}")

    def _value_match_expr(self, attr_path: str, value_expr: str) -> str:
        """Equality between an attribute read and a value expression.

        Plain ``==`` normally. Under ``fan_out_list_values`` a
        list-valued attribute represents multiple triples, so the
        match is a membership test over ``FLATTEN([attr])`` — which
        is ``[v1, v2, …]`` for a list attribute and ``[scalar]`` for
        a scalar, so one ``IN`` covers both cases (verified against a
        live ArangoDB). ``FLATTEN`` (vs. an ``IS_LIST`` ternary) keeps
        the emission free of the conditional the fan-out FOR also
        avoids."""
        if self.resolver.fan_out_list_values:
            return f"{value_expr} IN FLATTEN([{attr_path}])"
        return f"{attr_path} == {value_expr}"

    def _fan_out_source(self, attr_path: str) -> str:
        """The list a fan-out FOR iterates for *attr_path*:
        ``FLATTEN([attr])`` — the attribute's elements when it is a
        list, or a one-element ``[scalar]`` otherwise."""
        return f"FLATTEN([{attr_path}])"

    def _record_var_expr(self, var: Variable, expr: str) -> None:
        # First binding wins, matching legacy semantics: a variable that
        # appears in two triples gets its first-seen expression and any
        # later occurrence is enforced via FILTER (handled when we wire
        # multi-triple BGP joins).
        self.state.var_to_expr.setdefault(str(var), expr)

    # ------------------------------------------------------------------
    # FILTER expression translation
    # ------------------------------------------------------------------
    # SPARQL → AQL operator map for RelationalExpression. SPARQL uses
    # ``=`` / ``!=`` for equality; AQL needs ``==`` / ``!=``.
    _RELATIONAL_OP_MAP = {
        "=": "==",
        "!=": "!=",
        "<": "<",
        "<=": "<=",
        ">": ">",
        ">=": ">=",
    }

    def _translate_expr(self, expr: Any) -> str:
        """Translate a SPARQL Algebra expression node to an AQL expression
        string.

        Returns a parenthesized expression suitable for use in a
        ``FILTER`` clause. Every literal goes through
        :meth:`AqlQueryBuilder.bind` so AQL never sees inlined values.
        Mirrors the legacy ``filter-translator.js``'s
        ``translateFilterExpression`` / ``translateFilterTerm`` /
        ``translateFilterFunction`` switch.
        """
        # ----- Leaf terms -------------------------------------------------
        if isinstance(expr, Variable):
            mapped = self.state.var_to_expr.get(str(expr))
            if mapped is None:
                # SPARQL §17.2.1: a variable referenced in an expression
                # but not bound by the surrounding pattern evaluates to
                # the spec's "error" value, and the SURROUNDING OPERATOR
                # decides what to do with the error:
                #
                #   * ``FILTER(expr)`` — row is excluded (§18.5).
                #   * ``BIND(expr AS ?v)`` — row is kept, ``?v`` is left
                #     unbound on this row (§18.6).
                #   * ``NOT_NULL(a, b, …)`` — skip to the next argument
                #     (§17.4.1.3).
                #   * Most other builtins (DATATYPE, arithmetic, …) —
                #     propagate the error so the enclosing assignment
                #     leaves its target unbound.
                #
                # AQL's ``null`` follows the same error-propagation
                # semantics by construction: ``null == X`` → ``null``,
                # ``null + 1`` → ``null``, ``NOT_NULL(null, x)`` → ``x``,
                # and ``FILTER null`` excludes the row. So emitting the
                # literal ``null`` here gives every spec-correct
                # downstream behaviour for free — no per-operator
                # special-case code is needed.
                #
                # A SPARQL typo (``?nove`` vs ``?nova``) presents
                # identically to a deliberate use of an OPTIONAL-bound
                # variable, so we surface a ``W_UNBOUND_VARIABLE_IN_EXPR``
                # warning on the builder. The translation harness and
                # ops UI both render warnings, giving operators the
                # disambiguation a silent ``null`` would deny them.
                # The translation itself does NOT raise — that would
                # block W3C-conformant queries like
                # ``NOT_NULL(?z, -3)`` from translating.
                self.builder.warn(
                    code="W_UNBOUND_VARIABLE_IN_EXPR",
                    message=(
                        f"variable ?{expr} is referenced in an expression "
                        f"but never bound by the surrounding pattern; per "
                        f"SPARQL §17.2.1 this evaluates to an 'error' value "
                        f"(emitted as AQL ``null``, which the surrounding "
                        f"FILTER / BIND / COALESCE / arithmetic handles "
                        f"per its own null-propagation rules)"
                    ),
                    variable=str(expr),
                )
                return "null"
            return mapped
        if isinstance(expr, URIRef):
            return self.builder.bind(str(expr), hint="uri")
        if isinstance(expr, Literal):
            return self.builder.bind(_term_to_python(expr))

        name = getattr(expr, "name", None)
        if name is None:
            raise UnsupportedSparqlError(f"FILTER expression has no .name attribute: {type(expr).__name__}")

        # ----- Boolean composition ---------------------------------------
        if name == "ConditionalAndExpression":
            parts = [self._translate_expr(expr.expr)]
            for other in expr.other:
                parts.append(self._translate_expr(other))
            return "(" + " && ".join(parts) + ")"
        if name == "ConditionalOrExpression":
            parts = [self._translate_expr(expr.expr)]
            for other in expr.other:
                parts.append(self._translate_expr(other))
            return "(" + " || ".join(parts) + ")"

        # ----- Unary -----------------------------------------------------
        if name == "UnaryNot":
            return f"!({self._translate_expr(expr.expr)})"
        if name == "UnaryMinus":
            inner = self._translate_expr(expr.expr)
            alias = self.builder.fresh_alias(prefix="num")
            # ArangoDB 3.12 rejects ``-@value`` with ERR 1552. The
            # one-row subquery also enforces SPARQL's numeric operand
            # contract instead of allowing AQL to coerce strings.
            return f"(FOR {alias} IN [{inner}] RETURN IS_NUMBER({alias}) ? (0 - {alias}) : null)[0]"
        if name == "UnaryPlus":
            inner = self._translate_expr(expr.expr)
            alias = self.builder.fresh_alias(prefix="num")
            return f"(FOR {alias} IN [{inner}] RETURN IS_NUMBER({alias}) ? {alias} : null)[0]"

        # ----- Relational ------------------------------------------------
        if name == "RelationalExpression":
            op = expr.op
            if op in self._RELATIONAL_OP_MAP:
                left = self._translate_expr(expr.expr)
                right = self._translate_expr(expr.other)
                return f"({left} {self._RELATIONAL_OP_MAP[op]} {right})"
            if op in ("IN", "NOT IN"):
                left = self._translate_expr(expr.expr)
                # ``other`` for IN/NOT IN is normally a Python list of
                # expressions to test against. BUT rdflib represents an
                # EMPTY list (``?x IN ()`` / ``?x NOT IN ()``) as the
                # ``rdf:nil`` URIRef rather than ``[]`` — and since
                # ``URIRef`` is a ``str`` subclass, naively iterating it
                # would walk the IRI's characters and crash downstream
                # with "FILTER expression has no .name attribute: str"
                # (W3C ``functions/notin01``). Normalise nil → empty
                # list so the membership test degenerates correctly:
                # ``x IN []`` is always false, ``x NOT IN []`` always
                # true — exactly SPARQL 1.1 §17.4.1.9's contract for the
                # empty-set case.
                raw_items = expr.other
                if isinstance(raw_items, URIRef) and raw_items == RDF.nil:
                    items: list[str] = []
                else:
                    items = [self._translate_expr(item) for item in raw_items]
                aql_op = "IN" if op == "IN" else "NOT IN"
                return f"({left} {aql_op} [{', '.join(items)}])"
            raise UnsupportedSparqlError(f"unsupported relational operator in FILTER: {op!r}")

        # ----- Arithmetic ------------------------------------------------
        if name == "AdditiveExpression":
            return self._chain_binary(expr.expr, expr.op, expr.other)
        if name == "MultiplicativeExpression":
            return self._chain_binary(expr.expr, expr.op, expr.other)

        # ----- SPARQL builtins -------------------------------------------
        # The full Builtin_* dispatch table lives in
        # :mod:`arango_sparql.translate.filter_builtins` so this module
        # stays under the 1500-line modularity cap. Non-builtin
        # expression nodes (relational / arithmetic / unary / boolean
        # composition above) recurse back here.
        if name.startswith("Builtin_"):
            return translate_builtin(self, expr)

        # ----- Function call (XSD constructor casts, etc.) ---------------
        # rdflib emits a ``Function`` node for IRI-named function calls
        # such as the XSD constructor casts ``xsd:double(?x)`` /
        # ``xsd:integer(?x)`` (SPARQL 1.1 §17.5). Delegated to
        # :mod:`filter_builtins` so this over-cap module doesn't grow.
        if name == "Function":
            return translate_function(self, expr)

        raise UnsupportedSparqlError(
            f"FILTER expression node {name!r} is not yet supported (see "
            f"references/arango-sparql/src/lib/filter-translator.js for the "
            f"legacy implementation)"
        )

    def _chain_binary(self, head: Any, ops: list[str], tail: list[Any]) -> str:
        """Render an AdditiveExpression or MultiplicativeExpression.

        rdflib stores these with ``.expr`` (head), ``.op`` (a list of
        operator strings parallel to ``.other``), and ``.other`` (a list
        of subsequent operands). E.g. ``?a + 1 - 2`` becomes
        ``head=?a, ops=['+', '-'], tail=[1, 2]``.
        """
        result = self._translate_expr(head)
        for op, operand in zip(ops, tail, strict=True):
            right = self._translate_expr(operand)
            left_alias = self.builder.fresh_alias(prefix="num")
            right_alias = self.builder.fresh_alias(prefix="num")
            result = (
                f"(FOR {left_alias} IN [{result}] "
                f"FOR {right_alias} IN [{right}] "
                f"RETURN IS_NUMBER({left_alias}) && IS_NUMBER({right_alias}) "
                f"? ({left_alias} {op} {right_alias}) : null)[0]"
            )
        return result

    # ------------------------------------------------------------------
    # Internal: projection / RETURN
    # ------------------------------------------------------------------
    def _emit_projection(self) -> None:
        if not self.state.projection_vars:
            # ``SELECT *`` lands here with an empty PV — fall back to
            # every *user-visible* variable we bound during BGP traversal
            # so the query still produces a useful result. Order is
            # insertion order of var_to_expr to keep the output stable.
            #
            # Internal sigils must stay out of the star-projection:
            # ``_path_<n>`` (property-path SequencePath join points) and
            # ``_bn_<bgp>_<label>`` (BGP-scoped blank-node existentials)
            # are machinery the user cannot reference. Emitting them
            # would make ``SELECT *`` diverge from SPARQL 1.1 §18.2.4
            # (project only in-scope variables) — see ``paths.py`` and
            # the matching strip in ``union_paths.py``.
            keys = [
                name
                for name in self.state.var_to_expr
                if not name.startswith("_path_") and not name.startswith("_bn_")
            ]
        else:
            keys = [str(v) for v in self.state.projection_vars]
        # Caller-requested extras (canonical-key vars from the
        # federation entry point) append after the query's own list so
        # the partition's declared column order is preserved.
        for extra in self.extra_projection or ():
            name = extra.lstrip("?")
            if name not in keys:
                keys.append(name)
        mapping: list[tuple[str, str]] = []
        for key in keys:
            expr = self.state.var_to_expr.get(key)
            if expr is None:
                raise AqlEmitError(
                    f"projection variable ?{key} was never bound by the BGP; "
                    f"the SPARQL query selects a variable that doesn't appear "
                    f"in WHERE."
                )
            mapping.append((key, expr))
        self.builder.return_object(mapping, distinct=self.state.distinct)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _expression_variable_names(node: Any) -> set[str]:
    """Names of every SPARQL Variable inside an rdflib expression tree.

    rdflib expressions are ``CompValue`` mappings whose operands sit under
    varying keys (``expr``, ``other``, ``arg``, …) and in lists, so this
    walks every value rather than a fixed set of attributes.
    """
    out: set[str] = set()
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, Variable):
            out.add(str(item))
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list | tuple):
            stack.extend(item)
    return out


def _triple_priority(triple: tuple[Any, Any, Any]) -> tuple[int, int]:
    """Sort key for BGP triples — lower = visit earlier.

    Order:
      0. Type patterns (``?s a :Class``) — bind subjects to physical
         collections first.
      1. Triples whose subject is a Variable — these can reuse a prior
         alias when sorted after a type pattern bound the same subject.
         Property-path predicates (``Path`` instances) also land here:
         a ``?s :p/:q ?o`` against an already-bound ``?s`` is the
         common shape (e.g. type pattern + path-on-the-subject), and
         we want the path desugaring to see the same binding state a
         plain ``?s :name ?n`` would.
      2. Triples whose subject is a URI — they always open a fresh FOR.
    """
    s, p, o = triple
    if isinstance(p, URIRef) and p == RDF.type and isinstance(o, URIRef):
        primary = 0
    elif isinstance(s, Variable):
        primary = 1
    else:
        primary = 2
    # Stable secondary so two triples with the same primary preserve
    # rdflib's order — keeps golden output deterministic.
    return (primary, 0)


def _is_empty_bgp(node: Any) -> bool:
    """``True`` iff *node* is a ``BGP`` algebra node with zero
    triples.

    Used by :meth:`AlgebraVisitor.visit_Join` to detect the
    common ``Join(BGP[], BGP[triples])`` shape rdflib produces
    for ``Builtin_EXISTS`` / probe inner patterns, so the
    non-empty arm walks first and the empty arm becomes a true
    no-op (``visit_BGP``'s degenerate-FOR opener only fires when
    no other FOR has been emitted yet).
    """
    return getattr(node, "name", None) == "BGP" and not getattr(node, "triples", None)


def _collect_bgp_triples(node: Any) -> list[tuple[Any, Any, Any]]:
    """Walk *node*'s subtree collecting every triple from every BGP
    descendant, in walk order.

    Used by :meth:`AlgebraVisitor.visit_ConstructQuery` to synthesise
    the implicit template that ``CONSTRUCT WHERE { … }`` (SPARQL 1.1
    §16.2.1 short-form) leaves blank. The spec mandates "the
    template is the BGP", but a real WHERE may wrap the BGP in
    ``Join``, ``Filter``, etc.; we walk past those wrappers and
    pick up every BGP we find so the template stays faithful even
    when the user adds a FILTER constraint that isn't itself a
    template triple.

    Triples from ``OPTIONAL`` (`LeftJoin`) are intentionally
    included — the spec treats the BGP under OPTIONAL as part of
    the matching pattern, and any binding that survives the
    OPTIONAL also surfaces in the output template. Unbound
    variables (no row matched the OPTIONAL) would raise in
    :meth:`_construct_term_to_aql`, which the spec accepts as a
    legitimate failure mode for malformed templates.
    """
    out: list[tuple[Any, Any, Any]] = []
    _walk_for_bgp_triples(node, out)
    return out


def _walk_for_bgp_triples(node: Any, out: list[tuple[Any, Any, Any]]) -> None:
    """Recursive helper for :func:`_collect_bgp_triples`."""
    if node is None:
        return
    if getattr(node, "name", None) == "BGP":
        for triple in getattr(node, "triples", []) or []:
            out.append(triple)
        return
    for attr in ("p", "p1", "p2"):
        child = getattr(node, attr, None)
        if child is not None and hasattr(child, "name"):
            _walk_for_bgp_triples(child, out)


def _term_to_python(term: Any) -> Any:
    """Convert an rdflib Literal/URIRef into a JSON-safe Python value.

    Literals use ``Literal.toPython()`` so xsd:integer/xsd:decimal/
    xsd:dateTime round-trip through ArangoDB as the right primitive
    type rather than as their lexical form.
    """
    if isinstance(term, Literal):
        value = term.toPython()
        # ``xsd:decimal`` round-trips through rdflib as
        # ``decimal.Decimal``, which neither ``json`` nor python-arango
        # can serialize as a bind value (live-execution failure:
        # "Object of type Decimal is not JSON serializable"). AQL has
        # no decimal type anyway — degrade to float, the same precision
        # ArangoDB stores.
        if isinstance(value, decimal.Decimal):
            return float(value)
        if isinstance(value, datetime.date | datetime.time):
            # python-arango JSON-encodes bind values. rdflib converts XSD
            # date/time literals to Python temporal objects, which are not
            # JSON serializable; the physical document model stores their
            # lexical RDF form and the expression layer parses it as needed.
            return str(term)
        return value
    if isinstance(term, URIRef):
        return str(term)
    return term
