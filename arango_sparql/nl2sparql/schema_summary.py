"""Schema summary for the NL→SPARQL prompt, built from the analyzer's mapping.

The prompt used to carry only the OWL ontology, and the schema analyzer's OWL
export (arangodb-schema-analyzer 0.14) declares classes and object properties
but **no datatype properties** — so the model never saw a single field name.
On prod.demo ``IAM`` the analyzer found 1,576 fields across 113 types, yet the
model had to guess, and reached for the ``phys:typeValue`` storage annotation
(2026-10-08).

Ported from arango-cypher-py's ``nl2cypher._core._build_schema_summary``,
which never relies on OWL for its NL context: every type with up to
``MAX_PROPS`` of its properties, then every relationship with its domain and
range. Same rule as cypher: only *conceptual* names — no collection names,
mapping styles or ``phys:`` annotations; the translator owns the physical
mapping. Terms are written as SPARQL prefixed names in the ontology's default
(``:``) namespace — the form the model must emit. An undeclared property such
as ``:file_name`` resolves through the translator's local-name fallback to the
document attribute ``file_name`` (PRD §6.7).
"""

from __future__ import annotations

import re
from typing import Any

#: Properties listed per class, as in the sister project.
MAX_PROPS = 8

# A safe subset of SPARQL's PN_LOCAL: a name outside it cannot be written as a
# plain prefixed name, so it is left out rather than shown in a form the model
# would copy into an invalid query.
_PN_LOCAL_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")

_DEFAULT_PREFIX = re.compile(r"@prefix\s+:\s*<([^>]+)>", re.IGNORECASE)
_SPARQL_DEFAULT_PREFIX = re.compile(r"PREFIX\s+:\s*<([^>]+)>", re.IGNORECASE)

_HEADER = (
    "Classes and their data properties. Write every term with the ontology's "
    "default prefix `{prefix}` (declare `PREFIX : <{namespace}>`), e.g. "
    "`?x a :SomeClass ; :some_property ?value`. A property listed here can be "
    "used even if the Turtle above does not declare it."
)


def default_namespace(*turtles: str | None) -> str | None:
    """The ontology's default (``:``) namespace, from the first Turtle that
    declares one; ``None`` when none does (no summary is rendered then)."""
    for ttl in turtles:
        if not ttl:
            continue
        match = _DEFAULT_PREFIX.search(ttl) or _SPARQL_DEFAULT_PREFIX.search(ttl)
        if match:
            return match.group(1)
    return None


def _term(name: str) -> str | None:
    return f":{name}" if name and _PN_LOCAL_SAFE.match(name) else None


def _entity_props(entity: dict[str, Any], physical: dict[str, Any]) -> list[str]:
    props = [p.get("name", "") for p in entity.get("properties") or [] if isinstance(p, dict)]
    if not props:
        pme = (physical.get("entities") or {}).get(entity.get("name", ""))
        if isinstance(pme, dict) and isinstance(pme.get("properties"), dict):
            props = list(pme["properties"].keys())
    return props


def build_schema_summary(bundle: Any, *, namespace: str) -> str:
    """Render the analyzer bundle as a prompt section for SPARQL generation.

    *bundle* is a :class:`~arango_sparql.translate.mapping.MappingBundle` (only
    its ``conceptual_schema`` / ``physical_mapping`` dicts are read). Returns an
    empty string when the bundle carries no entities, so callers can pass the
    result straight to the prompt builder. Deterministic for a given bundle —
    classes and relationships are sorted — so the system prompt stays
    cache-stable across requests.
    """
    cs = getattr(bundle, "conceptual_schema", None) or {}
    pm = getattr(bundle, "physical_mapping", None) or {}

    class_lines: list[str] = []
    for entity in sorted(
        (e for e in cs.get("entities") or [] if isinstance(e, dict)),
        key=lambda e: str(e.get("name", "")),
    ):
        cls = _term(str(entity.get("name", "")))
        if cls is None:
            continue
        terms = [t for t in (_term(p) for p in _entity_props(entity, pm)) if t][:MAX_PROPS]
        listed = ", ".join(terms) if terms else "no properties"
        class_lines.append(f"  {cls} — {listed}")
    if not class_lines:
        return ""

    rel_lines: list[str] = []
    for rel in sorted(
        (r for r in cs.get("relationships") or [] if isinstance(r, dict)),
        key=lambda r: (str(r.get("type", "")), str(r.get("fromEntity", "")), str(r.get("toEntity", ""))),
    ):
        prop = _term(str(rel.get("type", "")))
        if prop is None:
            continue
        domain = _term(str(rel.get("fromEntity", ""))) if rel.get("fromEntity") != "Any" else None
        range_ = _term(str(rel.get("toEntity", ""))) if rel.get("toEntity") != "Any" else None
        line = f"  ?a {prop} ?b"
        if domain or range_:
            line += f"   (?a a {domain or 'any class'} ; ?b a {range_ or 'any class'})"
        rel_lines.append(line)

    lines = [_HEADER.format(prefix=":", namespace=namespace), *class_lines]
    if rel_lines:
        lines += ["Relationships (object properties between instances):", *rel_lines]
    return "\n".join(lines)
