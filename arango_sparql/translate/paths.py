"""Property-path expansion (SPARQL 1.1 §9 / §18.4).

The Algebra visitor's main triple dispatcher
(:meth:`arango_sparql.translate.visitor.AlgebraVisitor._emit_triple`)
treats the ``predicate`` slot of an rdflib triple as either
``URIRef`` (the common ``?s :name ?n`` shape) or ``Variable`` (the
``?s ?p ?o`` cross-collection-UNION case). Property paths
(``rdflib.paths.Path`` instances — ``SequencePath`` / ``InvPath`` /
``AlternativePath`` / ``MulPath`` / ``NegatedPath``) need a separate,
recursive expansion pass before they can reach the main dispatch
table.

Unlike most of the translation work in this package, property paths
are **not** ported from the legacy ``arango-sparql`` Foxx service.
That project's ``pgt-translator.js`` / ``rpt-translator.js`` files
never branch on path types — the legacy translator silently rejects
any predicate that isn't a fixed IRI. So this module is a
green-field implementation against the W3C SPARQL 1.1 spec (§18.4
"SPARQL Algebra", "Property Paths"), cross-validated against
``pyoxigraph`` as the reference triplestore via ``tests/cross/``.
Because there is no legacy code to mirror, every non-obvious
rewriting choice is documented inline so future readers can match
the spec line-by-line.

Slices land incrementally; this module currently implements the two
that are pure desugarings (no new builder or resolver work):

* :func:`emit_path_triple` — public dispatcher; called from
  :meth:`AlgebraVisitor._emit_triple` when the predicate slot is a
  ``Path`` instance.
* :func:`_emit_sequence_path` — ``?s :p/:q/.../:pN ?o`` expands to
  N triples joined by N-1 fresh intermediate variables, each of
  which is re-dispatched through ``_emit_triple`` so PG / LPG /
  default-collection branches all compose naturally.
* :func:`_emit_inverse_path` — ``?s ^path ?o`` swaps subject and
  object and recurses on the inner path (which may itself be a
  ``Path``, e.g. ``^(:p/:q)`` is an inverse-of-sequence).

All five rdflib path operators (``SequencePath`` / ``InvPath`` /
``AlternativePath`` / ``MulPath`` / ``NegatedPath``) have a
dedicated expander below. The remaining gap is inverse arms inside
``NegatedPath`` (``!(^:p)`` — see :func:`_emit_negated_path`) and
property paths over RPT-mapped subjects; both surface stable,
greppable ``UnsupportedSparqlError`` messages so the W3C XFAIL
bucket stays clean.

Intermediate variables minted by sequence expansion use the sigil
``_path_<n>`` so they cannot collide with user-supplied variable
names (every variable that's ever shown in projection / SELECT
output came from the user, and ``_path_*`` is never emitted into a
projection). rdflib's ``Variable`` accepts leading underscores at
construction; the existing visitor never minted underscore-prefixed
variables before this commit so the sigil is private by
construction.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from rdflib import Literal, URIRef, Variable
from rdflib.paths import (
    AlternativePath,
    InvPath,
    MulPath,
    NegatedPath,
    Path,
    SequencePath,
)

from ..errors import PATH_DEPTH_EXCEEDED_CODE, UnsupportedSparqlError
from .builder import indent_aql, subject_ref

if TYPE_CHECKING:
    from .visitor import AlgebraVisitor

# SPARQL property paths are unbounded; ArangoDB has no single AQL
# construct for "follow document attribute p forever". We desugar
# ``:p*`` / ``:p+`` / ``:p?`` to a UNION of fixed-length paths
# (0..N hops) so each arm reuses the existing SequencePath / edge
# emitters. N is configurable per deployment via
# ``SchemaResolver.property_path_max_depth`` (default below). A ``*`` /
# ``+`` path whose answer needs more than N repetitions makes the query
# fail at run time (``FAIL``, code ``E_SPARQL_PATH_DEPTH``) rather than
# return the pairs found within N (issue #183; see :func:`_emit_mul_path`).
PROPERTY_PATH_MAX_DEPTH: int = 10

# The depth bound of the traversal that answers ``:p*`` / ``:p+`` over an
# edge collection (:func:`_emit_edge_closure_arm`). AQL takes only a constant
# there. The walk reaches each vertex once, along a path with no repeated
# vertex, so no two of its steps are the same edge: no answer is more steps
# away than the edge collection has edges. The query checks that count
# against this bound and refuses past it, so the bound never cuts an answer.
# 2**53 - 1 is the largest integer every JSON client keeps exact.
EDGE_CLOSURE_MAX_DEPTH: int = 2**53 - 1


def emit_path_triple(
    visitor: AlgebraVisitor,
    subject: Any,
    predicate: Path,
    obj: Any,
) -> None:
    """Dispatch a triple whose ``predicate`` slot is a ``Path``.

    Routes to the per-type expander. Unknown path types raise
    :class:`UnsupportedSparqlError` carrying the path class name so
    the operator can grep ``UnsupportedSparql:`` in the W3C XFAIL
    report and find the bucket without re-reading rdflib's source.

    Path instances can nest arbitrarily — ``^(:p/:q)`` is an
    ``InvPath`` whose ``.arg`` is a ``SequencePath``; ``:p+/:q*`` is
    a ``SequencePath`` whose ``.args`` are two ``MulPath`` — so each
    expander recurses through :meth:`AlgebraVisitor._emit_triple`,
    which re-enters this dispatcher when it encounters another
    ``Path`` in the predicate slot. That keeps the rewrite rules
    flat and inspectable.
    """

    if isinstance(predicate, SequencePath):
        _emit_sequence_path(visitor, subject, predicate, obj)
        return
    if isinstance(predicate, InvPath):
        _emit_inverse_path(visitor, subject, predicate, obj)
        return
    if isinstance(predicate, AlternativePath):
        # Desugar to a UNION of single-triple BGPs — semantics are
        # identical (SPARQL 1.1 §18.4) and we share the two-phase
        # union emitter so the AQL shape matches an explicit
        # ``{ ?s :p ?o } UNION { ?s :q ?o }``. The union helper
        # re-dispatches each arm through ``_emit_triple`` so PG /
        # LPG / default-collection / RPT branches all compose.
        from .union_paths import emit_alternative_path

        emit_alternative_path(visitor, subject, predicate, obj)
        return
    if isinstance(predicate, MulPath):
        _emit_mul_path(visitor, subject, predicate, obj)
        return
    if isinstance(predicate, NegatedPath):
        _emit_negated_path(visitor, subject, predicate, obj)
        return
    raise UnsupportedSparqlError(f"property path type {type(predicate).__name__!r} is not supported")


def _rpt_path_unsupported(visitor: AlgebraVisitor, subject: Any) -> bool:
    """A path from an RPT-bound subject in a *mixed* mapping.

    Each step's intermediate variable must read the triples table too. In a
    pure triple store (:meth:`SchemaResolver.triple_store_class`) the visitor
    routes every unbound intermediate there, so paths compose; in a mapping
    that mixes RPT with PG or LPG it does not, so the path is refused.
    """
    if not (isinstance(subject, Variable) and str(subject) in visitor.state.var_to_rpt_class):
        return False
    return visitor.resolver.triple_store_class() is None


def _emit_sequence_path(
    visitor: AlgebraVisitor,
    subject: Any,
    predicate: SequencePath,
    obj: Any,
) -> None:
    """Expand ``?s :p1/:p2/.../:pN ?o`` to N triples joined by N-1 fresh vars.

    Per SPARQL 1.1 §18.4, the SequencePath rewrite is purely
    syntactic::

        ?s p1/p2/p3 ?o   ≡   ?s p1 ?_path_1 .
                              ?_path_1 p2 ?_path_2 .
                              ?_path_2 p3 ?o .

    We mint a fresh intermediate variable per inner join point
    (N-1 of them) and dispatch each (s, p_i, o_i) fragment back
    through :meth:`AlgebraVisitor._emit_triple`. The visitor's
    existing per-triple cases handle PG (``?s :name ?n``), LPG
    (``object_property`` edges), and the default-collection
    fallback the W3C harness relies on — no new code paths are
    introduced.

    RPT subjects are rejected with a typed error: an RPT-mapped
    subject's intermediate variables need to *also* be RPT-bound
    so each step reads from the triples table, which requires
    propagating :class:`~arango_sparql.translate.resolver.ResolvedClass`
    through ``state.var_to_rpt_class``. The plumbing is straight-
    forward but adds enough surface area to deserve its own slice;
    until then, RPT + property-paths surfaces a clean
    ``E_UNSUPPORTED_NODE``-class error rather than silently
    emitting wrong AQL (cf. ``.cursor/rules/comprehensiveness-over-
    simplification.mdc`` — "no swallowed errors").
    """

    steps = list(predicate.args)
    if len(steps) < 2:
        # Defensive: rdflib should always synthesise a SequencePath
        # with ≥ 2 steps (a single-step "sequence" would collapse to
        # its sole element at parse time). If a future rdflib version
        # ever hands us a degenerate one-step or zero-step sequence,
        # fall back to either the single-step recurse or a typed
        # error rather than crash with an IndexError downstream.
        if len(steps) == 1:
            visitor._emit_triple((subject, steps[0], obj))
            return
        raise UnsupportedSparqlError("SequencePath has no steps; expected at least one")
    if _rpt_path_unsupported(visitor, subject):
        raise UnsupportedSparqlError("property paths on RPT-mapped subjects are not yet supported")

    current_subject: Any = subject
    for index, step in enumerate(steps):
        is_last = index == len(steps) - 1
        current_object: Any = obj if is_last else visitor._fresh_path_var()
        visitor._emit_triple((current_subject, step, current_object))
        current_subject = current_object


def _emit_inverse_path(
    visitor: AlgebraVisitor,
    subject: Any,
    predicate: InvPath,
    obj: Any,
) -> None:
    """Expand ``?s ^path ?o`` to ``?o path ?s``.

    SPARQL 1.1 §18.4 specifies ``^path`` as the inverse — the same
    set of bindings as ``path``, but with subject and object swapped.
    The rewrite is one line; we recurse via
    :meth:`AlgebraVisitor._emit_triple` so a nested path (e.g.
    ``^(:p/:q)`` whose ``.arg`` is a ``SequencePath``) re-enters the
    dispatcher at the appropriate handler.

    Note: the inner path can be a ``URIRef`` (the common case,
    ``^:knows``) or another ``Path`` instance (``^(:p/:q)`` is an
    ``InvPath`` of a ``SequencePath``). We don't branch on the inner
    type here — the visitor's case-analysis does that on re-entry.
    """

    inner: Any = predicate.arg
    visitor._emit_triple((obj, inner, subject))


def _emit_mul_path(
    visitor: AlgebraVisitor,
    subject: Any,
    predicate: MulPath,
    obj: Any,
) -> None:
    """Expand ``:p*`` / ``:p+`` / ``:p?`` to a bounded UNION of path lengths.

    SPARQL 1.1 §18.4 defines the modifiers as repetition of the inner
    path: ``+`` (one or more), ``*`` (zero or more), ``?`` (zero or one).

    We lower each to ``UNION(arm_0, arm_1, …, arm_N)`` where ``arm_k`` is
    the inner path repeated ``k`` times (``arm_0`` for ``*`` / ``?`` is the
    identity binding ``?o ≡ ?s``). Each non-identity arm calls
    :func:`emit_path_triple` (or :meth:`AlgebraVisitor._emit_triple` for a
    plain ``URIRef`` leaf) so PG edge traversals, LPG discriminators, and
    default-collection joins compose without duplicating traversal logic.

    ``N`` is :attr:`SchemaResolver.property_path_max_depth` (default
    :data:`PROPERTY_PATH_MAX_DEPTH`).

    **Refuse, never truncate (issue #183).** ``*`` and ``+`` have no length
    limit, so ``N`` arms can miss pairs. Each such path also walks ``N + 1``
    repetitions, and the query fails (``FAIL``, message starting
    :data:`~arango_sparql.errors.PATH_DEPTH_EXCEEDED_CODE`) when that walk
    ends at a node the arms do not reach from the same subject. The check is
    exact. Let ``R(s)`` be the nodes reachable from ``s`` in ``1..N`` steps
    (``0..N`` for ``*``) and ``V(s)`` those exactly ``N + 1`` steps away.
    If ``V(s) ⊆ R(s)``, every later step stays inside ``R(s)`` (the step
    after ``V(s)`` lies in the steps after ``R(s)``, which are ``R(s) ∪
    V(s)``), so the answer is complete. If a node ``o`` is first reached
    after ``d > N`` steps, the node ``N + 1`` steps along a shortest walk to
    ``o`` is in ``V(s)`` and not in ``R(s)``, so the query fails. A graph
    with cycles is not refused for having them: walks around a cycle revisit
    nodes already in ``R(s)``.

    The check is anchored on the subject, with the object free: it must see
    every node past the limit, and a fixed object (a constant, a variable
    bound earlier, the subject itself) would hide them. When the object is
    free the arms already give ``R(s)`` and are reused; otherwise a separate
    check walks from the subject to a fresh end point first, and then the
    arms run as before. With a free subject the check covers every subject
    the arms would scan, so ``?s :p+ :b`` is refused when any chain in that
    scan is longer than ``N``, even one that never reaches ``:b``. Refusing
    is the price of not answering short; raise the limit to answer.

    **No limit over an edge collection of its own.** Where the repeated step
    is ``:p`` or ``^:p`` and ``:p`` is such a collection, walked with no
    test on the edge (:func:`_edge_closure`), the ``1..N`` arms and their
    check are replaced by one traversal that walks to the end
    (:func:`_emit_edge_closure_arm`); ``*`` keeps its zero-length arm.

    ``:p?`` is bounded by the modifier itself and has no check.
    """
    # Widen to ``str`` up front: the fold loop below re-assigns from
    # ``_combine_mul_modifiers`` (plain ``str``), which a narrower
    # rdflib ``Literal['*','+','?']`` inference would reject.
    mod: str = predicate.mod
    if mod not in ("*", "+", "?"):
        raise UnsupportedSparqlError(f"property path modifier {mod!r} is not supported")

    inner = predicate.path

    # Collapse nested transitive modifiers — ``(:p*)*`` / ``(:p+)*`` /
    # ``(:p?)+`` / … all reduce to a single equivalent modifier on the
    # leaf path (SPARQL 1.1 §18.4; W3C ``property-path/pp37`` is the
    # ``((:P)*)*`` case). The algebra is exact, not an approximation:
    #
    #   * ``?`` ∘ ``?``                     → ``?``   (at most one hop either way)
    #   * anything involving ``*`` or ``+``:
    #       - includes a zero-hop option iff EITHER modifier admits
    #         zero (``*`` or ``?``)            → ``*``
    #       - otherwise (both are ``+``)        → ``+``
    #
    # Verified against the nine modifier pairs: (``*``,``*``)→``*``,
    # (``+``,``+``)→``+``, (``*``,``+``)/(``+``,``*``)→``*``,
    # (``?``,``+``)/(``+``,``?``)→``*``, (``?``,``*``)/(``*``,``?``)→``*``,
    # (``?``,``?``)→``?``. Looped so arbitrarily deep nesting
    # (``(((:p+)*)?)`` …) folds to one modifier before expansion.
    while isinstance(inner, MulPath):
        mod = _combine_mul_modifiers(mod, inner.mod)
        inner = inner.path

    if isinstance(inner, (AlternativePath, NegatedPath)):
        raise UnsupportedSparqlError(
            f"nested property path {type(inner).__name__!r} inside MulPath (':p{mod}') is not supported"
        )

    if _rpt_path_unsupported(visitor, subject):
        raise UnsupportedSparqlError("property paths on RPT-mapped subjects are not yet supported")

    max_depth = visitor.resolver.property_path_max_depth
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 1:
        raise ValueError(f"SchemaResolver.property_path_max_depth must be an integer >= 1, got {max_depth!r}")
    if mod == "?":
        min_len, max_len = 0, 1
    elif mod == "+":
        min_len, max_len = 1, max_depth
    else:  # mod == "*"
        min_len, max_len = 0, max_depth

    # Small named factories rather than lambdas-with-defaults: mypy
    # cannot infer a lambda whose extra parameters exist only to bind
    # loop state, and the factory spells out the closure explicitly.
    def _zero_hop_driver(s: Any, o: Any) -> Callable[[AlgebraVisitor], None]:
        def drive(v: AlgebraVisitor) -> None:
            _emit_zero_hop_path(v, s, o)

        return drive

    def _expanded_arm_driver(s: Any, p: Any, o: Any) -> Callable[[AlgebraVisitor], None]:
        def drive(v: AlgebraVisitor) -> None:
            _emit_expanded_path_arm(v, s, p, o)

        return drive

    def _arm_drivers(end: Any) -> list[Callable[[AlgebraVisitor], None]]:
        drivers: list[Callable[[AlgebraVisitor], None]] = []
        if min_len == 0:
            drivers.append(_zero_hop_driver(subject, end))
        for length in range(max(1, min_len), max_len + 1):
            drivers.append(_expanded_arm_driver(subject, _repeat_inner_path(inner, length), end))
        return drivers

    from .union_paths import DepthGuard, _emit_union_of_arms

    # The path's end points are projected even when they are internal
    # sigils: ``:p+/:q`` hands its ``_path_<n>`` object to ``:q``.
    endpoints = frozenset(str(term) for term in (subject, obj) if isinstance(term, Variable))
    arm_drivers = _arm_drivers(obj)

    if mod == "?":
        if len(arm_drivers) == 1:
            arm_drivers[0](visitor)
            return
        # Repetition arms are per-length walks; when both ends are constant
        # they bind only internal sigils, so collapse to the existence
        # sentinel rather than re-projecting a fresh intermediate per length.
        _emit_union_of_arms(visitor, arm_drivers, distinct=True, existence_collapse=True, keep_vars=endpoints)
        return

    repeated = _path_text(inner) if isinstance(inner, URIRef) else f"({_path_text(inner)})"
    closure = _edge_closure(visitor, subject, inner, obj)
    if closure is not None:
        # One edge collection, walked to the end: complete, no depth limit
        # and no depth check (:func:`_emit_edge_closure_arm`).
        start, prop, end = closure
        drivers = [_zero_hop_driver(subject, obj)] if mod == "*" else []
        drivers.append(_edge_closure_driver(start, prop, end, f"{repeated}{mod}"))
        _emit_union_of_arms(visitor, drivers, distinct=True, existence_collapse=True, keep_vars=endpoints)
        return

    # ``*`` / ``+``: guard the bound (issue #183, docstring above).
    message = visitor.builder.bind(_depth_limit_message(f"{repeated}{mod}", max_depth), hint="path_depth")
    beyond_path = _repeat_inner_path(inner, max_len + 1)
    if _is_unbound_variable(visitor, obj, subject):
        _emit_union_of_arms(
            visitor,
            arm_drivers,
            distinct=True,
            existence_collapse=True,
            keep_vars=endpoints,
            depth_guard=DepthGuard(_expanded_arm_driver(subject, beyond_path, obj), message),
        )
        return

    end = visitor._fresh_path_var()
    _emit_union_of_arms(
        visitor,
        _arm_drivers(end),
        distinct=True,
        existence_collapse=True,
        keep_vars=endpoints | {str(end)},
        depth_guard=DepthGuard(_expanded_arm_driver(subject, beyond_path, end), message, only=True),
    )
    if len(arm_drivers) == 1:
        arm_drivers[0](visitor)
        return
    _emit_union_of_arms(visitor, arm_drivers, distinct=True, existence_collapse=True, keep_vars=endpoints)


def _is_unbound_variable(visitor: AlgebraVisitor, term: Any, subject: Any) -> bool:
    """Whether *term* is a variable nothing has bound yet, other than *subject*.

    Such an object leaves the path's arms unconstrained at the far end, so
    their rows are every pair the depth guard needs to see.
    """
    if not isinstance(term, Variable) or term == subject:
        return False
    name = str(term)
    return name not in visitor.state.var_to_expr and name not in visitor.state.var_to_doc_alias


def _edge_closure(visitor: AlgebraVisitor, subject: Any, inner: Any, obj: Any) -> tuple[Any, Any, Any] | None:
    """``(start, property, end)`` when ``subject inner* obj`` (or ``+``) can be
    answered by one traversal of an edge collection; ``None`` otherwise.

    *inner* is ``:p`` or ``^:p`` with ``:p`` stored in an edge collection that
    the per-hop triple walks with no condition on the edge itself: a
    dedicated collection, or a shared one without a type discriminator. Not
    for an edge type test (a walk that reaches a vertex once must not reach
    it first along an edge of another type, and only a test inside the
    traversal could stop that), a reified relationship (its triples end at
    the edge, not a vertex), an exit predicate, or triples stored as rows
    (RPT). The start must be read as a document, as the triple's own subject
    is: not routed to a triples table.

    ``(^:p)+`` is ``^(:p+)`` (SPARQL 1.1 §9.3: a path's inverse repeated is
    the repeated path inverted), so it is walked forwards from the object,
    as ``?s ^:p+ ?o`` is (:func:`_emit_inverse_path`). Anything else (a
    sequence ``(:p/:q)+``, a property stored as a document attribute, a pure
    triple store) keeps the bounded arms and their depth check, as does
    ``:x (^:p)+ ?y``, which the arms walk backwards from ``:x``.
    """
    if isinstance(inner, InvPath) and isinstance(inner.arg, URIRef):
        start, iri, end = obj, inner.arg, subject
        if _is_unbound_variable(visitor, start, None) and not _is_unbound_variable(visitor, end, None):
            # ``:x (^:p)+ ?y``: the arms walk back from ``:x`` hop by hop;
            # forwards, the walk would start from every candidate ``?y``, a
            # scan the arms never make for this shape. ``?y :p+ :x`` (and
            # ``:x ^:p+ ?y``) walk forwards in the arms too, and are walked.
            return None
    elif isinstance(inner, URIRef):
        start, iri, end = subject, inner, obj
    else:
        return None
    resolver = visitor.resolver
    if resolver.triple_store_class() is not None:
        return None
    if isinstance(start, Variable):
        if str(start) in visitor.state.var_to_rpt_class:
            return None
    elif isinstance(start, URIRef):
        if str(start) in visitor.state.const_subject_rpt_class:
            return None
    else:
        return None
    if _walked_back_from_document(visitor, start, end):
        return None
    if resolver.is_reified_entry(iri) or resolver.is_edge_exit(iri):
        return None
    prop = resolver.resolve_property(iri)
    if (
        not prop.is_object_property
        or prop.edge_collection is None
        or prop.mapping_style == "RPT_EDGE"
        or prop.reified_edge
        or prop.edge_target_of
        or (prop.mapping_style == "GENERIC_WITH_TYPE" and prop.type_field and prop.type_value)
    ):
        return None
    return start, prop, end


def _walked_back_from_document(visitor: AlgebraVisitor, start: Any, end: Any) -> bool:
    """Whether the one-hop arm walks backwards from a document the query
    already holds for *end*, which a traversal from *start* cannot match.

    With the start a variable nothing has bound, the edge triple walks back
    from its object when that is a document
    (:meth:`AlgebraVisitor._reverse_traversal_source`). An end bound to a
    document (or a constant read as one) can be one row per copy of its IRI
    (an entity of two classes, ``copies.copies_interchangeable``), and the
    walk back from each row's own document is what keeps one answer per
    entity; a walk forwards meets each copy by IRI and answers once per copy
    (``tests/cross/test_multi_document_iri_cross.py``, ``incoming_path``).
    Those paths keep the arms. An end known only by its IRI, a constant or a
    value, is met by IRI either way.
    """
    state = visitor.state
    if (
        not isinstance(start, Variable)
        or str(start) in state.var_to_doc_alias
        or str(start) in state.var_to_expr
    ):
        return False
    if isinstance(end, Variable):
        return str(end) in state.var_to_doc_alias
    return isinstance(end, URIRef) and str(end) in state.const_subject_alias


def _edge_closure_driver(start: Any, prop: Any, end: Any, path: str) -> Callable[[AlgebraVisitor], None]:
    def drive(v: AlgebraVisitor) -> None:
        _emit_edge_closure_arm(v, start, prop, end, path)

    return drive


def _emit_edge_closure_arm(visitor: AlgebraVisitor, start: Any, prop: Any, end: Any, path: str) -> None:
    """Bind *end* to every node one or more ``prop`` edges from *start*, once.

    The arms of :func:`_emit_mul_path` walk the path ``1..N`` times; this is
    the same walk taken to the end, by one breadth-first traversal per
    start. What each hop enforces is kept:

    * The start is read as the first hop's subject is
      (:meth:`AlgebraVisitor._ensure_subject_alias` with the property):
      its domain collection and class, its tenant filter, the named-graph
      scope, a constant IRI or a value bound earlier. Later hops start from
      a reached vertex, which the arms do not re-read, so neither does this.
    * Every vertex a hop reaches is held to the tenant
      (:meth:`AlgebraVisitor._tenant_test`), as the arms' ``FILTER`` holds
      each hop's vertex: a vertex that fails is not an answer
      (``FILTER``), and nothing is reached through it (``PRUNE``; ``e !=
      null`` spares the start, which ArangoDB's ``PRUNE`` also sees at
      depth 0 and which the arms never test this way).
    * The end is bound as the last hop's object is
      (:meth:`AlgebraVisitor._bind_vertex_object`): a fresh variable takes
      the vertex, a constant or a bound variable is compared by IRI.

    ``uniqueVertices: "global"`` reaches each vertex once, breadth first,
    which makes the walk linear in the edges reached rather than in the
    walks. It is exact: a vertex reachable along vertices that pass the
    tenant test is reached along the shortest such path (pruning stops
    only at vertices that fail, which no such path uses), and a vertex
    reached is reachable. Two consequences are handled here:

    * The start counts as visited, so a cycle back to it is not reported,
      though ``:p+`` holds there. ``back`` adds it: the start is one or more
      steps from itself exactly when an edge leads to it from the start or
      from a reached vertex (and it passes the test a reached vertex must).
    * A path that reaches each vertex once uses each edge at most once, so
      no answer is more than ``LENGTH(edge collection)`` steps away. The
      depth bound is :data:`EDGE_CLOSURE_MAX_DEPTH`, and the arm refuses
      (``FAIL``, :data:`~arango_sparql.errors.PATH_DEPTH_EXCEEDED_CODE`)
      when the collection holds more edges than that, so the bound cannot
      cut an answer short.

    Rows repeat only where the start has several documents; the caller's
    ``UNION_DISTINCT`` keeps each pair once (set semantics, SPARQL 1.1 §9.3).
    """
    builder = visitor.builder
    assert prop.edge_collection is not None  # _edge_closure guarantees it
    edges = builder.bind_collection(prop.edge_collection)
    depth = builder.bind(EDGE_CLOSURE_MAX_DEPTH, hint="depth")
    refusal = builder.bind(_edge_count_message(path, prop.edge_collection), hint="path_depth")
    builder.filter_raw(f"LENGTH({edges}) <= {depth} || FAIL({refusal})")
    source = visitor._ensure_subject_alias(start, prop)
    direction = "ANY" if prop.symmetric else "OUTBOUND"

    # Every vertex reachable in one or more steps, except (see ``back``) the
    # start itself.
    walked, walked_edge = builder.fresh_alias(prefix="v"), builder.fresh_alias(prefix="e")
    test = visitor._tenant_test(walked)
    order = builder.bind("bfs", hint="order")
    unique = builder.bind("global", hint="unique")
    walk = builder.create_child()
    walk.for_reachable(
        walked,
        walked_edge,
        source,
        prop.edge_collection,
        direction=direction,
        min_depth=1,
        max_depth=depth,
        order=order,
        unique_vertices=unique,
        prune=None if test is None else f"{walked_edge} != null && !({test})",
    )
    if test is not None:
        walk.filter_raw(test)
    walk.return_scalar(walked)
    reach = builder.fresh_alias(prefix="reach")
    builder.let(reach, f"(\n{indent_aql(builder.absorb_child(walk))}\n)")

    # The start, when one more step from it or from a reached vertex returns
    # to it: found one step backwards from the start (its in-edges, usually
    # few), not one step forwards from every reached vertex, which cost a
    # 1,000-hop chain three times the walk itself.
    back_test = visitor._tenant_test(source)
    hop_from, hop_edge = builder.fresh_alias(prefix="v"), builder.fresh_alias(prefix="e")
    probe = builder.create_child()
    if back_test is not None:
        probe.filter_raw(back_test)
    probe.for_traversal(
        hop_from, hop_edge, source, prop.edge_collection, direction="ANY" if prop.symmetric else "INBOUND"
    )
    probe.filter_raw(f"{hop_from}._id == {source}._id || {hop_from}._id IN {reach}[*]._id")
    probe.limit(1)
    probe.return_scalar(source)
    back = builder.fresh_alias(prefix="back")
    builder.let(back, f"(\n{indent_aql(builder.absorb_child(probe))}\n)")

    reached = builder.fresh_alias(prefix="v")
    builder.for_inline(reached, f"APPEND({reach}, {back})")
    visitor.state.doc_to_collection[reached] = prop.edge_collection
    visitor._record_vertex_home(reached, visitor._usable_range(prop))
    visitor._bind_vertex_object(end, reached, prop)


def _edge_count_message(path: str, edge_collection: str) -> str:
    """The run-time refusal for an edge collection too large for the depth bound."""
    return (
        f"{PATH_DEPTH_EXCEEDED_CODE}: property path {path} is walked as a traversal of at "
        f"most {EDGE_CLOSURE_MAX_DEPTH} steps, and edge collection {edge_collection!r} holds "
        "more edges than that, so a longer path could exist. The query is refused rather "
        "than answered without it."
    )


def _path_text(path: Any) -> str:
    """The path as SPARQL writes it, for messages (``<http://ex.org/p>``)."""
    n3 = getattr(path, "n3", None)
    return n3() if callable(n3) else str(path)


def _depth_limit_message(path: str, max_depth: int) -> str:
    """The run-time refusal for a ``*`` / ``+`` path longer than the limit."""
    return (
        f"{PATH_DEPTH_EXCEEDED_CODE}: property path {path} reaches nodes more than "
        f"{max_depth} repetitions away, past SchemaResolver.property_path_max_depth "
        f"({max_depth}). The query is refused rather than answered without them; "
        "raise property_path_max_depth to answer it."
    )


def _emit_negated_path(
    visitor: AlgebraVisitor,
    subject: Any,
    predicate: NegatedPath,
    obj: Any,
) -> None:
    """Expand ``?s !(:p1|:p2|…) ?o`` to an ATTRIBUTES fan-out with NOT IN.

    SPARQL 1.1 §18.4 defines ``!iri`` as the set of triples whose
    predicate is *not* in the negated set. In the PG / LPG / default-
    collection document model "the predicate" is the attribute name
    on the subject document, so a negated path is::

        FOR k IN ATTRIBUTES(<subject>, true)
        FILTER k NOT IN [<system_attrs>, <resolved negated attrs>]
        -- ?o bound to <subject>[k]

    which iterates every non-system attribute on the subject and
    yields one binding row per attribute whose name isn't in the
    negated set. Shares the ``SYSTEM_ATTRIBUTES_TO_SKIP`` /
    ``graph_field`` filter shape with
    :func:`arango_sparql.translate.variable_predicates._emit_attributes_branch`
    so the same wildcard-leak guarantees apply (named-graph metadata
    never surfaces as a triple predicate).

    **Limitations (deliberate XFAILs):**

    * Inverse arms (``!(^:p)`` — "any predicate that is not the
      *incoming* :p") require walking every other document's
      outgoing edges to find candidates whose target is this
      subject; that's a join across the entire collection per
      query, qualitatively different from forward-attribute
      iteration. Surfaced as ``UnsupportedSparqlError`` with the
      ``inverse arms`` substring so the W3C XFAIL bucket is greppable.

    * RPT subjects are rejected with the same typed-error shape
      :func:`_emit_sequence_path` uses — the triples table needs
      its own per-arm emission (``FOR row IN @@triples FILTER
      row.subject == @s && row.predicate NOT IN @neg_preds``)
      that we haven't ported yet. Same XFAIL bucket as
      property-paths-on-RPT.
    """

    # ---- Reject inverse arms early --------------------------------
    # A NegatedPath whose ``.args`` includes an InvPath has different
    # semantics than the forward-only fan-out below, so a partial
    # emission would be silently wrong. Catch it at dispatch time
    # with a stable, greppable message.
    #
    # rdflib represents the parsed arm in two equivalent forms
    # depending on grammar context:
    #   * the runtime ``rdflib.paths.InvPath`` class for nested
    #     inverse paths reached through the path-algebra builder; and
    #   * a ``CompValue`` whose ``.name == "InversePath"`` for the
    #     ``!(^:p)`` shape, which is constructed by the SPARQL
    #     parser directly (verified at module-load time against
    #     rdflib 7.x).
    # Probe both so the XFAIL message matches what the user wrote.
    for arm in predicate.args:
        is_inverse = isinstance(arm, InvPath) or getattr(arm, "name", None) == "InversePath"
        if is_inverse:
            raise UnsupportedSparqlError(
                "negated property paths with inverse arms ('!(^:p)') are not yet supported"
            )
        if not isinstance(arm, URIRef):
            raise UnsupportedSparqlError(
                f"negated property path arm of type "
                f"{type(arm).__name__!r} is not supported "
                f"(only forward IRI arms are implemented)"
            )

    # ---- Reject RPT subjects --------------------------------------
    if isinstance(subject, Variable) and str(subject) in visitor.state.var_to_rpt_class:
        raise UnsupportedSparqlError("negated property paths on RPT-mapped subjects are not yet supported")

    # ---- Resolve negated predicates to physical attribute names ---
    # The resolver may surface a ``W_SCHEMA_UNMAPPED_IRI`` warning
    # per predicate IRI that isn't in the ontology — that's the
    # right behaviour: a permissive resolver falls back to the
    # local-name attribute, so an unknown ``ex:p1`` still becomes
    # ``"p1"`` in the negated set. A non-IRI arm (``!(^:p)`` — a
    # nested inverse path inside the negated set) has no attribute
    # to negate; refuse explicitly rather than crash in the resolver.
    negated_iris: list[URIRef] = []
    for arm in predicate.args:
        if not isinstance(arm, URIRef):
            raise UnsupportedSparqlError(
                f"negated property path arm {arm!r} is not a plain IRI "
                f"(nested path forms inside '!(...)' are not supported)"
            )
        negated_iris.append(arm)
    # Local import, like SYSTEM_ATTRIBUTES_TO_SKIP below.
    from .variable_predicates import refuse_nested_attribute_scan

    refuse_nested_attribute_scan(visitor, "a negated property path")
    negated_attrs = sorted({visitor.resolver.resolve_property(iri).attribute for iri in negated_iris})

    # ---- Open the subject FOR -------------------------------------
    # Handles both Variable and URIRef subjects; for URIRef it
    # adds the ``alias._uri == @uri`` FILTER. For Variable it
    # opens a default-collection FOR if no prior binding exists,
    # or reuses the existing alias.
    subject_alias = visitor._ensure_subject_alias(subject)

    # ---- ATTRIBUTES fan-out + NOT IN guard -------------------------
    # Same skip-list construction as the variable-predicates
    # emitter so the two stay in lockstep — extending one (e.g.
    # to skip a new system attribute) extends the other for free.
    # Local import to avoid a hard module-level coupling between
    # paths.py and variable_predicates.py (they otherwise have no
    # call-graph relationship).
    from .variable_predicates import SYSTEM_ATTRIBUTES_TO_SKIP

    key_alias = visitor.builder.fresh_alias(prefix="k")
    visitor.builder.for_attributes(key_alias, subject_alias)
    skip_list = sorted(
        {
            *SYSTEM_ATTRIBUTES_TO_SKIP,
            visitor.resolver.graph_field,
            *negated_attrs,
        }
    )
    skip_bind = visitor.builder.bind(skip_list, hint="neg_path_skip")
    visitor.builder.filter_raw(f"{key_alias} NOT IN {skip_bind}")

    # ---- Bind the object ------------------------------------------
    # ``?o`` binds to the attribute value at this key. Same
    # var_to_expr-aware logic the variable-predicates emitter
    # uses, so a ``?o`` that's also bound by another triple gets
    # the equality-FILTER join automatically.
    value_expr = f"{subject_alias}[{key_alias}]"
    if isinstance(obj, Variable):
        o_name = str(obj)
        existing = visitor.state.var_to_expr.get(o_name)
        if existing is None:
            visitor._record_var_expr(obj, value_expr)
        elif existing != value_expr:
            visitor.builder.filter_raw(f"{value_expr} == {existing}")
        return
    if isinstance(obj, (Literal, URIRef)):
        # Local import to avoid circular dependency at module
        # import time — :mod:`visitor` already imports this module.
        from .visitor import _term_to_python

        obj_bind = visitor.builder.bind(_term_to_python(obj), hint="obj")
        visitor.builder.filter_eq(value_expr, obj_bind)
        return
    raise UnsupportedSparqlError(
        f"negated property path object term type {type(obj).__name__!r} is not supported"
    )


def _emit_zero_hop_path(
    visitor: AlgebraVisitor,
    subject: Any,
    obj: Any,
) -> None:
    """Identity arm for ``:p*`` / ``:p?`` — bind ``?o`` to the same term as ``?s``.

    SPARQL 1.1 §18.5 (ZeroLengthPath): a zero-length path from a term
    matches that term, whether or not a document stores it. So a constant
    subject is the IRI itself, and a subject already bound to a *value* —
    an earlier triple's object, a sub-select column, a ``VALUES`` row — is
    that value. Only a subject with no binding at all needs a document to
    range over.
    """
    if isinstance(subject, URIRef):
        uri_expr = visitor.builder.bind(str(subject), hint="uri")
    elif (
        isinstance(subject, Variable)
        and str(subject) not in visitor.state.var_to_doc_alias
        and str(subject) in visitor.state.var_to_expr
    ):
        uri_expr = visitor.state.var_to_expr[str(subject)]
    else:
        uri_expr = subject_ref(visitor._ensure_subject_alias(subject))
    if not visitor.builder.has_for_clause():
        # The arm reads no collection (a UNION arm is its own subquery);
        # give its FILTER/RETURN a scope, as an empty BGP does (``visit_BGP``).
        visitor.builder.for_inline(visitor.builder.fresh_alias(prefix="empty"), "[1]")
    if isinstance(obj, Variable):
        o_name = str(obj)
        existing = visitor.state.var_to_expr.get(o_name)
        if existing is None:
            visitor.state.var_to_expr[o_name] = uri_expr
        elif existing != uri_expr:
            visitor.builder.filter_raw(f"{uri_expr} == {existing}")
        return
    if isinstance(obj, URIRef):
        bind = visitor.builder.bind(str(obj), hint="uri")
        visitor.builder.filter_eq(uri_expr, bind)
        return
    raise UnsupportedSparqlError(
        f"zero-hop property path object term type {type(obj).__name__!r} is not supported"
    )


def _emit_expanded_path_arm(
    visitor: AlgebraVisitor,
    subject: Any,
    expanded: Any,
    obj: Any,
) -> None:
    """Emit one UNION arm: a fixed-length expansion of the inner path."""
    if isinstance(expanded, Path):
        emit_path_triple(visitor, subject, expanded, obj)
        return
    if isinstance(expanded, URIRef):
        visitor._emit_triple((subject, expanded, obj))
        return
    raise UnsupportedSparqlError(f"repeated path inner type {type(expanded).__name__!r} is not supported")


def _combine_mul_modifiers(outer: str, inner: str) -> str:
    """Fold two nested transitive-path modifiers into one.

    See the table in :func:`_emit_mul_path`. ``outer`` is the modifier
    applied to the (already-modified) inner path; ``inner`` is the
    modifier on the leaf. Both are one of ``"*"`` / ``"+"`` / ``"?"``.
    """
    if outer == "?" and inner == "?":
        return "?"
    # At least one side is ``*`` or ``+`` → the result is unbounded.
    # It admits the zero-hop (identity) arm iff either side does, i.e.
    # either side is ``*`` (zero-or-more) or ``?`` (zero-or-one).
    includes_zero = outer in ("*", "?") or inner in ("*", "?")
    return "*" if includes_zero else "+"


def _repeat_inner_path(inner: Any, count: int) -> Any:
    """Repeat *inner* *count* times as a path (URIRef or SequencePath)."""
    if count < 1:
        raise ValueError(f"path repeat count must be >= 1, got {count}")
    if isinstance(inner, URIRef):
        if count == 1:
            return inner
        return SequencePath(*([inner] * count))
    if isinstance(inner, SequencePath):
        if count == 1:
            return inner
        parts: list[Any] = []
        for _ in range(count):
            parts.extend(inner.args)
        return SequencePath(*parts)
    if isinstance(inner, InvPath):
        if count == 1:
            return inner
        return SequencePath(*([inner] * count))
    raise UnsupportedSparqlError(f"cannot repeat property path inner type {type(inner).__name__!r}")


# ---------------------------------------------------------------------------
# Re-exports for downstream callers (the visitor only ever imports
# ``emit_path_triple``; the other names are exported so tests and any
# future inline-expansion site can short-circuit a known case without
# round-tripping through the dispatch).
# ---------------------------------------------------------------------------

__all__ = [
    "emit_path_triple",
    # Useful for isinstance checks in tests / introspection.
    "Path",
    "SequencePath",
    "InvPath",
    "AlternativePath",
    "MulPath",
    "NegatedPath",
]


# ``URIRef`` is imported for the type hints above; rdflib's static
# analyzer flags unused imports otherwise. The runtime cost is nil
# since rdflib is already loaded by the visitor.
_URIREF_FOR_TYPING = URIRef
