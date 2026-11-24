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
from .builder import subject_ref

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

    # ``*`` / ``+``: guard the bound (issue #183, docstring above).
    repeated = _path_text(inner) if isinstance(inner, URIRef) else f"({_path_text(inner)})"
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
