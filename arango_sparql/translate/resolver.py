"""URI → ArangoDB collection / property resolution.

Wraps an in-memory ``rdflib.Graph`` loaded from the OWL/Turtle ontology
that ``arango-schema-mapper`` produces. The mapper's emitter
(``references/arango-schema-mapper/schema_analyzer/owl_export.py``)
attaches three annotation properties under the ``phys:`` namespace:

- ``phys:collectionName "..."`` on every ``owl:Class`` → ArangoDB
  document collection name.
- ``phys:edgeCollectionName "..."`` on every ``owl:ObjectProperty`` →
  ArangoDB edge collection name.
- ``phys:typeField`` / ``phys:typeValue`` for hybrid (multi-class)
  collections — used to emit the type test (``builder.type_predicate``):
  ``doc.<typeField> == <typeValue>`` for a scalar field, ``<typeValue> IN
  doc.<typeField>`` for an array of labels (``phys:typeFieldIsArray``).
- ``phys:attributeName "..."`` on an ``owl:DatatypeProperty`` → the stored
  attribute, one literal name (a dot is part of it); or
  ``phys:attributePath '["address", "city"]'`` → a value inside a nested
  object, one segment per level (issue #72). Never both.

The resolver normalizes every spelling of the physical IRI seen in the
wild so callers do not have to care which mapper version produced the
ontology. The canonical spelling is the one the analyzers actually emit
by default (``arango-schema-analyzer``'s
``DEFAULT_OWL_PHYSICAL_IRI = http://arangodb.com/schema/physical#``);
the older ``arango.solutions/phys#`` and ``arango-schema-mapper/phys#``
spellings are accepted for back-compat.

Visitors call into this resolver rather than touching the ontology
graph directly so the lookup surface stays narrow and cacheable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from rdflib import OWL, RDF, RDFS, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection

from ..errors import SchemaResolutionError
from .builder import parse_flag
from .mapping import MappingError, property_field_path
from .predicates import PredicateHandler

if TYPE_CHECKING:
    from .mapping import MappingBundle

# Every physical-IRI spelling seen in the wild. The FIRST entry is the
# canonical one the analyzers emit by default (``arango-schema-analyzer``
# ``DEFAULT_OWL_PHYSICAL_IRI``); it must lead so the synthesizer (which
# uses ``_PHYS_NAMESPACES[0]``) aligns with real analyzer output. The
# remaining spellings are accepted so ontologies from older mappers still
# resolve.
_PHYS_NAMESPACES = (
    Namespace("http://arangodb.com/schema/physical#"),
    Namespace("https://arango.solutions/phys#"),
    Namespace("https://arango-schema-mapper.example.org/phys#"),
)
_LOCAL_NAME_RE = re.compile(r"[#/]([^#/]+)$")

#: ``phys:mappingStyle`` values a class may carry (PRD §6.1). ``LABELS`` is
#: arango-schema-analyzer's label-array style and ``MULTI_LABEL`` / ``UNION``
#: are arango-query-core's supertype styles; any other value is refused
#: rather than read as a plain collection.
CLASS_MAPPING_STYLES = frozenset({"COLLECTION", "DOCUMENT", "LABEL", "LABELS", "MULTI_LABEL", "RPT", "UNION"})

# Synthetic-IRI namespace used by :meth:`SchemaResolver.from_mapping_bundle`
# when a :class:`~arango_sparql.translate.mapping.MappingBundle` carries no
# inline OWL ontology. The choice of ``urn:`` rather than a resolvable
# ``https:`` IRI is deliberate — the synthesized concepts are not meant to
# be dereferenced, only to give the resolver a stable IRI per entity/
# relationship label.
_SYNTHETIC_CONCEPT_NS = Namespace("urn:arango-sparql:concept#")
# Canonical ``phys:*`` namespace used by the synthesizer. Matches the
# first entry in :data:`_PHYS_NAMESPACES` so the resolver's lookup logic
# (which accepts either spelling) finds the annotations on the first
# probe.
_SYNTHETIC_PHYS_NS = _PHYS_NAMESPACES[0]


def local_name(iri: URIRef | str) -> str:
    """Return the local part of an IRI (after the last ``#`` or ``/``).

    Matches the behavior of the legacy
    ``references/arango-sparql/src/lib/uri-resolver.js`` ``extractPropertyName``
    so unmapped property IRIs degrade to the same physical attribute name.
    """
    text = str(iri)
    match = _LOCAL_NAME_RE.search(text)
    if match:
        return match.group(1)
    return text


@dataclass
class ResolvedClass:
    """An OWL class resolved to its ArangoDB physical collection.

    The four ``*_column`` defaults match the legacy Foxx RPT layout
    (``references/arango-sparql/src/lib/rpt-translator.js`` with
    ``constants.COLLECTIONS.TRIPLES`` columns ``subject_uri`` /
    ``predicate`` / ``object_uri`` / ``object_value``). Customers who
    renamed these columns can override via the corresponding
    ``phys:*Column`` annotations on the OWL class — see PRD §6.2.
    """

    iri: str
    collection: str
    type_field: str | None = None
    type_value: str | None = None
    type_field_is_array: bool | None = None
    """``phys:typeFieldIsArray``: whether ``type_field`` holds an array of
    labels (r2g's LPG ``labels``) rather than one value. ``None`` = unknown —
    the visitor then emits a test that is correct for both shapes (issue #10;
    ``builder.type_predicate``)."""
    style: str | None = None
    """One of :data:`CLASS_MAPPING_STYLES` (PRD §6.1) when the OWL ontology
    declared ``phys:mappingStyle``; ``None`` for legacy mappings that omit
    the explicit style. The visitor treats ``None`` as ``COLLECTION``
    (default PG) — keeps pre-existing ontologies working without
    mass-annotation. ``LABELS`` / ``MULTI_LABEL`` resolve to a label-array
    test (``type_field`` = ``phys:labelsField``); a ``UNION`` supertype has
    no collection of its own (``collection == ""``) and is read through
    :meth:`SchemaResolver.supertype_members`."""
    subject_column: str = "subject_uri"
    predicate_column: str = "predicate"
    object_uri_column: str = "object_uri"
    object_value_column: str = "object_value"
    tenant_field: str | None = None
    """Discriminator column the visitor uses to gate every read of
    this entity with ``FILTER doc.<tenant_field> == @tenant_id``.
    Sourced from ``phys:tenantField`` on the OWL class — see PRD
    §6.5.1. ``None`` means single-tenant deployment for this entity
    (no tenant FILTER emitted)."""
    tenant_entity: str | None = None
    """Name of the tenant root entity (e.g. ``"Org"``) this class
    is scoped under, sourced from ``phys:tenantEntity``. Two
    classes that resolve to *different* ``tenant_entity`` values
    must never be joined in the same BGP — the visitor raises
    ``E_TRANSLATE_CROSS_TENANT_JOIN`` (PRD §6.5.1) rather than
    emit AQL that could broadcast across tenants."""

    shard_family: tuple[str, ...] | None = None
    """Sorted tuple of physical collections this class's
    ``collection`` belongs to (per PRD §6.5.3 ``shardFamilies``), or
    ``None`` when the class is not part of any sharded family.

    When set, the visitor swaps the plain ``FOR doc IN @@coll`` into a
    ``FOR row IN UNION_DISTINCT((FOR a IN @@shard1 RETURN a), ...)``
    fan-out so every shard contributes rows, and the builder emits a
    leading ``WITH @@shard1, @@shard2, …`` so the cluster optimiser
    locks the family at parse time. Sourced from
    ``MappingBundle.physical_mapping.shardFamilies`` — there's no
    OWL ``phys:`` representation today because the analyzer attaches
    the family list to the mapping wire shape, not the per-class
    annotations.

    Stored sorted so two classes that resolve into the same family
    compare ``==`` regardless of declaration order — important for
    the resolver's cache and for the builder's de-duplication of the
    ``WITH`` clause."""


@dataclass
class ResolvedProperty:
    """An OWL property resolved to its ArangoDB physical attribute or
    edge collection (depending on whether it is a datatype or object
    property).

    For ``owl:ObjectProperty`` resolutions, the additional
    ``mapping_style`` / ``type_field`` / ``type_value`` fields tell the
    visitor which AQL traversal pattern to emit (PRD §6.1):

    * ``DEDICATED_COLLECTION`` (the default when ``edge_collection`` is
      set but no discriminator) → ``FOR v, e IN OUTBOUND <s>
      @@edgeColl``.
    * ``GENERIC_WITH_TYPE`` → the same traversal plus
      ``FILTER e.<type_field> == @<type_value>`` so we don't bleed in
      sibling edge types riding the same shared collection.

    For a datatype property stored inside a nested object (issue #72,
    ``phys:attributePath``), ``nested_path`` holds one name per level and
    ``attribute`` is its first segment, the top-level attribute that
    contains the value. Readers use :attr:`field_path`, never
    ``attribute`` alone, so a nested value is never read from the top level.
    """

    iri: str
    attribute: str
    is_object_property: bool = False
    edge_collection: str | None = None
    mapping_style: str | None = None
    type_field: str | None = None
    type_value: str | None = None
    domain_iri: str | None = None
    range_iri: str | None = None
    """The first of :attr:`range_iris`; ``None`` when there is none."""
    range_iris: tuple[str, ...] = ()
    """Every class ``rdfs:range`` names: each value of a repeated
    ``rdfs:range`` (the shape the schema analyzer's ``owl_export`` writes for
    a relationship with several target classes), and each member of an
    ``owl:unionOf`` (issue #122)."""
    symmetric: bool = False
    """``owl:SymmetricProperty``: ``a p b`` holds exactly when ``b p a`` does.

    For an edge-backed property this means each pair is stored once, in
    either direction, and the visitor traverses ``ANY`` (LDBC's ``knows``
    stored once, read as the symmetric ``directKnows``; plan decision D10).
    An edge collection that stores both directions must not be declared
    symmetric: ``ANY`` would return each pair twice."""

    nested_path: tuple[str, ...] | None = None

    reified_edge: bool = False
    """``phys:reifiedEdge "true"``: the relationship's instances are edge
    documents (LDBC plan decision D8). In ``?s p ?k`` the object ``?k`` is the
    edge itself — the node that stands for the relationship and carries its
    data (``?k :creationDate ?d`` reads the edge's attribute) — not the vertex
    at its far end, which an exit predicate (:attr:`edge_target_of`) reaches."""

    edge_target_of: tuple[str, ...] = ()
    """``phys:edgeTargetOf <entry>`` (repeatable): in ``?k p ?o``, ``?k`` is an
    edge of one of these reified relationships and ``?o`` is the vertex at its
    ``_to`` end (LDBC's ``snvoc:hasPerson`` for ``knows`` and ``hasMember``)."""

    @property
    def field_path(self) -> tuple[str, ...]:
        """Where the value is stored: one name per level, top level first."""
        return self.nested_path if self.nested_path is not None else (self.attribute,)


#: The ``iri`` of the class :meth:`SchemaResolver.triple_store_class` returns:
#: it stands for "any subject of the triple store", not an OWL class.
TRIPLE_STORE_IRI = "urn:arango-sparql:triple-store"


@dataclass
class SchemaResolver:
    """Resolve SPARQL IRIs against the loaded OWL ontology.

    The ontology is treated as immutable after load; if the schema can
    change at runtime, build a new ``SchemaResolver`` and swap atomically
    rather than mutating this one.
    """

    ontology: Graph
    default_collection: str = "Document"
    strict_subject_resolution: bool = False
    """When ``True``, a triple-pattern subject that cannot be routed to a
    mapped collection raises :class:`SchemaResolutionError` instead of
    silently falling back to :attr:`default_collection`.

    Off by default so the legacy / W3C-harness behaviour (a real
    ``Document`` catch-all collection) is preserved. The live service
    turns it **on** (see ``service.mapping._resolver_from_request``): a
    customer database has no ``Document`` collection, so the fallback
    emits ``FOR doc IN @@…_Document`` that only fails at execution time
    with an opaque ``ERR 1203`` — exactly the "never emit silently wrong
    AQL" case AGENTS.md hard-rule #5 forbids. Surfacing it as a typed
    translate-time error lets the UI point the user at the real fix (add a
    ``?s a :Class`` type pattern, or map the subject's collection).
    """
    graph_field: str = "_graph"
    """Document attribute that carries the RDF named-graph IRI for
    the SPARQL ``GRAPH <iri> { … }`` / ``GRAPH ?g { … }`` constructs.

    Each document MAY carry ``<graph_field>: <iri>`` (a string) to
    declare that the triple represented by the document lives in
    the named graph ``<iri>``. Documents without this attribute (or
    with the attribute set to ``null``) are considered part of the
    dataset's *default graph*.

    The default field name ``"_graph"`` mirrors the per-document
    encoding chosen in :doc:`/docs/architecture/decisions/0001-named-graphs-per-document`
    (ADR-0001). Deployments that already use that name for something
    else can override here without forking the visitor.

    Layout-agnostic: the same attribute name applies to PG class
    collections, LPG edge collections, and RPT predicate
    collections — the visitor's ``visit_Graph`` consults this
    field on whatever document the underlying FOR loop produces.
    """

    property_path_max_depth: int = 10
    """Maximum repetitions when lowering ``:p+`` / ``:p*`` / ``:p?``; see
    :mod:`arango_sparql.translate.paths`."""

    predicates: Mapping[str, PredicateHandler] = field(default_factory=dict)
    """Caller-registered predicates (magic properties), by IRI: the seam in
    :mod:`arango_sparql.translate.predicates`. An IRI not registered here is
    an ordinary predicate."""

    fan_out_list_values: bool = False
    """Treat a list-valued document attribute as MULTIPLE triples
    (one per element), per RDF semantics for multi-valued predicates.

    The flattened document model stores ``:s :p 1, 2`` as
    ``{p: [1, 2]}`` — a single attribute. With this knob **off**
    (default), ``?s :p ?o`` binds ``?o`` to the whole list: one row,
    which is right for schemas whose arrays are genuinely atomic
    values (tags stored as an array on purpose) and keeps the emitted
    AQL free of per-read fan-out loops. With the knob **on**, every
    datatype attribute read emits
    ``FOR v IN (IS_LIST(attr) ? attr : [attr])`` and constant/join
    comparisons become membership tests — restoring SPARQL's
    one-row-per-value semantics for RDF-loaded data. The W3C
    live-execution harness turns this on because its loader is
    exactly such an RDF flattener (ADR-0002-adjacent; PRD §6.6).

    Scope note: applies to required BGP reads and variable-predicate
    fan-outs. OPTIONAL's conditional-binding path keeps scalar
    semantics for now (a multi-valued OPTIONAL needs the subquery
    emitter) — a list-valued attribute under OPTIONAL still binds
    the list."""

    permissive_class_resolution: bool = False
    """When ``True``, an unknown class IRI degrades to
    :attr:`default_collection` + a ``W_SCHEMA_UNMAPPED_CLASS`` warning,
    mirroring how :meth:`resolve_property` already handles unmapped
    property IRIs.

    Default is ``False`` to preserve the strict pre-existing contract
    that production callers rely on (every queried class IRI MUST be
    declared in the ontology). The W3C DAWG translation-only harness
    flips this on so the visitor can be exercised against the
    arbitrary IRIs the W3C corpus uses (``foaf:Person``,
    ``owl:Restriction``, ad-hoc test classes) without authoring a
    matching ontology per query.

    Semantically defensible: SPARQL is open-world. A query that
    references a class IRI the database doesn't know about should
    return no rows (the open-world correct answer), not raise a
    translation error. The warning surface preserves the diagnostic
    so operators can see what fell back."""

    default_graph_includes_named: bool = True
    """Whether queries outside any ``GRAPH`` wrapper see *all*
    documents (lax, default) or only documents in the default
    graph (strict, SPARQL-conformant).

    SPARQL 1.1 §8.3 leaves this choice to the dataset declaration,
    so both modes are spec-conformant — pick the one that matches
    how your dataset was loaded:

    * ``True`` (lax, default): default-graph queries see every
      document regardless of ``<graph_field>`` value. This is the
      v0.9 default because (1) existing translation goldens do not
      include a graph filter and would all need to change, and
      (2) legacy data loaded before ``<graph_field>`` existed is
      still queryable without migration.
    * ``False`` (strict): default-graph queries emit
      ``FILTER doc.<graph_field> == null`` on every FOR, restricting
      results to documents that lack a named-graph assignment.

    A future slice may flip the default to strict once the
    live-execution harness lands and the goldens can be co-updated
    mechanically; until then this knob is the migration seam.
    """
    shard_families: tuple[tuple[str, ...], ...] = ()
    """Sorted, immutable view of the ``physicalMapping.shardFamilies``
    list from the source :class:`MappingBundle` (PRD §6.5.3).

    Each inner tuple is the sorted member-collection names of one
    shard family. Empty (default) when the deployment is single-shard
    — visitor emits the plain ``FOR doc IN @@coll`` form. When non-
    empty the resolver computes :attr:`_shard_family_by_collection`
    once and uses it on every ``resolve_class`` to attach
    :attr:`ResolvedClass.shard_family`.

    Tuples (rather than lists) so a freshly-constructed resolver is
    cheap to hash / compare in the schema cache layer."""

    uri_indexes: UriIndexes | None = None
    """What the schema analysis records about ``_uri`` and its indexes
    (:func:`analyze_uri_indexes`).

    A union read checks each later member's row for a copy in an earlier
    member by an ``_uri`` lookup (#124, PRD §6.1); when that member has no
    usable index the lookup scans it once per row, so the visitor warns
    (``W_SCHEMA_URI_INDEX_MISSING``, :meth:`UriIndexes.scanned`). ``None``
    when the mapping records no indexes (a hand-written ontology or
    mapping): nothing is warned."""

    _class_cache: dict[str, ResolvedClass] = field(default_factory=dict)
    _property_cache: dict[str, ResolvedProperty] = field(default_factory=dict)
    _attribute_uri_map: dict[str, str] | None = field(default=None)
    """Lazily-built reverse property index for
    :meth:`attribute_uri_map`; ``None`` until first use."""
    _edge_properties: tuple[ResolvedProperty, ...] | None = field(default=None)
    """Lazily-built object-property mappings backed by edge collections."""
    _triple_store_class: ResolvedClass | None = field(default=None)
    """Cached result of :meth:`triple_store_class`."""
    _triple_store_checked: bool = field(default=False)
    _tenant_scoped_classes: tuple[ResolvedClass, ...] | None = field(default=None)
    """Cached result of :meth:`tenant_scoped_classes`."""
    _nested_properties: tuple[str, ...] | None = field(default=None)
    """Lazily-built IRIs of datatype properties stored in a nested object."""
    _shard_family_by_collection: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Reverse index: physical collection name → the shard family
    tuple it belongs to. Built once in :meth:`__post_init__` so
    every ``resolve_class`` lookup is O(1)."""
    warnings: list[dict[str, Any]] = field(default_factory=list)
    """Schema-mapping advisories accumulated during resolution.

    Each entry is a ``{"code", "message", ...}`` dict. The
    ``W_SCHEMA_*`` code prefix marks the entry as a schema-mapping
    warning so :class:`arango_sparql.api.TranslateResult` can split it
    out into its own ``schema_warnings`` projection for the UI to
    render in a dedicated sidebar.

    De-duplicated by ``(code, IRI)`` so a query that references the same
    unmapped predicate ten times emits one advisory rather than ten.
    """
    _warned_keys: set[tuple[str, str]] = field(default_factory=set)
    _entry_warnings: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    """The advisories each cached resolution raised, by ``(kind, key)``, so a
    cache hit reports them again (:meth:`_replay`): a translation reports
    the advisories of every IRI it resolves, not only of the first one to
    resolve it (#157)."""
    _captures: list[list[dict[str, Any]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Build the collection → family reverse index once. A
        # collection that appears in two families would be ambiguous
        # (which fan-out applies?), so we reject the duplicate at
        # construction time with a typed error rather than at
        # translate time when the message would be far less actionable.
        if not self.shard_families:
            return
        index: dict[str, tuple[str, ...]] = {}
        for family in self.shard_families:
            for coll in family:
                if coll in index and index[coll] != family:
                    raise SchemaResolutionError(
                        f"collection {coll!r} appears in two distinct "
                        f"shardFamilies ({index[coll]!r} and {family!r}); "
                        f"a physical collection must belong to at most one "
                        f"family (PRD §6.5.3)"
                    )
                index[coll] = family
        self._shard_family_by_collection = index

    @classmethod
    def from_turtle(
        cls,
        ttl: str,
        *,
        default_collection: str = "Document",
        graph_field: str = "_graph",
        default_graph_includes_named: bool = True,
        permissive_class_resolution: bool = False,
        fan_out_list_values: bool = False,
        predicates: Mapping[str, PredicateHandler] | None = None,
    ) -> SchemaResolver:
        """Convenience constructor — parse *ttl* into a fresh ``rdflib.Graph``.

        ``graph_field`` and ``default_graph_includes_named`` are
        forwarded to the resolver verbatim; see the class-level
        docstring for the named-graph storage model semantics.

        ``permissive_class_resolution`` — when ``True``, unknown class
        IRIs degrade to ``default_collection`` rather than raising
        ``SchemaResolutionError``; see the class-level field doc.
        """
        graph = Graph()
        if ttl:
            graph.parse(data=ttl, format="turtle")
        return cls(
            ontology=graph,
            default_collection=default_collection,
            graph_field=graph_field,
            default_graph_includes_named=default_graph_includes_named,
            permissive_class_resolution=permissive_class_resolution,
            fan_out_list_values=fan_out_list_values,
            predicates=dict(predicates or {}),
        )

    @classmethod
    def from_mapping_bundle(
        cls,
        bundle: MappingBundle,
        *,
        default_collection: str = "Document",
        permissive_class_resolution: bool = False,
        predicates: Mapping[str, PredicateHandler] | None = None,
    ) -> SchemaResolver:
        """Build a resolver from a :class:`~arango_sparql.translate.mapping.MappingBundle`.

        Two paths, picked at runtime:

        1. **Inline OWL** — when ``bundle.owl_turtle`` is set (the typical
           analyzer output, see PRD §6.3.1), we parse it directly. The
           analyzer is responsible for embedding ``phys:*`` annotations on
           every ``owl:Class`` and ``owl:ObjectProperty`` so the resolver
           can dereference them without further work.

        2. **Synthetic** — when no inline OWL is present (heuristic or
           hand-authored mappings; see PRD §6.3.2), we synthesize a
           minimal ``rdflib.Graph`` from ``bundle.physical_mapping``.
           Each entity becomes one ``owl:Class`` with a
           ``urn:arango-sparql:concept#<Label>`` IRI; each relationship
           becomes one ``owl:ObjectProperty``. Physical annotations
           (``collectionName``, ``edgeCollectionName``, ``typeField`` /
           ``typeValue``, RPT column overrides) are attached as
           ``phys:*`` literals so the existing ``resolve_class`` /
           ``resolve_property`` paths work unchanged.

        Callers that need IRIs in their own namespace (e.g. SPARQL
        queries using ``http://customer.example/onto#``) should supply
        an OWL ontology via ``bundle.owl_turtle`` rather than rely on
        the synthetic ``urn:`` namespace.
        """

        shard_families = _project_shard_families(bundle.physical_mapping)
        uri_indexes = analyze_uri_indexes(bundle)
        if bundle.owl_turtle:
            graph = Graph()
            graph.parse(data=bundle.owl_turtle, format="turtle")
            return cls(
                ontology=graph,
                default_collection=default_collection,
                shard_families=shard_families,
                permissive_class_resolution=permissive_class_resolution,
                predicates=dict(predicates or {}),
                uri_indexes=uri_indexes,
            )
        graph = _synthesize_graph_from_bundle(bundle)
        return cls(
            ontology=graph,
            default_collection=default_collection,
            shard_families=shard_families,
            permissive_class_resolution=permissive_class_resolution,
            predicates=dict(predicates or {}),
            uri_indexes=uri_indexes,
        )

    # ------------------------------------------------------------------
    # Class resolution
    # ------------------------------------------------------------------
    def supertype_members(self, iri: URIRef | str) -> tuple[ResolvedClass, ...]:
        """The concrete classes a ``UNION`` supertype stands for.

        The same rule as arango-query-core's ``MappingResolver.supertype_members``
        (its OWL form, ``owl_turtle``, writes the members as ``rdfs:subClassOf``
        subclasses): members are the declared subclasses, a member that is
        itself a supertype is flattened, a cycle or a supertype with no members
        is a mapping error. Ordered by IRI (an RDF graph has no declaration
        order). ``tests/translate/test_supertype_classes.py`` checks it against
        arango-query-core's own answer whenever that library is installed.
        """
        out: list[ResolvedClass] = []

        def visit(cls: URIRef, path: tuple[str, ...]) -> None:
            if str(cls) in path:
                raise SchemaResolutionError(f"supertype cycle: {' -> '.join((*path, str(cls)))}")
            resolved = self.resolve_class(cls)
            if resolved.style != "UNION":
                if not path:
                    raise SchemaResolutionError(f"class {str(cls)!r} is not a UNION supertype")
                if resolved not in out:
                    out.append(resolved)
                return
            members = sorted(
                (m for m in self.ontology.subjects(RDFS.subClassOf, cls) if isinstance(m, URIRef)), key=str
            )
            if not members:
                raise SchemaResolutionError(
                    f"UNION supertype {str(cls)!r} has no members: declare them with rdfs:subClassOf"
                )
            for member in members:
                visit(member, (*path, str(cls)))

        visit(URIRef(str(iri)), ())
        return tuple(out)

    def resolve_class(self, iri: URIRef | str) -> ResolvedClass:
        key = str(iri)
        cached = self._class_cache.get(key)
        if cached is not None:
            self._replay("class", key)
            return cached
        with self._capture("class", key):
            return self._resolve_class(iri)

    def _resolve_class(self, iri: URIRef | str) -> ResolvedClass:
        key = str(iri)
        ref = URIRef(key)
        if (ref, RDF.type, OWL.Class) not in self.ontology:
            if not self.permissive_class_resolution:
                raise SchemaResolutionError(f"class IRI {key!r} is not declared owl:Class in the ontology")
            # Permissive mode (opt-in): degrade to the default
            # document collection — same shape that
            # :meth:`resolve_property` already uses for unmapped
            # property IRIs. The shard-family reverse index is
            # consulted so a permissive fallback still picks up the
            # cluster fan-out if the default collection is part of a
            # sharded family.
            fallback_collection = self.default_collection
            self._warn_schema(
                code="W_SCHEMA_UNMAPPED_CLASS",
                message=(
                    f"class IRI {key!r} is not declared owl:Class in the "
                    f"ontology; falling back to default collection "
                    f"{fallback_collection!r} (permissive mode)"
                ),
                iri=key,
                fallback_collection=fallback_collection,
            )
            shard_family = self._shard_family_by_collection.get(fallback_collection)
            resolved = ResolvedClass(
                iri=key,
                collection=fallback_collection,
                shard_family=shard_family,
            )
            self._class_cache[key] = resolved
            return resolved
        style = self._physical_string(ref, "mappingStyle")
        if style is not None and style not in CLASS_MAPPING_STYLES:
            # An unknown style used to read as a plain collection, which is
            # how arango-query-core's supertypes and the analyzer's LABELS
            # classes returned wrong rows without an error.
            raise SchemaResolutionError(
                f"class {key!r} has phys:mappingStyle {style!r}, which this translator does not "
                f"support (known: {', '.join(sorted(CLASS_MAPPING_STYLES))})"
            )
        if style == "UNION":
            # A supertype has no collection of its own: the visitor reads its
            # members. The empty name makes any reader that does not know
            # supertypes fail when it binds the collection, never scan a
            # collection named after the class.
            resolved = ResolvedClass(
                iri=key,
                collection="",
                style=style,
                tenant_field=self._physical_string(ref, "tenantField"),
                tenant_entity=self._physical_string(ref, "tenantEntity"),
            )
            self._class_cache[key] = resolved
            return resolved
        physical = self._physical_string(ref, "collectionName")
        if physical is None:
            # Class is declared in the ontology but the mapper did not
            # attach a ``phys:collectionName`` annotation. We degrade to
            # the IRI's local name (matching the legacy translator) but
            # surface a schema-warning so the operator can fix the
            # mapping rather than chase a phantom collection name later.
            collection = local_name(ref)
            self._warn_schema(
                code="W_SCHEMA_DEFAULT_COLLECTION",
                message=(
                    f"class {key!r} has no phys:collectionName annotation; "
                    f"falling back to local-name collection {collection!r}"
                ),
                iri=key,
                class_iri=key,
                default_collection=collection,
            )
        else:
            collection = physical
        type_field = self._physical_string(ref, "typeField")
        type_value = self._physical_string(ref, "typeValue")
        type_field_is_array = parse_flag(self._physical_string(ref, "typeFieldIsArray"))
        if style in ("LABELS", "MULTI_LABEL"):
            # The analyzer's LABELS and arango-query-core's MULTI_LABEL share
            # one field set: ``labelValue IN doc[labelsField]``.
            labels_field = self._physical_string(ref, "labelsField")
            if labels_field is None:
                raise SchemaResolutionError(f"{style} class {key!r} has no phys:labelsField")
            type_field = labels_field
            type_value = self._physical_string(ref, "labelValue") or local_name(ref)
            type_field_is_array = True
        # RPT classes default the resolver-visible "collection" to the
        # triples table — that's where the engine reads rows. Inline
        # OWL ontologies that explicitly declare ``phys:mappingStyle
        # "RPT"`` but omit ``phys:collectionName`` get the same
        # treatment so a hand-authored ontology composes the same way
        # as the synthesised one.
        if style == "RPT" and physical is None:
            triples_collection = self._physical_string(ref, "triplesCollection")
            collection = triples_collection or "_triples"
        # Per-column overrides (RPT only). The default values match
        # the legacy Foxx fixture columns; a customer who renamed any
        # of these supplies the override on the OWL class. Tenant
        # annotations apply to every style — the visitor consumes
        # them to gate each FOR with a tenant FILTER.
        kwargs: dict[str, Any] = {}
        for attr_name, phys_local in (
            ("subject_column", "subjectColumn"),
            ("predicate_column", "predicateColumn"),
            ("object_uri_column", "objectUriColumn"),
            ("object_value_column", "objectValueColumn"),
        ):
            value = self._physical_string(ref, phys_local)
            if value is not None:
                kwargs[attr_name] = value
        tenant_field = self._physical_string(ref, "tenantField")
        tenant_entity = self._physical_string(ref, "tenantEntity")
        shard_family = self._shard_family_by_collection.get(collection)
        resolved = ResolvedClass(
            iri=key,
            collection=collection,
            type_field=type_field,
            type_value=type_value,
            type_field_is_array=type_field_is_array,
            style=style,
            tenant_field=tenant_field,
            tenant_entity=tenant_entity,
            shard_family=shard_family,
            **kwargs,
        )
        self._class_cache[key] = resolved
        return resolved

    # ------------------------------------------------------------------
    # Property resolution
    # ------------------------------------------------------------------
    def resolve_property(self, iri: URIRef | str) -> ResolvedProperty:
        key = str(iri)
        cached = self._property_cache.get(key)
        if cached is not None:
            self._replay("property", key)
            return cached
        with self._capture("property", key):
            return self._resolve_property(iri)

    def _resolve_property(self, iri: URIRef | str) -> ResolvedProperty:
        key = str(iri)
        ref = URIRef(key)
        is_object = (ref, RDF.type, OWL.ObjectProperty) in self.ontology
        is_datatype = (ref, RDF.type, OWL.DatatypeProperty) in self.ontology
        if not (is_object or is_datatype):
            # Unmapped property — degrade to local-name attribute access.
            # This matches the legacy translator's behavior for any
            # predicate IRI not present in the ontology and keeps simple
            # SPARQL queries working against a freshly-mapped schema
            # before a full ontology has been authored. Surface a
            # schema-warning so the operator (and the UI's schema-
            # warnings sidebar) can see the silently-degraded resolution.
            fallback_attribute = local_name(ref)
            self._warn_schema(
                code="W_SCHEMA_UNMAPPED_IRI",
                message=(
                    f"property IRI {key!r} is not declared in the ontology; "
                    f"falling back to local-name attribute {fallback_attribute!r}"
                ),
                iri=key,
                fallback=fallback_attribute,
            )
            resolved = ResolvedProperty(iri=key, attribute=fallback_attribute)
            self._property_cache[key] = resolved
            return resolved

        edge_collection = self._physical_string(ref, "edgeCollectionName") if is_object else None
        # ``phys:mappingStyle`` and ``phys:typeField`` / ``phys:typeValue``
        # only matter for object properties — they pick the traversal
        # pattern (DEDICATED_COLLECTION vs GENERIC_WITH_TYPE per PRD §6.1)
        # and the typed-edge discriminator. We surface them on
        # datatype-property resolutions too so a future RPT/LABEL
        # property can read them without a separate code path, but for
        # now they're only consumed by the visitor's edge-traversal
        # branch.
        mapping_style = self._physical_string(ref, "mappingStyle") if is_object else None
        type_field = self._physical_string(ref, "typeField") if is_object else None
        type_value = self._physical_string(ref, "typeValue") if is_object else None
        # If the ontology declares an object property without an
        # explicit ``phys:mappingStyle`` but does provide a
        # ``phys:typeField`` / ``phys:typeValue`` discriminator, treat
        # it as ``GENERIC_WITH_TYPE``. Otherwise default to
        # ``DEDICATED_COLLECTION`` (one edge collection per relationship
        # type — the typical PG-style mapping). This mirrors the
        # algorithmic detector's behaviour in
        # :mod:`arango_sparql.schema.detect` so a hand-authored OWL that
        # omits ``phys:mappingStyle`` still routes to the correct
        # traversal pattern.
        if is_object and mapping_style is None:
            if type_field and type_value:
                mapping_style = "GENERIC_WITH_TYPE"
            elif edge_collection:
                mapping_style = "DEDICATED_COLLECTION"
        domain_iri = self._first_object(ref, RDFS.domain)
        range_iris = self.class_list(ref, RDFS.range)
        # ``phys:attributeName`` maps an OWL-style conceptual property name to
        # its stored document field (CDF CC-12: ``accountId`` → ``account_id``).
        # Absent the annotation, the local name doubles as the attribute —
        # the long-standing behavior for identity-named mappings.
        attribute = self._physical_string(ref, "attributeName") or local_name(ref)
        # ``phys:attributePath`` (issue #72) is a JSON array naming a value
        # inside a nested object, one segment per level. It takes the place
        # of ``phys:attributeName``, whose dots are part of one literal name.
        path = self._attribute_path(ref, key)
        nested_path = path if path is not None and len(path) > 1 else None
        if path is not None:
            attribute = path[0]
        resolved = ResolvedProperty(
            iri=key,
            attribute=attribute,
            is_object_property=is_object,
            edge_collection=edge_collection,
            mapping_style=mapping_style,
            type_field=type_field,
            type_value=type_value,
            domain_iri=domain_iri,
            range_iri=range_iris[0] if range_iris else None,
            range_iris=range_iris,
            symmetric=is_object and (ref, RDF.type, OWL.SymmetricProperty) in self.ontology,
            nested_path=nested_path,
            reified_edge=is_object and parse_flag(self._physical_string(ref, "reifiedEdge")) is True,
            edge_target_of=tuple(self._physical_iris(ref, "edgeTargetOf")) if is_object else (),
        )
        if resolved.reified_edge and resolved.edge_collection is None:
            raise SchemaResolutionError(
                f"object property {key!r} is a reified edge (phys:reifiedEdge) but has no "
                f"phys:edgeCollectionName: its instances must be edge documents"
            )
        self._property_cache[key] = resolved
        return resolved

    def _attribute_path(self, ref: URIRef, key: str) -> tuple[str, ...] | None:
        """Read ``phys:attributePath`` on *ref*: ``None`` when absent, else
        its segments (one segment is a plain attribute, like attributeName).

        A malformed annotation raises rather than falling back to the local
        name, which would read a field that does not exist and answer empty.
        """
        raw = self._physical_string(ref, "attributePath")
        if raw is None:
            return None
        if self._physical_string(ref, "attributeName") is not None:
            raise SchemaResolutionError(
                f"property {key!r} declares both phys:attributeName and phys:attributePath; "
                f"declare one (attributeName is one literal name, attributePath a nested value)"
            )
        try:
            segments = json.loads(raw)
            path = property_field_path(key, {"path": segments})
        except (ValueError, MappingError) as exc:
            raise SchemaResolutionError(
                f"property {key!r} has a malformed phys:attributePath {raw!r}: expected a "
                f'JSON array of field names such as ["address", "city"] ({exc})'
            ) from exc
        assert path is not None  # property_field_path returns a tuple for a non-None path
        return path

    def nested_properties(self) -> tuple[str, ...]:
        """Return the IRIs of datatype properties stored in a nested object.

        Emitters that enumerate a document's top-level attributes
        (``ATTRIBUTES()``: variable predicates, negated property paths) cannot
        see these values, so they refuse while this is non-empty rather than
        return an answer that silently leaves them out (issue #72).
        """
        if self._nested_properties is not None:
            return self._nested_properties
        nested = [
            str(subject)
            for subject in sorted(set(self.ontology.subjects(RDF.type, OWL.DatatypeProperty)), key=str)
            if isinstance(subject, URIRef) and self.resolve_property(subject).nested_path is not None
        ]
        self._nested_properties = tuple(nested)
        return self._nested_properties

    # ------------------------------------------------------------------
    # Reverse property index (attribute name → predicate IRI)
    # ------------------------------------------------------------------
    def attribute_uri_map(self) -> dict[str, str]:
        """Return ``physical attribute name → predicate IRI`` for every
        declared ``owl:DatatypeProperty``.

        This is the inverse of :meth:`resolve_property`'s
        IRI → attribute direction, and exists for the variable-predicate
        emitter (PRD §6.6): an ``ATTRIBUTES()`` fan-out can only bind
        ``?p`` to a spec-correct IRI when the ontology tells us which
        IRI a document attribute came from. Only datatype properties
        participate — object properties live in edge collections, not
        document attributes, so they can never surface from an
        ``ATTRIBUTES()`` iteration.

        Keys are sorted at build time so the bound map is deterministic
        across runs (golden stability). When two declared properties
        share a local name the lexically-smallest IRI wins and a
        ``W_SCHEMA_AMBIGUOUS_ATTRIBUTE`` advisory is recorded — the
        flattened document model genuinely cannot distinguish the two.
        Empty when the ontology declares no datatype properties, which
        callers treat as "mapping unavailable" (the emitter falls back
        to the attribute-name carve-out rather than filtering every
        row out).
        """
        if self._attribute_uri_map is not None:
            self._replay("attribute_uri_map", "")
            return self._attribute_uri_map
        with self._capture("attribute_uri_map", ""):
            self._attribute_uri_map = self._build_attribute_uri_map()
        return self._attribute_uri_map

    def _build_attribute_uri_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        # ``key=str`` both satisfies the rdflib ``Node`` sort typing and
        # makes the collision rule explicit: lexically-smallest IRI wins.
        for subject in sorted(set(self.ontology.subjects(RDF.type, OWL.DatatypeProperty)), key=str):
            if not isinstance(subject, URIRef):
                continue
            prop = self.resolve_property(subject)
            if prop.nested_path is not None:
                # Its top-level attribute holds the enclosing object, not this
                # value; keying it here would bind ?p to the wrong predicate.
                continue
            attribute = prop.attribute
            existing = mapping.get(attribute)
            if existing is not None:
                self._warn_schema(
                    code="W_SCHEMA_AMBIGUOUS_ATTRIBUTE",
                    message=(
                        f"datatype properties {existing!r} and {str(subject)!r} "
                        f"share the physical attribute name {attribute!r}; "
                        f"variable-predicate results bind ?p to {existing!r}"
                    ),
                    iri=str(subject),
                    attribute=attribute,
                )
                continue
            mapping[attribute] = str(subject)
        return mapping

    def domain_classes(self, property_iri: str) -> tuple[str, ...]:
        """The class IRIs ``rdfs:domain`` gives *property_iri*: every value of a
        repeated ``rdfs:domain`` and every member of an ``owl:unionOf``
        (:meth:`class_list`). Empty when no domain is declared (or it is
        something else, such as an intersection).

        The visitor routes a subject that no type pattern names by it: in
        RDF a domain means every subject of the property *is* of that class.
        """
        return self.class_list(URIRef(property_iri), RDFS.domain)

    def class_list(self, subject: URIRef, predicate: URIRef) -> tuple[str, ...]:
        """Every class *predicate* (``rdfs:domain`` / ``rdfs:range``) names on
        *subject*, in IRI order: each value of a repeated triple and each
        member of an ``owl:unionOf``; anything else (an intersection) is
        left out.

        A repeated ``rdfs:domain`` or ``rdfs:range`` is what mapping
        producers write for a property shared by several classes
        (arango-query-core's ``mapping_to_turtle``, the schema analyzer's
        ``owl_export``, :func:`_synthesize_graph_from_bundle`), and it means
        where the subjects or objects are stored: any of them.
        ``ontology.value()`` returned one of them, arbitrarily, and routing
        or a backward walk missed the others (issue #122).
        """
        out: set[str] = set()
        for value in self.ontology.objects(subject, predicate):
            if isinstance(value, URIRef):
                out.add(str(value))
                continue
            members = self.ontology.value(value, OWL.unionOf)
            if members is not None:
                out.update(str(m) for m in Collection(self.ontology, members) if isinstance(m, URIRef))
        return tuple(sorted(out))

    def declares_class(self, iri: str) -> bool:
        """Whether *iri* is something a domain or range can name: an
        ``owl:Class``, or a reified relationship (plan D8)."""
        return (URIRef(iri), RDF.type, OWL.Class) in self.ontology or self.is_reified_entry(iri)

    def undeclared_class_fallback(self, iri: str, where: str) -> ResolvedClass:
        """The default collection, standing for a class the ontology does not
        declare but a domain names (*where*), with a warning (#126).

        arango-query-core writes ``rdfs:domain :Any`` for a relationship with
        no ``fromEntity``. Its subjects are stored where any subject no class
        places is, the default collection, so a domain naming it reads that
        beside its declared classes, as 0.5.0 read it. A type pattern on the
        class still refuses it (:meth:`resolve_class`); this is not cached.
        """
        self._warn_schema(
            code="W_SCHEMA_UNDECLARED_DOMAIN_CLASS",
            message=(
                f"{where} names {iri!r}, which is not declared owl:Class; its subjects are "
                f"read from the default collection {self.default_collection!r}"
            ),
            iri=iri,
            fallback_collection=self.default_collection,
        )
        return ResolvedClass(
            iri=iri,
            collection=self.default_collection,
            shard_family=self._shard_family_by_collection.get(self.default_collection),
        )

    def warn_undeclared_range(self, iri: str, where: str) -> None:
        """Warn that a range names an undeclared class, so it is not used
        (objects are not looked up through it; #126)."""
        self._warn_schema(
            code="W_SCHEMA_UNDECLARED_RANGE_CLASS",
            message=(
                f"{where} names {iri!r}, which is not declared owl:Class; the range is not "
                f"used to look objects up"
            ),
            iri=iri,
        )

    def warn_unindexed_uri(self, collection: str) -> None:
        """Warn that a union read looks copies up by ``_uri`` in *collection*,
        which has no persistent index on it, so each row scans it (#124)."""
        self._warn_schema(
            code="W_SCHEMA_URI_INDEX_MISSING",
            message=(
                f"collection {collection!r} has no persistent index on _uri; a union read looks "
                f"up each row's copies there by _uri, which scans the whole collection once per "
                f"row. Add a persistent index on _uri to {collection!r}, then refresh the schema "
                f"analysis (/schema/introspect?force=true), which is cached"
            ),
            iri=collection,
            collection=collection,
        )

    def triple_store_class(self) -> ResolvedClass | None:
        """The triples table, when the whole mapping is one triple store.

        When every ``owl:Class`` in the ontology is ``RPT``-style on the
        same triples collection with the same columns, the database is a
        pure triple store: any subject's triples can only be in that table.
        The visitor then reads a subject that no type pattern routed (a
        constant IRI, or a variable bound only to a value) from the triples
        table instead of refusing it, which is how SPARQL over a triple
        store normally reads (``?m :hasCreator ?p`` with no ``?m a :C``).

        Returns ``None`` for any other mapping, including a hybrid one that
        mixes RPT with PG or LPG classes: there an untyped subject could
        live in several places, so it stays unroutable (and, under
        ``strict_subject_resolution``, raises).
        """
        if self._triple_store_checked:
            return self._triple_store_class
        self._triple_store_checked = True
        classes = sorted(
            (c for c in self.ontology.subjects(RDF.type, OWL.Class) if isinstance(c, URIRef)), key=str
        )
        resolved = [self.resolve_class(c) for c in classes]
        if not resolved or any(r.style != "RPT" for r in resolved):
            return None

        def layout(r: ResolvedClass) -> tuple[str, ...]:
            return (
                r.collection,
                r.subject_column,
                r.predicate_column,
                r.object_uri_column,
                r.object_value_column,
                str(r.tenant_field),
            )

        if len({layout(r) for r in resolved}) != 1:
            return None
        self._triple_store_class = replace(resolved[0], iri=TRIPLE_STORE_IRI)
        return self._triple_store_class

    def tenant_scoped_classes(self, *, rpt: bool = False) -> tuple[ResolvedClass, ...]:
        """The classes that declare ``phys:tenantField``: stored as documents,
        or with *rpt* the triples-table (RPT) ones.

        A document a walk reaches (an edge's far vertex, the source of a
        backward walk) is not opened by its class, so nothing applied that
        class's tenant FILTER; the visitor tests it against every document
        class here (issue #118), and refuses reading a triples table that an
        RPT class here uses. A class that does not resolve still counts, from
        its ``phys:collectionName``, so its documents are not left unguarded.
        Resolving the classes reports no advisory and caches nothing, so a
        class the query names later still gets its own.
        """
        if self._tenant_scoped_classes is None:
            found: list[ResolvedClass] = []
            warnings, warned, cache = list(self.warnings), set(self._warned_keys), dict(self._class_cache)
            try:
                for cls in sorted(self.ontology.subjects(RDF.type, OWL.Class), key=str):
                    if not isinstance(cls, URIRef):
                        continue
                    tenant_field = self._physical_string(cls, "tenantField")
                    if tenant_field is None:
                        continue
                    try:
                        resolved = self.resolve_class(cls)
                    except SchemaResolutionError:
                        collection = self._physical_string(cls, "collectionName")
                        if not collection:
                            continue
                        resolved = ResolvedClass(
                            iri=str(cls),
                            collection=collection,
                            tenant_field=tenant_field,
                            tenant_entity=self._physical_string(cls, "tenantEntity"),
                        )
                    if resolved.tenant_field and resolved.style != "UNION" and resolved.collection:
                        found.append(resolved)
            finally:
                self.warnings[:] = warnings
                self._warned_keys.clear()
                self._warned_keys.update(warned)
                self._class_cache.clear()
                self._class_cache.update(cache)
            self._tenant_scoped_classes = tuple(found)
        return tuple(c for c in self._tenant_scoped_classes if (c.style == "RPT") == rpt)

    def edge_properties(self) -> tuple[ResolvedProperty, ...]:
        """Return edge-backed object properties in deterministic IRI order.

        Variable-predicate scans use this catalogue to merge document
        attributes with traversable object-property triples. Object
        properties without ``phys:edgeCollectionName`` are excluded because
        they have no executable edge source in the document-edge model.
        """
        if self._edge_properties is not None:
            return self._edge_properties
        resolved: list[ResolvedProperty] = []
        subjects = sorted(
            set(self.ontology.subjects(RDF.type, OWL.ObjectProperty)),
            key=str,
        )
        for subject in subjects:
            if not isinstance(subject, URIRef):
                continue
            prop = self.resolve_property(subject)
            if prop.edge_collection:
                resolved.append(prop)
        self._edge_properties = tuple(resolved)
        return self._edge_properties

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
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

    def _physical_string(self, subject: URIRef, predicate_local: str) -> str | None:
        """Return the literal value of ``phys:<predicate_local>`` on
        ``subject``, regardless of which physical-IRI spelling the
        mapper used."""
        for ns in _PHYS_NAMESPACES:
            value = self.ontology.value(subject=subject, predicate=ns[predicate_local])
            if value is not None:
                return str(value)
        return None

    def is_reified_entry(self, iri: URIRef | str) -> bool:
        """Whether *iri* is an object property declared ``phys:reifiedEdge``:
        its object is a relationship node stored as an edge (plan D8). Reads
        the annotation only, so it resolves (and warns about) nothing."""
        return parse_flag(self._physical_string(URIRef(str(iri)), "reifiedEdge")) is True

    def is_edge_exit(self, iri: URIRef | str) -> bool:
        """Whether *iri* declares ``phys:edgeTargetOf``: an exit predicate of
        reified relationships (plan D8). Reads the annotation only."""
        return bool(self._physical_iris(URIRef(str(iri)), "edgeTargetOf"))

    def relationship_edge_class(self, iri: URIRef | str) -> ResolvedClass:
        """The edge documents of a reified relationship, as a class (plan D8).

        A datatype property's ``rdfs:domain`` may name a reified relationship
        beside classes (arango-query-core's convention: the relationship is
        the domain of its own data), because its nodes carry that data too
        (LDBC ``creationDate`` on a ``knows`` node). Routing a subject by
        such a domain reads that relationship's edge collection, with its
        edge-type test in a shared collection, as one more member.
        """
        prop = self.resolve_property(iri)
        if not prop.reified_edge or prop.edge_collection is None:
            raise SchemaResolutionError(f"{str(iri)!r} is not a reified relationship (phys:reifiedEdge)")
        typed = prop.mapping_style == "GENERIC_WITH_TYPE" and prop.type_field and prop.type_value
        return ResolvedClass(
            iri=prop.iri,
            collection=prop.edge_collection,
            type_field=prop.type_field if typed else None,
            type_value=prop.type_value if typed else None,
            type_field_is_array=False if typed else None,
            style="EDGE",
        )

    def _physical_iris(self, subject: URIRef, predicate_local: str) -> list[str]:
        """Every IRI value of ``phys:<predicate_local>`` on ``subject``, in IRI
        order, under any physical-IRI spelling."""
        found = {
            str(value)
            for ns in _PHYS_NAMESPACES
            for value in self.ontology.objects(subject, ns[predicate_local])
            if isinstance(value, URIRef)
        }
        return sorted(found)

    def _first_object(self, subject: URIRef, predicate: URIRef) -> str | None:
        value = self.ontology.value(subject=subject, predicate=predicate)
        return str(value) if value is not None else None


# ---------------------------------------------------------------------------
# MappingBundle → synthetic rdflib.Graph
# ---------------------------------------------------------------------------


# Physical-annotation predicates we project from MappingBundle entity- and
# relationship-spec dicts into the synthesized graph. The mapping is
# intentionally explicit (not a generic `for k, v in spec.items()`) so a
# future bundle field with the same camelCase shape but a different
# semantic does not accidentally leak into the ontology. Add a row here
# when extending the bundle wire shape.
_BUNDLE_ENTITY_ANNOTATIONS: tuple[tuple[str, str], ...] = (
    ("typeField", "typeField"),
    ("typeValue", "typeValue"),
    ("typeFieldIsArray", "typeFieldIsArray"),
    ("triplesCollection", "triplesCollection"),
    ("subjectColumn", "subjectColumn"),
    ("predicateColumn", "predicateColumn"),
    ("objectUriColumn", "objectUriColumn"),
    ("objectValueColumn", "objectValueColumn"),
    ("tenantField", "tenantField"),
    ("tenantEntity", "tenantEntity"),
    # ``LABELS`` / ``MULTI_LABEL``: ``labelValue IN doc[labelsField]`` (#94, #126).
    ("labelsField", "labelsField"),
    ("labelValue", "labelValue"),
)

_BUNDLE_RELATIONSHIP_ANNOTATIONS: tuple[tuple[str, str], ...] = (
    ("typeField", "typeField"),
    ("typeValue", "typeValue"),
    ("triplesCollection", "triplesCollection"),
)


#: Index types an equality FILTER on their first field is served by. A sparse
#: one serves it too: the copy check also filters ``_uri != null``.
_EQUALITY_INDEX_TYPES = frozenset({"persistent", "hash", "skiplist"})


@dataclass(frozen=True)
class UriIndexes:
    """What a schema analysis records about ``_uri`` and its indexes, by
    collection (:attr:`SchemaResolver.uri_indexes`, :func:`analyze_uri_indexes`).

    ``collections`` are the collections the analysis describes; ``carrying``
    those whose documents hold ``_uri``; ``indexed`` those with an equality
    index whose first field is ``_uri``."""

    collections: frozenset[str] = frozenset()
    carrying: frozenset[str] = frozenset()
    indexed: frozenset[str] = frozenset()

    def scanned(self, earlier: str, later: Iterable[str]) -> bool:
        """Whether the copy check that looks in *earlier* for the rows of the
        *later* members scans it: the analysis knows *earlier*, finds no
        usable index on it, and a later member's documents carry ``_uri``
        (only such a row starts the lookup). A collection the analysis does
        not describe is not judged."""
        return (
            earlier in self.collections
            and earlier not in self.indexed
            and any(c in self.carrying for c in later)
        )


def _records_indexes(bundle: MappingBundle) -> bool:
    """Whether *bundle* comes from a schema analysis of the database, which
    lists every non-primary index (so a collection without ``indexes`` has
    none): the service's own analysis, or a CSI the schema analyzer wrote in
    reverse. A hand-written or forward-produced mapping records no indexes."""
    if bundle.source is not None and bundle.source.kind == "analyzer":
        return True
    provenance = bundle.metadata.get("csiProvenance") if isinstance(bundle.metadata, dict) else None
    return (
        isinstance(provenance, dict)
        and provenance.get("producer") == "arango-schema-analyzer"
        and provenance.get("direction") == "reverse"
    )


def analyze_uri_indexes(bundle: MappingBundle | None) -> UriIndexes | None:
    """What *bundle*'s physical mapping records about ``_uri`` and its
    indexes, or ``None`` when it records no indexes (:func:`_records_indexes`).

    A collection carries ``_uri`` when an entity mapped to it maps a property
    to the ``_uri`` field (the schema analyzer lists every observed field).
    It is indexed when an entity on it lists an index of an equality type
    whose first field is ``_uri``. The analyzer writes ``indexes`` only for a
    collection that has a non-primary index, so an entity without the key
    has none. Malformed entries are skipped, as :func:`_project_shard_families`
    skips them.
    """
    if bundle is None or not _records_indexes(bundle):
        return None
    entities = bundle.physical_mapping.get("entities") if bundle.physical_mapping else None
    if not isinstance(entities, dict):
        return UriIndexes()
    collections: set[str] = set()
    carrying: set[str] = set()
    indexed: set[str] = set()
    for entity in entities.values():
        if not isinstance(entity, dict):
            continue
        collection = entity.get("collectionName")
        if not isinstance(collection, str) or not collection:
            continue
        collections.add(collection)
        properties = entity.get("properties")
        if isinstance(properties, dict) and any(
            isinstance(p, dict) and p.get("field") == "_uri" for p in properties.values()
        ):
            carrying.add(collection)
        for index in entity.get("indexes") or []:
            if not isinstance(index, dict) or index.get("type") not in _EQUALITY_INDEX_TYPES:
                continue
            fields = index.get("fields")
            if isinstance(fields, list) and fields and fields[0] == "_uri":
                indexed.add(collection)
    return UriIndexes(frozenset(collections), frozenset(carrying), frozenset(indexed))


def _project_shard_families(
    physical_mapping: dict[str, Any],
) -> tuple[tuple[str, ...], ...]:
    """Project ``physicalMapping.shardFamilies`` onto a deterministic
    immutable view (PRD §6.5.3).

    Each inner tuple is the sorted member-collection names of one
    family; the outer tuple is sorted by the first member of each
    family so two semantically-equal bundles produce two
    ``==``-equal resolver shard-family lists.

    A non-list / non-string entry is dropped silently — the
    :mod:`arango_sparql.translate.mapping` wire-shape validator is
    responsible for refusing malformed bundles; this projector
    operates defensively on already-normalised data so a
    forward-compat analyzer that adds a future field type cannot
    crash the translate path.
    """

    raw = physical_mapping.get("shardFamilies") if physical_mapping else None
    if not raw or not isinstance(raw, list):
        return ()
    families: list[tuple[str, ...]] = []
    for family in raw:
        if not isinstance(family, list):
            continue
        members = tuple(sorted(str(m) for m in family if isinstance(m, str)))
        if members:
            families.append(members)
    families.sort(key=lambda f: (f[0], f))
    return tuple(families)


def _synthetic_iri(label: str) -> URIRef:
    """Return a stable ``urn:arango-sparql:concept#<Label>`` IRI.

    Percent-encodes characters that are not valid in an IRI fragment so
    labels with spaces, slashes, or other punctuation (rare but possible
    when the analyzer surfaces a customer's literal label) still produce
    a round-trippable IRI.
    """

    return URIRef(str(_SYNTHETIC_CONCEPT_NS) + quote(label, safe=""))


def _synthesize_graph_from_bundle(bundle: MappingBundle) -> Graph:
    """Build a minimal rdflib graph carrying the bundle's physical
    mapping as ``phys:*`` annotations on synthesized ``owl:Class`` and
    ``owl:ObjectProperty`` resources.

    The output is *not* a faithful OWL ontology — it carries no
    ``rdfs:label`` strings and no domain/range axioms beyond what the
    bundle itself declares. Its sole purpose is to give the resolver a
    graph it can read using its existing lookup paths.
    """

    g = Graph()
    # A ``UNION`` supertype's members: the physical ``members`` list or, failing
    # that, the conceptual entity's ``subtypes`` (arango-query-core's mapping
    # contract). The resolver reads them as ``rdfs:subClassOf`` (#94, #126).
    conceptual = bundle.conceptual_schema or {}
    concept_entities = [
        e for e in conceptual.get("entities", []) or [] if isinstance(e, dict) and e.get("name")
    ]
    subtypes = {str(e["name"]): e.get("subtypes") or [] for e in concept_entities}
    # A relationship's endpoints live in the conceptual schema in
    # arango-query-core's contract (its ``turtle_to_mapping`` writes none in
    # the physical block); the physical spec's own, when given, wins (#126).
    endpoints = {
        str(r["type"]): (r.get("fromEntity"), r.get("toEntity"))
        for r in conceptual.get("relationships", []) or []
        if isinstance(r, dict) and r.get("type")
    }
    for label, spec in bundle.entities().items():
        if not isinstance(label, str) or not label:
            continue
        iri = _synthetic_iri(label)
        g.add((iri, RDF.type, OWL.Class))

        style = str(spec.get("style") or "COLLECTION")
        # For RPT entities the resolver-visible "collection" is the
        # triples table itself — that is where the engine reads rows.
        collection = spec.get("collectionName")
        if style == "RPT" and not collection:
            collection = spec.get("triplesCollection") or "_triples"
        if collection:
            g.add((iri, _SYNTHETIC_PHYS_NS["collectionName"], Literal(str(collection))))
        g.add((iri, _SYNTHETIC_PHYS_NS["mappingStyle"], Literal(style)))

        for src_key, phys_local in _BUNDLE_ENTITY_ANNOTATIONS:
            value = spec.get(src_key)
            if value is None:
                continue
            g.add((iri, _SYNTHETIC_PHYS_NS[phys_local], Literal(str(value))))
        for member in spec.get("members") or subtypes.get(label) or []:
            if isinstance(member, str) and member:
                g.add((_synthetic_iri(member), RDFS.subClassOf, iri))

        # Per-property conceptual→physical mapping (CDF CC-12): CSI producers
        # emit OWL-style conceptual property names (``accountId``) with the
        # stored field recorded under ``properties.<name>.field``
        # (``account_id``). Declare each as an ``owl:DatatypeProperty`` with a
        # ``phys:attributeName`` annotation so :meth:`resolve_property` reads
        # the stored name instead of degrading to the IRI local name.
        prop_map = spec.get("properties")
        if isinstance(prop_map, dict):
            for prop_name, prop_spec in prop_map.items():
                if not isinstance(prop_name, str) or not prop_name:
                    continue
                p_iri = _synthetic_iri(prop_name)
                g.add((p_iri, RDF.type, OWL.DatatypeProperty))
                g.add((p_iri, RDFS.domain, iri))
                # ``field`` is one literal name; ``path`` names a value inside
                # a nested object, one segment per level (issue #72).
                location = property_field_path(prop_name, prop_spec)
                if location is None:
                    continue
                if len(location) > 1:
                    g.add((p_iri, _SYNTHETIC_PHYS_NS["attributePath"], Literal(json.dumps(list(location)))))
                elif location[0] != prop_name:
                    g.add((p_iri, _SYNTHETIC_PHYS_NS["attributeName"], Literal(location[0])))

    # A conceptual entity's properties are its datatype properties, as
    # arango-query-core's ``mapping_to_turtle`` declares them, so a subject
    # read by one is routed by its domain (#126).
    for entity in concept_entities:
        for prop in entity.get("properties") or []:
            name = prop.get("name") if isinstance(prop, dict) else None
            if isinstance(name, str) and name:
                p_iri = _synthetic_iri(name)
                g.add((p_iri, RDF.type, OWL.DatatypeProperty))
                g.add((p_iri, RDFS.domain, _synthetic_iri(str(entity["name"]))))

    for rtype, spec in bundle.relationships().items():
        if not isinstance(rtype, str) or not rtype:
            continue
        iri = _synthetic_iri(rtype)
        g.add((iri, RDF.type, OWL.ObjectProperty))

        edge_collection = spec.get("edgeCollectionName")
        style = str(spec.get("style") or "DEDICATED_COLLECTION")
        # RPT_EDGE relationships ride the entity's triples table; if no
        # explicit edge collection is provided, fall back to the
        # bundle's triples collection (if any).
        if style == "RPT_EDGE" and not edge_collection:
            edge_collection = spec.get("triplesCollection") or "_triples"
        if edge_collection:
            g.add(
                (
                    iri,
                    _SYNTHETIC_PHYS_NS["edgeCollectionName"],
                    Literal(str(edge_collection)),
                )
            )
        g.add((iri, _SYNTHETIC_PHYS_NS["mappingStyle"], Literal(style)))

        concept_from, concept_to = endpoints.get(rtype, (None, None))
        from_entity = spec.get("fromEntity") or concept_from
        to_entity = spec.get("toEntity") or concept_to
        if isinstance(from_entity, str) and from_entity:
            g.add((iri, RDFS.domain, _synthetic_iri(from_entity)))
        if isinstance(to_entity, str) and to_entity:
            g.add((iri, RDFS.range, _synthetic_iri(to_entity)))

        for src_key, phys_local in _BUNDLE_RELATIONSHIP_ANNOTATIONS:
            value = spec.get(src_key)
            if value is None:
                continue
            g.add((iri, _SYNTHETIC_PHYS_NS[phys_local], Literal(str(value))))

    return g
