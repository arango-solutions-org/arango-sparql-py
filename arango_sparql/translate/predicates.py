"""Caller-registered predicates (magic properties): a seam (issue #102).

Stores extend SPARQL with reserved predicates in ordinary triple patterns
(Jena's ``text:query``, Stardog's ``textMatch``). This library knows none of
them. A caller registers a handler per predicate IRI
(``SchemaResolver(predicates={iri: handler})``), and a basic graph pattern
that uses the IRI is compiled through it. Jena's list shape is read on both
sides::

    (?ticket ?score) <p> (c:body "late delivery" 10) .

The subject list's first item is the entity; the rest are variables the
handler may bind (a score). The object list holds the handler's arguments.
rdflib expands both lists to ``rdf:first`` / ``rdf:rest`` triples over blank
nodes; :func:`extract_predicate_calls` collects them back.

**What a handler may do.** The entity's documents are read by one loop. The
handler returns a :class:`LoopPlan` that can make that loop iterate another
source (an ArangoSearch view), add ``SEARCH`` and ``OPTIONS``, add ``SORT``
and ``LIMIT``, and bind its subject-list variables. Everything it writes goes
through the :class:`LoopEmitter`: values as bind parameters, never spliced
into the query. A handler refuses with
:class:`~arango_sparql.errors.UnsupportedSparqlError`.

**Where it applies.** The entity must be opened in the same group by a type
pattern (``?ticket a c:Ticket``) of a class stored in its own collection;
that pattern's loop is the one the handler shapes. Anything else is refused
with code :data:`PREDICATE_ERROR`. A plan with ``SORT`` / ``LIMIT`` must be
the query's first loop, so the limit counts the entity's own matches; it is
applied right after the entity's loop opens, before the rest of the pattern.

An IRI with no registered handler is an ordinary predicate, as before.

:func:`text_match_handler` is a ready handler for ArangoSearch full-text
matching.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rdflib import RDF, BNode, Literal, URIRef, Variable

from ..errors import UnsupportedSparqlError
from .builder import attribute_path_ref

if TYPE_CHECKING:
    from .builder import AqlQueryBuilder
    from .resolver import ResolvedClass, ResolvedProperty

#: The error code every refusal from this seam carries.
PREDICATE_ERROR = "E_PREDICATE"


@dataclass(frozen=True)
class PredicateCall:
    """One use of a registered predicate, as its handler sees it."""

    predicate: str
    subjects: tuple[Any, ...]
    """The subject list as rdflib terms; ``subjects[0]`` is the entity."""
    arguments: tuple[Any, ...]
    """The object list as rdflib terms (IRIs, literals)."""
    class_iri: str = ""
    collection: str = ""
    """The entity's class and the collection its loop reads."""
    searched: ResolvedProperty | None = None
    """The physical mapping of ``arguments[0]``, when it is a datatype
    property IRI (the searched property)."""

    @property
    def entity(self) -> Variable:
        return self.subjects[0]

    def refuse(self, reason: str) -> UnsupportedSparqlError:
        return UnsupportedSparqlError(f"<{self.predicate}>: {reason}", code=PREDICATE_ERROR)


@dataclass
class LoopEmitter:
    """What a handler writes AQL with: the entity's loop alias, bind
    parameters, and attribute references on the alias."""

    builder: AqlQueryBuilder
    alias: str

    def bind(self, value: Any, *, hint: str | None = None) -> str:
        """A value bind parameter (``@name``)."""
        return self.builder.bind(value, hint=hint)

    def bind_source(self, name: str) -> str:
        """A collection or view bind parameter (``@@name``)."""
        return self.builder.bind_collection(name)

    def field(self, path: tuple[str, ...]) -> str:
        """The attribute at ``path`` on the entity's document."""
        return attribute_path_ref(self.alias, path)


@dataclass(frozen=True)
class LoopPlan:
    """How the entity's loop reads (every text written with the emitter)."""

    source: str
    """An ``@@`` bind from :meth:`LoopEmitter.bind_source`."""
    search: str | None = None
    options: str | None = None
    """An AQL object literal for ``OPTIONS``, such as
    ``{ collections: [@coll] }``."""
    sort: str | None = None
    descending: bool = True
    limit: str | None = None
    """A bind parameter from :meth:`LoopEmitter.bind`."""
    bindings: Mapping[str, str] = field(default_factory=dict)
    """Subject-list variable name → the AQL expression it binds to."""


PredicateHandler = Callable[[PredicateCall, LoopEmitter], LoopPlan]


def _list_items(head: Any, cells: dict[Any, dict[Any, Any]], used: set[Any], what: str) -> list[Any]:
    items: list[Any] = []
    node = head
    while node != RDF.nil:
        cell = cells.get(node)
        if cell is None or RDF.first not in cell or RDF.rest not in cell:
            raise UnsupportedSparqlError(f"{what} is not a well-formed list", code=PREDICATE_ERROR)
        used.add(node)
        items.append(cell[RDF.first])
        node = cell[RDF.rest]
    return items


