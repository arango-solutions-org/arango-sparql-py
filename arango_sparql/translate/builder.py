"""Parameterized AQL query builder.

This is the **only** place AQL strings are assembled. Visitors call into
the builder; the builder owns alias minting, bind-variable naming, and
clause ordering. Direct string concatenation of AQL is forbidden — see
``.cursor/rules/100-backend-python.mdc``.

The clause set here is intentionally narrow — it covers what the BGP
+ SELECT path needs (FOR / FILTER / SORT / LIMIT / RETURN). Additional
clauses (LET, COLLECT, nested sub-queries) are added as the matching
visitor methods are ported via the SPARQL→AQL skill recipe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..errors import AqlEmitError, UnsupportedSparqlError

_AQL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# A collection name only ever reaches AQL as a ``@@`` bind parameter, so it
# needs to be a valid ArangoDB name, not an AQL identifier. ArangoDB's
# traditional names allow letters, digits, ``_`` and ``-`` (max 256 bytes);
# ``IAM-TERRAFORM-DOCS-DEMO_Relations`` on prod.demo is one. Extended names
# (3.11+) may hold more, so anything printable without ``/`` is accepted and
# the server stays the authority on what exists.
_MAX_COLLECTION_NAME_BYTES = 256
_BIND_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_]")


def _valid_collection_name(name: str) -> bool:
    if not name or "/" in name or len(name.encode("utf-8")) > _MAX_COLLECTION_NAME_BYTES:
        return False
    return all(ch.isprintable() for ch in name)


def is_aql_identifier(name: str) -> bool:
    """Whether *name* can be written bare after a dot (``doc.name``)."""
    return bool(_AQL_IDENT_RE.match(name or ""))


def attribute_ref(alias: str, name: str) -> str:
    """AQL attribute access ``alias.<name>`` that is valid for ANY attribute name.

    A plain identifier is emitted bare (``doc.name`` — every existing golden).
    Anything else (``phys:typeValue``, ``first name``, ``é``) is backtick-
    quoted: ``doc.`phys:typeValue```. Emitting it bare produced invalid AQL
    (``doc.phys:typeValue`` → ERR 1501), and an unquoted caller-influenced
    name is an injection shape.

    Inside AQL backticks ``\\`` starts an escape and a backtick ends the
    quoting, so a name containing either cannot be passed safely and is
    refused — never emitted as plausible-but-wrong AQL. Mirrors
    arango-cypher-py's ``_reject_unsafe_escaped_names`` rule.
    """
    if is_aql_identifier(name):
        return f"{alias}.{name}"
    if not name or "`" in name or "\\" in name:
        shown = name if len(name) <= 80 else name[:80] + "…"
        raise UnsupportedSparqlError(
            f"attribute name {shown!r} contains a backtick or backslash (or is empty) "
            "and cannot be passed safely to AQL; rename the attribute or map the property "
            "to a different one in the ontology"
        )
    return f"{alias}.`{name}`"


# Sentinel for distinguishing "bind name absent" from "bind value is None"
# in :meth:`AqlQueryBuilder.absorb_child`'s collision check. A bare
# ``in`` test would conflate the two; a sentinel object makes the
# branch unambiguous and the error message precise.
_MISSING = object()


class _ClauseKind(Enum):
    FOR = 1
    FILTER = 2
    LET = 3
    SORT = 4
    LIMIT = 5
    RETURN = 6
    COLLECT = 7
    RAW = 99


@dataclass
class _Clause:
    kind: _ClauseKind
    text: str


@dataclass
class AqlQueryBuilder:
    """Fluent, parameter-safe AQL builder.

    Mirrors the surface of the legacy
    ``references/arango-sparql/src/lib/aql-query-builder.js`` so the
    porting recipe stays mechanical, while emitting bind-safe AQL using
    ArangoDB's standard ``@var`` (value) and ``@@coll`` (collection)
    placeholder conventions.
    """

    seed_params: dict[str, Any] = field(default_factory=dict)

    _alias_counter: int = 0
    _bind_counter: int = 0
    _coll_counter: int = 0
    _coll_bind_by_name: dict[str, str] = field(default_factory=dict)
    _bind_vars: dict[str, Any] = field(default_factory=dict)
    _body_clauses: list[_Clause] = field(default_factory=list)
    """FOR / FILTER / LET / RAW clauses, in the order the visitor adds them."""
    _with_collections: list[str] = field(default_factory=list)
    """Sharded-family member collection names whose ``@@coll`` bind
    references must appear in the leading ``WITH`` clause that
    :meth:`finalize` prepends (PRD §6.5.3). Order-preserving and
    de-duplicated by string equality — two triples that both
    reference the ``[us, eu, apac]`` family produce one ``WITH`` line."""
    _sort_keys: list[str] = field(default_factory=list)
    """Pending ``SORT`` keys, joined into a single comma-separated clause
    by :meth:`finalize`. Each entry is a ``"<expr> <ASC|DESC>"`` string
    appended in visit order."""
    _limit_clause: _Clause | None = None
    _return_clause: _Clause | None = None
    _ask_mode: bool = False
    """When set, :meth:`finalize` wraps the assembled body in
    ``RETURN LENGTH(<body>) > 0`` so an ASK query produces a single
    boolean row rather than a SELECT-style projection."""
    _empty_result_fallback: bool = False
    """Emit one empty binding when a grouped aggregate has no input rows."""
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        for k, v in self.seed_params.items():
            if k.startswith("_") or k.startswith("@"):
                raise ValueError(f"reserved bind-var name: {k!r}")
            self._bind_vars[k] = v

    # ------------------------------------------------------------------
    # Aliases & bind variables
    # ------------------------------------------------------------------
    def fresh_alias(self, prefix: str = "doc") -> str:
        """Return a unique FOR-loop variable name."""
        if not _AQL_IDENT_RE.match(prefix):
            raise ValueError(f"invalid alias prefix: {prefix!r}")
        self._alias_counter += 1
        return f"{prefix}{self._alias_counter}"

    def bind(self, value: Any, *, hint: str | None = None) -> str:
        """Register *value* as a bind variable and return its placeholder.

        Always use this for any literal or caller-supplied value — never
        inline a Python value into an AQL string.
        """
        self._bind_counter += 1
        suffix = ""
        if hint:
            if not _AQL_IDENT_RE.match(hint):
                raise ValueError(f"invalid bind hint: {hint!r}")
            suffix = f"_{hint}"
        name = f"_p{self._bind_counter}{suffix}"
        self._bind_vars[name] = value
        return f"@{name}"

    def bind_collection(self, collection_name: str) -> str:
        """Register a collection name as a ``@@coll`` bind variable.

        Reuses the same bind name for repeated occurrences of the same
        collection in a single query so ArangoDB plans them as the same
        relation instead of two separate references.
        """
        if not _valid_collection_name(collection_name):
            # A translation error (not ValueError): routes and the NL repair
            # loop map SparqlError; a bare ValueError escaped as a 500 / an
            # "LLM transport failure".
            raise AqlEmitError(f"invalid ArangoDB collection name: {collection_name!r}")
        cached = self._coll_bind_by_name.get(collection_name)
        if cached is not None:
            return f"@@{cached}"
        self._coll_counter += 1
        # The bind-parameter NAME must be an identifier even when the
        # collection name is not (``IAM-TERRAFORM…`` → ``c1_IAM_TERRAFORM…``);
        # the counter keeps two names that sanitize alike distinct.
        name = f"c{self._coll_counter}_{_BIND_NAME_UNSAFE.sub('_', collection_name)}"
        # AQL collection bind-var names must not start with a digit; the
        # ``c`` prefix above already guarantees that, but we double-check
        # via the ident regex on the full name to catch typos.
        if not _AQL_IDENT_RE.match(name):  # pragma: no cover - defensive
            raise AqlEmitError(f"generated invalid collection bind name {name!r}")
        self._coll_bind_by_name[collection_name] = name
        self._bind_vars[f"@{name}"] = collection_name
        return f"@@{name}"

    # ------------------------------------------------------------------
    # Clause emission
    # ------------------------------------------------------------------
    def for_(self, alias: str, collection: str) -> AqlQueryBuilder:
        """Emit ``FOR <alias> IN <collection>``.

        The collection is registered as a ``@@coll`` bind variable so
        the collection name never gets concatenated into the query
        string directly.
        """
        if not _AQL_IDENT_RE.match(alias):
            raise ValueError(f"invalid FOR alias: {alias!r}")
        coll_ref = self.bind_collection(collection)
        self._body_clauses.append(_Clause(_ClauseKind.FOR, f"FOR {alias} IN {coll_ref}"))
        return self

    def for_traversal(
        self,
        vertex_alias: str,
        edge_alias: str,
        start_alias: str,
        edge_collection: str,
        *,
        direction: str = "OUTBOUND",
    ) -> AqlQueryBuilder:
        """Emit ``FOR <vertex>, <edge> IN <DIRECTION> <start> @@<edgeColl>``.

        The 1..1 single-step traversal pattern AQL prefers for
        SPARQL-style "follow one edge per triple" walks. ``start_alias``
        is the AQL identifier of the FOR-loop alias whose document is
        the traversal source — typically the subject of the SPARQL
        triple. ``vertex_alias`` and ``edge_alias`` are minted by the
        caller via :meth:`fresh_alias` so they're guaranteed unique.

        ``direction`` is one of ``OUTBOUND`` / ``INBOUND`` / ``ANY``;
        the SPARQL→AQL visitor uses ``OUTBOUND`` for plain
        ``?s :rel ?o`` triples and would route ``INBOUND`` for inverse
        property paths once those land. We validate the literal here so
        a typo can never silently degrade traversal semantics — an
        invalid direction produces an empty result set in AQL with no
        error.
        """
        if not _AQL_IDENT_RE.match(vertex_alias):
            raise ValueError(f"invalid traversal vertex alias: {vertex_alias!r}")
        if not _AQL_IDENT_RE.match(edge_alias):
            raise ValueError(f"invalid traversal edge alias: {edge_alias!r}")
        if not _AQL_IDENT_RE.match(start_alias):
            raise ValueError(f"invalid traversal start alias: {start_alias!r}")
        if direction not in ("OUTBOUND", "INBOUND", "ANY"):
            raise ValueError(f"invalid traversal direction: {direction!r}")
        coll_ref = self.bind_collection(edge_collection)
        self._body_clauses.append(
            _Clause(
                _ClauseKind.FOR,
                f"FOR {vertex_alias}, {edge_alias} IN {direction} {start_alias} {coll_ref}",
            )
        )
        return self

    def for_sharded(
        self,
        alias: str,
        shard_collections: tuple[str, ...] | list[str],
    ) -> AqlQueryBuilder:
        """Emit a sharded FOR — a UNION_DISTINCT over per-shard scans.

        Renders as::

            FOR <alias> IN UNION_DISTINCT(
              (FOR <inner1> IN @@<shard1> RETURN <inner1>),
              (FOR <inner2> IN @@<shard2> RETURN <inner2>),
              ...
            )

        Downstream ``FILTER`` / ``RETURN`` clauses reference ``alias``
        (the union row) the same way they would a plain FOR — every
        per-shard ``inner_k`` is a private alias confined to its
        sub-scan. This is the PRD §6.5.3 cross-shard broadcast: the
        cluster optimiser sees one row stream over the union of all
        family members.

        The ``@@coll`` bind names are minted via
        :meth:`bind_collection` so two triples that target the same
        family share the same bind variables (one ``@@c<n>_<name>``
        per shard, regardless of how many triples touch it). Each
        member collection is also recorded for the leading ``WITH``
        clause :meth:`finalize` prepends — ArangoDB cluster mode
        requires the optimiser to know about every collection the
        query reads at parse time, not at execution.

        ``shard_collections`` must be non-empty and contain unique
        members; the resolver guarantees both of these invariants in
        :meth:`SchemaResolver.__post_init__`, but we re-assert here
        so a hand-rolled call site can't slip through.
        """

        if not _AQL_IDENT_RE.match(alias):
            raise ValueError(f"invalid FOR alias: {alias!r}")
        if not shard_collections:
            raise AqlEmitError("for_sharded requires at least one member collection")
        members = list(shard_collections)
        if len(set(members)) != len(members):
            raise AqlEmitError(f"for_sharded members must be unique, got {members!r}")
        sub_scans: list[str] = []
        for member in members:
            inner = self.fresh_alias()
            coll_ref = self.bind_collection(member)
            sub_scans.append(f"(FOR {inner} IN {coll_ref} RETURN {inner})")
            if member not in self._with_collections:
                self._with_collections.append(member)
        # Indent the inner scans for readability — the AQL planner is
        # whitespace-agnostic, but a multi-shard family is one of the
        # rare clauses that genuinely benefits from line breaks in
        # the rendered query (the operator reading EXPLAIN output
        # should be able to spot each shard at a glance).
        body = ",\n  ".join(sub_scans)
        clause = f"FOR {alias} IN UNION_DISTINCT(\n  {body}\n)"
        self._body_clauses.append(_Clause(_ClauseKind.FOR, clause))
        return self

    def filter_eq(self, lhs: str, rhs_bind_placeholder: str) -> AqlQueryBuilder:
        """Emit ``FILTER <lhs> == <bind>``.

        ``lhs`` is a builder-emitted attribute path (e.g. ``doc1.name``);
        ``rhs_bind_placeholder`` must be the result of a prior
        :meth:`bind` call (i.e. ``@_pN``).
        """
        if not rhs_bind_placeholder.startswith("@"):
            raise AqlEmitError(f"filter_eq RHS must be a bind placeholder, got {rhs_bind_placeholder!r}")
        self._body_clauses.append(_Clause(_ClauseKind.FILTER, f"FILTER {lhs} == {rhs_bind_placeholder}"))
        return self

    def collect(
        self,
        *,
        keys: list[tuple[str, str]] | None = None,
        aggregates: list[tuple[str, str]] | None = None,
        count_into: str | None = None,
    ) -> AqlQueryBuilder:
        """Emit a ``COLLECT`` clause.

        Three idiomatic AQL forms:

        * ``COLLECT k1 = e1, k2 = e2``                  (group only)
        * ``COLLECT [k1 = e1] WITH COUNT INTO c``        (count shorthand)
        * ``COLLECT [k1 = e1] AGGREGATE a = f(e), …``    (full form)

        ``keys`` is the GROUP BY key list — pairs of ``(alias,
        expression)`` — emitted in declaration order. ``aggregates``
        likewise pairs ``(alias, "<AGG>(<expr>)")``. Pass ``count_into``
        for the ``WITH COUNT INTO`` shorthand (only valid when
        ``aggregates`` is empty); pass ``aggregates`` for the full form
        (which can also include ``COUNT``).

        Validation:
        * a query may have at most one ``COLLECT`` (AQL also forbids
          multiples without a sub-query wrapper).
        * post-COLLECT, every prior FOR alias is out of scope — callers
          that emit FILTER / LET / SORT / RETURN after a COLLECT must
          reference only the COLLECT's own output aliases.

        Mirrors the legacy ``aql-query-builder.js#collect`` shape that
        the SPARQL ``AggregateJoin`` visitor needs.
        """
        if any(c.kind == _ClauseKind.COLLECT for c in self._body_clauses):
            raise AqlEmitError("COLLECT already emitted; only one per query")
        if count_into is not None and aggregates:
            raise AqlEmitError("COLLECT cannot combine WITH COUNT INTO and AGGREGATE in the same clause")
        keys = keys or []
        aggregates = aggregates or []
        for alias, _ in keys:
            if not _AQL_IDENT_RE.match(alias):
                raise ValueError(f"invalid COLLECT key alias: {alias!r}")
        for alias, _ in aggregates:
            if not _AQL_IDENT_RE.match(alias):
                raise ValueError(f"invalid COLLECT aggregate alias: {alias!r}")
        if count_into is not None and not _AQL_IDENT_RE.match(count_into):
            raise ValueError(f"invalid COLLECT count_into alias: {count_into!r}")
        parts: list[str] = ["COLLECT"]
        if keys:
            parts.append(", ".join(f"{a} = {e}" for a, e in keys))
        if count_into is not None:
            parts.append(f"WITH COUNT INTO {count_into}")
        elif aggregates:
            parts.append("AGGREGATE " + ", ".join(f"{a} = {expr}" for a, expr in aggregates))
        self._body_clauses.append(_Clause(_ClauseKind.COLLECT, " ".join(parts)))
        return self

    def let_outbound_first_uri(
        self,
        let_alias: str,
        *,
        start_alias: str,
        edge_collection: str,
        type_field: str | None = None,
        type_value: Any = None,
    ) -> AqlQueryBuilder:
        """Emit a ``LET <alias> = (FOR v, e IN OUTBOUND <start> @@<coll>
        [FILTER e.<type_field> == @bind] LIMIT 1 RETURN v._uri)[0]`` clause.

        The OPTIONAL emitter for object-property triples — picks the
        target vertex's ``_uri`` of the first matching edge, or ``null``
        when no edge matches (which is exactly SPARQL's OPTIONAL
        "unbound" semantics).

        ``LIMIT 1`` is correct for SPARQL OPTIONAL: the SPARQL semantics
        of ``OPTIONAL { ?s :p ?o }`` only need to know whether ``?o``
        binds, and SPARQL does not specify which match to pick when
        multiple exist (set semantics). Picking the first is what the
        legacy Foxx ``aql-translator.js#processOptionalPatterns`` does
        too, and matches the W3C "any-of" projection.

        Vertex / edge aliases are minted internally so the caller does
        not have to care about uniqueness — the let alias is the only
        identifier the caller needs.
        """
        if not _AQL_IDENT_RE.match(let_alias):
            raise ValueError(f"invalid LET alias: {let_alias!r}")
        if not _AQL_IDENT_RE.match(start_alias):
            raise ValueError(f"invalid traversal start alias: {start_alias!r}")
        if (type_field is None) != (type_value is None):
            raise ValueError(
                "type_field and type_value must be provided together "
                "(GENERIC_WITH_TYPE) or both omitted (DEDICATED_COLLECTION)"
            )
        v_alias = self.fresh_alias(prefix="v")
        e_alias = self.fresh_alias(prefix="e")
        coll_ref = self.bind_collection(edge_collection)
        parts = [f"FOR {v_alias}, {e_alias} IN OUTBOUND {start_alias} {coll_ref}"]
        if type_field is not None:
            if not _AQL_IDENT_RE.match(type_field):
                raise ValueError(f"invalid traversal type_field: {type_field!r}")
            bind = self.bind(type_value, hint=type_field)
            parts.append(f"FILTER {e_alias}.{type_field} == {bind}")
        parts.append("LIMIT 1")
        parts.append(f"RETURN {v_alias}._uri")
        subquery = " ".join(parts)
        self._body_clauses.append(_Clause(_ClauseKind.LET, f"LET {let_alias} = ({subquery})[0]"))
        return self

    def let(self, alias: str, expression: str) -> AqlQueryBuilder:
        """Emit ``LET <alias> = <expression>``.

        Used by ``visit_Extend`` (BIND) to introduce a SPARQL-bound
        variable as a first-class AQL identifier so subsequent FILTER /
        SORT / RETURN clauses can refer to it by name. Mirrors the
        legacy ``aql-query-builder.js#let``.

        ``expression`` is intentionally raw — callers must pre-bind any
        literals it references via :meth:`bind` before calling.
        """
        if not _AQL_IDENT_RE.match(alias):
            raise ValueError(f"invalid LET alias: {alias!r}")
        self._body_clauses.append(_Clause(_ClauseKind.LET, f"LET {alias} = {expression}"))
        return self

    def filter_raw(self, expression: str) -> AqlQueryBuilder:
        """Emit a raw ``FILTER <expression>``.

        Reserved for visitor methods that compose multi-operand filter
        expressions (FILTER NOT EXISTS, regex, complex boolean trees).
        Callers are responsible for binding every literal that appears
        in *expression* via :meth:`bind` first.
        """
        self._body_clauses.append(_Clause(_ClauseKind.FILTER, f"FILTER {expression}"))
        return self

    def create_child(self) -> AqlQueryBuilder:
        """Spawn a child builder seeded with this builder's counter state.

        Used for sub-SELECT emission (PRD §6.6 ToMultiSet row): the
        child accumulates the inner query's clauses + binds + return
        independently, then :meth:`absorb_child` merges the binds back
        and advances this builder's counters past the child's.

        Why counter seeding matters: bind-variable names
        (``_p<N>``), document aliases (``doc<N>``), and collection
        binds (``c<N>_<coll>``) all use a per-builder counter. If
        the child started at 1, its first bind would be ``_p1`` —
        but the parent's ``_p1`` is already taken. Seeding the
        child's counters with the parent's CURRENT values
        guarantees disjoint names, so the merge is safe and the
        AQL bind-vars dict never has a collision.

        Per-builder state that's *not* shared:

        * ``_coll_bind_by_name`` — the parent's cache of "this
          collection is already bound under this name". Sharing
          would let the child reuse parent's name and elide its
          own ``@@c<N>_<coll>``, which is a minor efficiency
          improvement but introduces a subtle invariant
          (parent's coll cache leaks into child's bind dict
          when the child wins a race). Easier to leave each
          builder with its own cache; ArangoDB collapses two
          binds to the same collection name into a single
          collection lock at execution time, so the only cost
          is one extra entry in the bind-vars dict.
        * ``_body_clauses`` / ``_sort_keys`` / ``_limit_clause`` /
          ``_return_clause`` — the child's clauses live in the
          child's own ``finalize()`` output, not in the parent's
          assembled body. That's the whole point of spawning a
          child.
        """
        child = AqlQueryBuilder()
        child._bind_counter = self._bind_counter
        child._alias_counter = self._alias_counter
        child._coll_counter = self._coll_counter
        return child

    def absorb_child(self, child: AqlQueryBuilder) -> str:
        """Merge *child*'s bind-vars into this builder and return the
        child's finalized AQL string.

        Counters are advanced to the child's final state so any
        further parent-side mints don't repeat names the child used.
        Bind-name collisions are treated as a hard error — they can
        only happen if the caller didn't use :meth:`create_child`
        to seed the child, or if some downstream code reset a
        counter behind the builder's back. The defensive check is
        cheap (a dict ``in`` per child bind) and the bug it catches
        is silent corruption of the bind-vars dict (last-write-wins
        with no warning), which is worth catching loudly.
        """
        inner_aql, child_binds = child.finalize()
        for name, value in child_binds.items():
            existing = self._bind_vars.get(name, _MISSING)
            if existing is _MISSING:
                self._bind_vars[name] = value
                continue
            if existing != value:
                raise AqlEmitError(
                    f"bind-var name collision while absorbing child "
                    f"sub-query builder: {name!r} = {existing!r} "
                    f"(parent) vs {value!r} (child). Counter seeding "
                    f"broke — make sure create_child() was used to "
                    f"spawn the child builder."
                )
        self._bind_counter = child._bind_counter
        self._alias_counter = child._alias_counter
        self._coll_counter = child._coll_counter
        return inner_aql

    def for_union(
        self,
        row_alias: str,
        arm_aqls: list[str],
        *,
        distinct: bool = False,
    ) -> AqlQueryBuilder:
        """Emit ``FOR <row_alias> IN UNION[|_DISTINCT]((<arm1>), …)``.

        Used by :mod:`arango_sparql.translate.union_paths` for SPARQL
        ``UNION`` (multiset — ``distinct=False``) and for property-path
        ``AlternativePath`` / ``MulPath`` expansions (set — ``distinct=True``).
        Each arm is a complete AQL block (its own FOR / FILTER / SORT /
        LIMIT / RETURN) that the union helper produced via a child
        builder; wrapping each in ``(...)`` makes it an expression
        that evaluates to a list, which is what AQL's ``UNION`` /
        ``UNION_DISTINCT`` consume.

        Why the ``distinct`` switch:

        * SPARQL 1.1 §18.5 defines pattern ``UNION`` over multisets —
          duplicate solutions are preserved. ``UNION_DISTINCT`` would
          collapse them.
        * SPARQL 1.1 §9 / §18.4 property paths evaluate to **sets of
          nodes** (ALP); a path of length two that matches several
          walks must contribute each endpoint only once. MulPath /
          AlternativePath therefore pass ``distinct=True``.

        Indentation is two spaces per arm line for EXPLAIN-output
        readability, matching :meth:`for_subquery`.
        """
        if not _AQL_IDENT_RE.match(row_alias):
            raise ValueError(f"invalid FOR alias: {row_alias!r}")
        if len(arm_aqls) < 2:
            raise AqlEmitError(f"for_union requires at least 2 arms, got {len(arm_aqls)}")
        for i, arm in enumerate(arm_aqls):
            if not arm or not arm.strip():
                raise AqlEmitError(
                    f"for_union arm {i} is empty; every UNION arm must be a non-empty AQL block"
                )
        arm_blocks = ",\n".join(
            "(\n" + "\n".join("  " + line for line in arm.splitlines()) + "\n)" for arm in arm_aqls
        )
        op = "UNION_DISTINCT" if distinct else "UNION"
        self._body_clauses.append(
            _Clause(
                _ClauseKind.FOR,
                f"FOR {row_alias} IN {op}(\n{arm_blocks}\n)",
            )
        )
        return self

    def has_for_clause(self) -> bool:
        """``True`` iff at least one FOR clause has been emitted into
        the current body.

        Used by :meth:`AlgebraVisitor.visit_BGP` to decide whether
        an empty BGP needs to open a degenerate one-row FOR
        (SPARQL §18.5 says the empty BGP is join's identity, so
        if a sibling pattern already opened a FOR the empty BGP
        is a no-op; only when the empty BGP is the WHOLE pattern
        do we need to manufacture a scope opener).
        """
        return any(c.kind == _ClauseKind.FOR for c in self._body_clauses)

    def for_inline(
        self,
        row_alias: str,
        inline_expr: str,
    ) -> AqlQueryBuilder:
        """Emit ``FOR <row_alias> IN <inline_expr>``.

        Used by :meth:`AlgebraVisitor.visit_BGP` to honour SPARQL's
        empty-BGP semantic (a single empty solution mapping) by
        opening a degenerate one-row FOR over a constant list
        like ``[1]``. The row alias is never referenced
        downstream — it's purely a scope opener so subsequent
        Extend / Filter / Project clauses have a FOR to attach
        to, satisfying AQL's "every body clause needs an
        enclosing FOR" invariant.

        Unlike :meth:`for_values` (which binds a list-of-objects
        via ``@<name>`` parameter), this method splices the AQL
        expression directly — so the caller is responsible for
        constructing a literal AQL value that's safe to inline.
        The single intended use is empty-BGP opening with the
        literal ``[1]``; do not use this for user-controlled
        data (the bind-vars path via :meth:`for_values` or
        :meth:`bind` is the safe one).
        """
        if not _AQL_IDENT_RE.match(row_alias):
            raise ValueError(f"invalid FOR alias: {row_alias!r}")
        if not inline_expr or not inline_expr.strip():
            raise AqlEmitError("for_inline requires a non-empty AQL expression")
        self._body_clauses.append(
            _Clause(
                _ClauseKind.FOR,
                f"FOR {row_alias} IN {inline_expr}",
            )
        )
        return self

    def for_values(self, row_alias: str, values_bind: str) -> AqlQueryBuilder:
        """Emit ``FOR <row_alias> IN <values_bind>`` for SPARQL VALUES.

        ``values_bind`` is the ``@<name>`` placeholder returned by
        :meth:`bind` when the visitor binds a Python list-of-dicts
        representing the VALUES rows. Mirrors :meth:`for_` but the
        source is a bind variable holding a list, not a collection.

        Used by :meth:`AlgebraVisitor.visit_ToMultiSet` when the
        inner pattern is rdflib's ``values`` algebra node (inline
        binding data — SPARQL 1.1 §10.2). The list rows already
        carry per-variable values as Python primitives (via
        :func:`_term_to_python`), so the FOR-loop alias's dotted
        access (``row.<var>``) yields a value with the correct
        Python type without any AQL-side coercion.
        """
        if not _AQL_IDENT_RE.match(row_alias):
            raise ValueError(f"invalid FOR alias: {row_alias!r}")
        if not values_bind.startswith("@") or values_bind.startswith("@@"):
            raise ValueError(f"expected a value bind placeholder (@name), got: {values_bind!r}")
        self._body_clauses.append(_Clause(_ClauseKind.FOR, f"FOR {row_alias} IN {values_bind}"))
        return self

    def for_subquery(self, row_alias: str, inner_aql: str) -> AqlQueryBuilder:
        """Emit ``FOR <row_alias> IN (<indented inner AQL>)``.

        Used by :meth:`AlgebraVisitor.visit_ToMultiSet` for SPARQL
        sub-SELECTs. The inner AQL block is rendered exactly as the
        child builder finalized it (its own FOR / FILTER / SORT /
        LIMIT / RETURN), wrapped in parentheses so ArangoDB treats
        the result as a list expression the outer FOR can iterate.

        Indentation is two spaces per line — purely cosmetic, but
        it makes the AQL readable when emitted into EXPLAIN output
        or written to a log. The bind-vars dict is unaffected
        (binds were merged in :meth:`absorb_child`).
        """
        if not _AQL_IDENT_RE.match(row_alias):
            raise ValueError(f"invalid FOR alias: {row_alias!r}")
        if not inner_aql or not inner_aql.strip():
            raise AqlEmitError("for_subquery requires a non-empty inner AQL block")
        indented = "\n".join("  " + line for line in inner_aql.splitlines())
        self._body_clauses.append(
            _Clause(
                _ClauseKind.FOR,
                f"FOR {row_alias} IN (\n{indented}\n)",
            )
        )
        return self

    def for_attributes(
        self,
        key_alias: str,
        document_alias: str,
    ) -> AqlQueryBuilder:
        """Emit ``FOR <key_alias> IN ATTRIBUTES(<document_alias>, true)``.

        The ``true`` second argument tells ATTRIBUTES to **skip the
        ArangoDB system attributes** (``_id``, ``_key``, ``_rev``,
        ``_from``, ``_to``) so we don't need a separate FILTER to
        exclude them. The visitor's variable-predicate dispatcher
        uses this to fan an unbound predicate variable out across
        every attribute of a document — the AQL analog of "every
        triple in the dataset whose subject is this document".

        Two visitor-side caveats live with this primitive (not
        enforced here — the caller decides):

        * ``_uri`` is **not** a system attribute — it's our own
          synthetic "subject IRI" column the schema-mapper writes.
          A caller that wants triple-like semantics ``RETURN {s, p, o}``
          should filter ``key NOT IN @sys_attrs`` to exclude it
          (otherwise we'd emit a triple whose predicate is ``"_uri"``).
        * Until per-class attribute→URI mapping lands, ``key`` is the
          raw attribute name (a string). SPARQL's ``?p`` is supposed
          to bind to an IRI; the harness counts translation as a
          pass regardless, but live-execution cross-validation
          against a W3C-conformant store will diverge for any query
          that depends on ``?p`` being an IRI. PRD §6.6 row tracks
          this carve-out.
        """
        if not _AQL_IDENT_RE.match(key_alias):
            raise ValueError(f"invalid FOR alias: {key_alias!r}")
        if not _AQL_IDENT_RE.match(document_alias):
            raise ValueError(f"invalid document alias: {document_alias!r}")
        self._body_clauses.append(
            _Clause(
                _ClauseKind.FOR,
                f"FOR {key_alias} IN ATTRIBUTES({document_alias}, true)",
            )
        )
        return self

    def sort(self, expression: str, *, descending: bool = False) -> AqlQueryBuilder:
        """Append one ORDER-BY key to the next ``SORT`` clause.

        Multiple consecutive ``sort()`` calls collapse into a single
        comma-separated AQL ``SORT a DESC, b ASC`` statement (the
        idiomatic form, matching the legacy
        ``aql-query-builder.js#sort`` behavior). Two separate ``SORT``
        clauses would also be valid AQL but they read as if the second
        re-sorts the first's output, so we prefer the canonical join.
        """
        direction = "DESC" if descending else "ASC"
        self._sort_keys.append(f"{expression} {direction}")
        return self

    def limit(self, count: int, *, offset: int = 0) -> AqlQueryBuilder:
        if not isinstance(count, int) or count < 0:
            raise ValueError(f"LIMIT count must be a non-negative int, got {count!r}")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError(f"LIMIT offset must be a non-negative int, got {offset!r}")
        if self._limit_clause is not None:
            raise AqlEmitError("LIMIT already emitted; only one LIMIT per query")
        clause = f"LIMIT {offset}, {count}" if offset else f"LIMIT {count}"
        self._limit_clause = _Clause(_ClauseKind.LIMIT, clause)
        return self

    def return_object(
        self,
        mapping: list[tuple[str, str]],
        *,
        distinct: bool = False,
    ) -> AqlQueryBuilder:
        """Emit a ``RETURN { key: expr, ... }`` clause.

        ``mapping`` is a list of ``(key, expression)`` tuples in the
        order the SPARQL projection requested them. The order is
        preserved deterministically so golden-test diffs stay readable.
        """
        if self._return_clause is not None:
            raise AqlEmitError("RETURN already emitted; only one RETURN per query")
        for key, _ in mapping:
            if not _AQL_IDENT_RE.match(key):
                raise AqlEmitError(f"invalid RETURN key: {key!r}")
        body = ", ".join(f"{k}: {expr}" for k, expr in mapping)
        keyword = "RETURN DISTINCT" if distinct else "RETURN"
        self._return_clause = _Clause(_ClauseKind.RETURN, f"{keyword} {{ {body} }}")
        return self

    def return_scalar(self, expression: str) -> AqlQueryBuilder:
        """Emit ``RETURN <expression>`` — used by AskQuery (returns ``1``)
        and the future ConstructQuery / ValuesQuery emitters.
        """
        if self._return_clause is not None:
            raise AqlEmitError("RETURN already emitted; only one RETURN per query")
        self._return_clause = _Clause(_ClauseKind.RETURN, f"RETURN {expression}")
        return self

    def return_triples(
        self,
        triple_exprs: list[tuple[str, str, str]],
    ) -> AqlQueryBuilder:
        """Emit ``RETURN [ {subject: …, predicate: …, object: …}, … ]``.

        Used by :meth:`AlgebraVisitor.visit_ConstructQuery` and
        :meth:`AlgebraVisitor.visit_DescribeQuery`. The route layer
        flattens the per-row list of triple dicts into an RDF graph
        before serialising into Turtle / N-Triples / RDF/XML / JSON-LD.

        ``triple_exprs`` is a list of ``(s_expr, p_expr, o_expr)`` AQL
        expressions; one inner dict is emitted per template triple.
        Repeated subjects across template entries are perfectly fine —
        each row of the WHERE-binding result expands into the full set
        of template triples. The RDF renderer dedupes via rdflib's
        ``Graph.add`` set semantics so duplicate triples collapse the
        way RDF set semantics require.
        """

        if self._return_clause is not None:
            raise AqlEmitError("RETURN already emitted; only one RETURN per query")
        if not triple_exprs:
            raise AqlEmitError("return_triples requires at least one triple template entry")
        items = ", ".join(f"{{subject: {s}, predicate: {p}, object: {o}}}" for s, p, o in triple_exprs)
        self._return_clause = _Clause(_ClauseKind.RETURN, f"RETURN [{items}]")
        return self

    def return_triples_subquery(self, subquery: str) -> AqlQueryBuilder:
        """Emit ``RETURN ( <subquery> )`` for DESCRIBE attribute fan-out.

        ``subquery`` must be a complete AQL expression that produces a
        list of ``{subject, predicate, object}`` dicts (typically the
        ``FOR k IN ATTRIBUTES(doc) FILTER k NOT IN [...] RETURN
        {subject: doc._uri, predicate: k, object: doc[k]}`` shape that
        :meth:`AlgebraVisitor.visit_DescribeQuery` builds). The
        outer FOR-loop's binding row therefore expands into the full
        set of attribute triples for the described resource.

        Reserved for DESCRIBE; CONSTRUCT uses the simpler
        :meth:`return_triples` because its template is a static list.
        """

        if self._return_clause is not None:
            raise AqlEmitError("RETURN already emitted; only one RETURN per query")
        if not subquery or not subquery.strip():
            raise AqlEmitError("return_triples_subquery requires a non-empty subquery")
        self._return_clause = _Clause(_ClauseKind.RETURN, f"RETURN ({subquery})")
        return self

    def set_ask_mode(self) -> AqlQueryBuilder:
        """Flip the builder into ASK-output mode.

        The next :meth:`finalize` will wrap the whole assembled query in
        ``RETURN LENGTH(<inner>) > 0`` — the legacy
        ``aql-translator.js`` recipe for ``ASK`` (``ASK is essentially
        SELECT LIMIT 1``).
        """
        self._ask_mode = True
        return self

    def set_empty_result_fallback(self) -> AqlQueryBuilder:
        """Preserve SPARQL's empty solution for grouped aggregates.

        AQL ``COLLECT key = expr`` produces no row when its input is empty;
        SPARQL's grouped aggregate evaluation retains one empty solution.
        Finalization wraps the complete grouped query once and substitutes
        ``{}`` only when its result list is empty.
        """

        self._empty_result_fallback = True
        return self

    def clear_empty_result_fallback(self) -> AqlQueryBuilder:
        """Disable the grouped fallback when a later HAVING may remove rows."""

        self._empty_result_fallback = False
        return self

    def warn(self, *, code: str, message: str, **extra: Any) -> None:
        self.warnings.append({"code": code, "message": message, **extra})

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------
    def finalize(self) -> tuple[str, dict[str, Any]]:
        """Render the assembled clauses into a single AQL string.

        Decouples visitor traversal order from AQL clause order: body
        clauses (FOR/FILTER/LET/RAW) come out in insertion order, then
        SORT, LIMIT, and finally RETURN — regardless of what order the
        Algebra visitor walked them in. This is what lets
        ``visit_Slice`` (which receives LIMIT after the inner Project
        already emitted RETURN) work without contortions.
        """
        if self._return_clause is None:
            raise AqlEmitError("finalize() called without a RETURN; every query must terminate in RETURN")
        if not any(c.kind == _ClauseKind.FOR for c in self._body_clauses):
            raise AqlEmitError("query has no FOR clause; every BGP/SELECT translation needs at least one")
        with_clause: str | None = None
        if self._with_collections:
            # ArangoDB cluster mode: ``WITH`` must be the FIRST clause
            # so the planner knows which collections to lock at parse
            # time (PRD §6.5.3). Bind every member as a ``@@coll``
            # placeholder — :meth:`bind_collection` is idempotent so
            # we always end up referencing the SAME bind name that
            # the per-shard FOR sub-scans use, never a new one.
            refs = ", ".join(self.bind_collection(name) for name in self._with_collections)
            with_clause = f"WITH {refs}"
        ordered: list[_Clause] = []
        ordered.extend(self._body_clauses)
        if self._sort_keys:
            ordered.append(_Clause(_ClauseKind.SORT, "SORT " + ", ".join(self._sort_keys)))
        if self._empty_result_fallback:
            ordered.append(self._return_clause)
            inner = "\n".join(c.text for c in ordered)
            indented = "\n".join("  " + line for line in inner.splitlines())
            results_alias = self.fresh_alias(prefix="results")
            result_alias = self.fresh_alias(prefix="result")
            wrapper = [
                f"LET {results_alias} = (\n{indented}\n)",
                (f"FOR {result_alias} IN (LENGTH({results_alias}) == 0 ? [{{}}] : {results_alias})"),
            ]
            if self._limit_clause is not None:
                wrapper.append(self._limit_clause.text)
            wrapper.append(f"RETURN {result_alias}")
            body = "\n".join(wrapper)
        else:
            if self._limit_clause is not None:
                ordered.append(self._limit_clause)
            ordered.append(self._return_clause)
            body = "\n".join(c.text for c in ordered)
        if with_clause is not None:
            body = f"{with_clause}\n{body}"
        if self._ask_mode:
            # Wrap the assembled body in a LENGTH() probe so the executor
            # gets a single-row boolean result rather than a SPARQL-shaped
            # projection. Mirrors the legacy
            # ``aql-translator.js#translateQuery`` ASK branch.
            indented = "\n".join("  " + line for line in body.splitlines())
            aql = f"RETURN LENGTH(\n{indented}\n) > 0"
        else:
            aql = body
        return aql, dict(self._bind_vars)
