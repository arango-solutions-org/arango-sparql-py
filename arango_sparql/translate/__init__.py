"""SPARQL → AQL translation pipeline.

Submodules:

- :mod:`.parser`   — wraps ``rdflib.plugins.sparql`` parser + algebra.
- :mod:`.visitor`  — Algebra visitor; one ``visit_<NodeType>`` per node.
- :mod:`.builder`  — parameterized AQL query builder (bind-vars only).
- :mod:`.resolver` — OWL/Turtle URI → ArangoDB collection resolver.
- :mod:`.mapping`  — ``MappingBundle`` wire-format (conceptual + physical).
- :mod:`.csi`      — ``CSI v1`` interchange → ``MappingBundle`` adapter.
- :mod:`.predicates` — caller-registered predicates (magic properties), and an
  ArangoSearch full-text handler for them.
"""

from __future__ import annotations