def extract_predicate_calls(
    triples: list[Any], predicates: Mapping[str, PredicateHandler]
) -> tuple[list[Any], list[PredicateCall]]:
    """Split a BGP into its ordinary triples and its registered-predicate calls.

    A list on either side of a call is read back from its blank-node cells,
    and those cells leave the BGP with the call, before the visitor turns
    blank nodes into variables.
    """
    registered = {URIRef(iri) for iri in predicates}
    heads = [t for t in triples if t[1] in registered]
    if not heads:
        return triples, []
    cells: dict[Any, dict[Any, Any]] = {}
    for s, p, o in triples:
        if isinstance(s, BNode) and p in (RDF.first, RDF.rest):
            cells.setdefault(s, {})[p] = o
    used: set[Any] = set()
    calls: list[PredicateCall] = []
    for subject, predicate, obj in heads:
        what = f"<{predicate}>"
        subjects = (
            _list_items(subject, cells, used, f"{what}'s subject")
            if isinstance(subject, BNode)
            else [subject]
        )
        arguments = _list_items(obj, cells, used, f"{what}'s object") if isinstance(obj, BNode) else [obj]
        if not subjects or not isinstance(subjects[0], Variable):
            raise UnsupportedSparqlError(
                f"{what}'s subject must be a variable or a list starting with one", code=PREDICATE_ERROR
            )
        if any(not isinstance(s, Variable) for s in subjects[1:]):
            raise UnsupportedSparqlError(
                f"{what} binds only variables in its subject list", code=PREDICATE_ERROR
            )
        calls.append(PredicateCall(str(predicate), tuple(subjects), tuple(arguments)))
    remaining = [
        t for t in triples if t[1] not in registered and not (t[0] in used and t[1] in (RDF.first, RDF.rest))
    ]
    return remaining, calls


def resolve_call(
    call: PredicateCall, resolved: ResolvedClass, prop: ResolvedProperty | None
) -> PredicateCall:
    return PredicateCall(
        call.predicate, call.subjects, call.arguments, resolved.iri, resolved.collection, prop
    )


# ---------------------------------------------------------------------------
# A ready handler: full-text matching through an ArangoSearch view
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextIndex:
    """One collection's fields as an ArangoSearch view indexes them."""

    view: str
    analyzer: str
    """The analyzer the view applies to :attr:`fields`. A query is tokenized
    with the same one; a different analyzer would match nothing silently."""
    fields: tuple[str, ...]
    """Attribute names (dotted for a nested path) in the collection."""

    def __post_init__(self) -> None:
        if not self.view or not self.analyzer or not self.fields:
            raise ValueError("a text index needs a view, an analyzer and at least one field")


def text_match_handler(indexes: Mapping[str, TextIndex]) -> PredicateHandler:
    """A handler for ``(?entity [?score]) <p> (:property "terms" [limit])``.

    ``indexes`` is by collection. An entity matches when every term of
    ``"terms"``, tokenized by the view's analyzer, occurs in the searched
    property's field. The entity's loop iterates the view, restricted to the
    entity's collection; ``?score`` is BM25; a limit keeps the best-scored
    matches.
    """

    def handle(call: PredicateCall, emit: LoopEmitter) -> LoopPlan:
        if not 2 <= len(call.arguments) <= 3:
            raise call.refuse('takes (:property "terms") or (:property "terms" limit)')
        prop_iri, terms = call.arguments[0], call.arguments[1]
        if not isinstance(prop_iri, URIRef):
            raise call.refuse(f"the searched property must be an IRI, not {prop_iri.n3()}")
        if not isinstance(terms, Literal) or terms.datatype is not None or not str(terms).strip():
            raise call.refuse("the terms must be a non-empty plain string")
        limit: int | None = None
        if len(call.arguments) == 3:
            raw = call.arguments[2]
            value = raw.toPython() if isinstance(raw, Literal) else None
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise call.refuse("the limit must be a positive integer")
            limit = value
        if len(call.subjects) > 2:
            raise call.refuse("binds at most a score: (?entity ?score)")
        index = indexes.get(call.collection)
        if index is None:
            raise call.refuse(f"collection {call.collection!r} has no text index")
        if call.searched is None:
            raise call.refuse(f"{prop_iri.n3()} is not a mapped datatype property of {call.class_iri}")
        path = call.searched.field_path
        if ".".join(path) not in index.fields:
            raise call.refuse(f"view {index.view!r} does not index {call.collection}.{'.'.join(path)}")
        query = emit.bind(str(terms), hint="text")
        analyzer = emit.bind(index.analyzer, hint="analyzer")
        collection = emit.bind(call.collection, hint="collection")
        score = f"BM25({emit.alias})"
        bindings = {str(call.subjects[1]): score} if len(call.subjects) == 2 else {}
        return LoopPlan(
            source=emit.bind_source(index.view),
            search=f"ANALYZER(TOKENS({query}, {analyzer}) ALL == {emit.field(path)}, {analyzer})",
            options=f"{{ collections: [{collection}] }}",
            sort=score if limit is not None else None,
            limit=emit.bind(limit, hint="limit") if limit is not None else None,
            bindings=bindings,
        )

    return handle
