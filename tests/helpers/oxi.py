"""pyoxigraph helpers — load RDF, run reference SPARQL, compare bindings.

The cross-validation tests under :mod:`tests.cross` use this module
to treat ``pyoxigraph`` as the W3C-compliant ground truth. Per
``.cursor/rules/200-testing.mdc``, individual tests must not import
``pyoxigraph`` directly — they go through these helpers so the
binding-equality semantics stay consistent.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import pyoxigraph as oxi
except ImportError:  # pragma: no cover - pyoxigraph is in [dev]
    oxi = None  # type: ignore[assignment]


@dataclass
class OxiQueryResult:
    """Form-aware result of a single `oxi_query()` call.

    `kind` is `"ask"` or `"select"`, set from the *actual Python type*
    `store.query()` returned -- never from inspecting the SPARQL text.
    """

    kind: str
    rows: list[dict[str, str]] | None = None
    boolean: bool | None = None


def load_store(rdf_paths: list[Path]) -> Any:
    """Load one or more RDF files into an in-memory pyoxigraph store."""
    if oxi is None:
        raise RuntimeError("pyoxigraph is not installed; add the [dev] extra")
    store = oxi.Store()
    for path in rdf_paths:
        store.load(path.read_bytes(), _format_for(path))
    return store


def load_store_from_string(ttl: str) -> Any:
    """Convenience helper for inline test data."""
    if oxi is None:
        raise RuntimeError("pyoxigraph is not installed; add the [dev] extra")
    store = oxi.Store()
    store.load(ttl.encode("utf-8"), oxi.RdfFormat.TURTLE)
    return store


def _format_for(path: Path) -> Any:
    suffix = path.suffix.lower()
    if oxi is None:  # pragma: no cover
        raise RuntimeError("pyoxigraph is not installed")
    if suffix in {".ttl", ".turtle"}:
        return oxi.RdfFormat.TURTLE
    if suffix in {".nt"}:
        return oxi.RdfFormat.N_TRIPLES
    if suffix in {".nq"}:
        return oxi.RdfFormat.N_QUADS
    if suffix in {".trig"}:
        return oxi.RdfFormat.TRIG
    raise ValueError(f"unsupported RDF file suffix: {suffix!r}")


def literal_to_python(literal: Any) -> Any:
    """Coerce a pyoxigraph ``Literal`` to the Python primitive AQL
    will round-trip via JSON.

    pyoxigraph 0.3+ exposes a ``Literal.value`` attribute (lexical
    form) and a ``.datatype`` (a ``NamedNode``). We delegate the
    XSD → Python coercion to the same map the SRX parser uses so the
    loader and the comparator stay symmetric — a divergence between
    the two sides has to come from AQL execution, not from a
    type-mapping mismatch in our test harness.
    """
    text = literal.value
    datatype = getattr(literal, "datatype", None)
    if datatype is None:
        return text
    dt_iri = datatype.value
    if dt_iri == "http://www.w3.org/2001/XMLSchema#boolean":
        return text.strip().lower() == "true"
    if dt_iri in (
        "http://www.w3.org/2001/XMLSchema#integer",
        "http://www.w3.org/2001/XMLSchema#int",
        "http://www.w3.org/2001/XMLSchema#long",
        "http://www.w3.org/2001/XMLSchema#short",
        "http://www.w3.org/2001/XMLSchema#byte",
        "http://www.w3.org/2001/XMLSchema#nonNegativeInteger",
        "http://www.w3.org/2001/XMLSchema#nonPositiveInteger",
        "http://www.w3.org/2001/XMLSchema#positiveInteger",
        "http://www.w3.org/2001/XMLSchema#negativeInteger",
        "http://www.w3.org/2001/XMLSchema#unsignedLong",
        "http://www.w3.org/2001/XMLSchema#unsignedInt",
        "http://www.w3.org/2001/XMLSchema#unsignedShort",
        "http://www.w3.org/2001/XMLSchema#unsignedByte",
    ):
        try:
            return int(text)
        except ValueError:
            return text
    if dt_iri in (
        "http://www.w3.org/2001/XMLSchema#decimal",
        "http://www.w3.org/2001/XMLSchema#double",
        "http://www.w3.org/2001/XMLSchema#float",
    ):
        try:
            return float(text)
        except ValueError:
            return text
    return text


def oxi_bindings(store: Any, sparql: str, *, union_default_graph: bool = False) -> list[dict[str, str]]:
    """Run *sparql* against *store* and return SELECT bindings as plain dicts.

    Values are stringified via the term's ``str()`` so equality comparison
    between pyoxigraph and ArangoDB-derived bindings does not depend on
    Python object identity.

    pyoxigraph 0.5 API: ``store.query()`` returns ``QuerySolutions`` which
    exposes ``.variables`` (a list of ``Variable`` objects); each
    ``QuerySolution`` row supports ``__getitem__`` by either a
    ``Variable`` or its name string and yields ``NamedNode | Literal | ...``
    or ``None`` for unbound projection variables.

    *union_default_graph* reads the default graph as the union of every
    graph, as the resolver's lax ``default_graph_includes_named`` does.
    """
    result = store.query(sparql, use_default_graph_as_union=union_default_graph)
    variables = list(result.variables)
    rows: list[dict[str, str]] = []
    for solution in result:
        row: dict[str, str] = {}
        for var in variables:
            term = solution[var]
            if term is None:
                continue
            row[var.value] = str(term)
        rows.append(row)
    return rows


def oxi_query(store: Any, sparql: str) -> OxiQueryResult:
    """Run *sparql* against *store* and return a form-aware result.

    Branches on the Python type `store.query()` actually returns
    (`pyoxigraph.QueryBoolean` for ASK, `pyoxigraph.QuerySolutions` for
    SELECT) -- never by inspecting the SPARQL text (a `SELECT` inside a
    nested ASK pattern, or a comment, would confuse a text-based check).

    `QueryBoolean` exposes only `.serialize()`, never `.variables` --
    reading `.variables` on it raises. `bool(result)` is the correct,
    documented way to extract the ASK answer.

    Any `SyntaxError`/`RuntimeError` `store.query()` raises (malformed
    SPARQL, an unsupported custom function, ...) propagates to the
    caller unchanged -- classifying/tagging that failure is the judge's
    responsibility (D-05), not this helper's.
    """
    result = store.query(sparql)
    if isinstance(result, oxi.QueryBoolean):
        return OxiQueryResult(kind="ask", boolean=bool(result))
    variables = list(result.variables)
    rows: list[dict[str, str]] = []
    for solution in result:
        row: dict[str, str] = {}
        for var in variables:
            term = solution[var]
            if term is None:
                continue
            row[var.value] = str(term)
        rows.append(row)
    return OxiQueryResult(kind="select", rows=rows)


def normalize_oxi_row(row: dict[str, str]) -> dict[str, Any]:
    """Strip pyoxigraph's N-Triples lexical envelope from a SELECT row.

    pyoxigraph stringifies Literals as ``"Alice"`` (with quotes, plus an
    optional ``^^<datatype>`` suffix) and IRIs as ``<http://...>``. The
    AQL interpreter returns plain Python values, so to compare bindings
    we unwrap each term to a bare ``str`` / ``int``. Integer-lexical
    literals are coerced to ``int`` so ``"30"^^xsd:integer`` compares
    equal to the AQL side's ``30``.
    """
    out: dict[str, Any] = {}
    for k, v in row.items():
        if v.startswith('"'):
            inner = v.split('"^^')[0].strip('"')
            try:
                out[k] = int(inner)
            except ValueError:
                out[k] = inner
        elif v.startswith("<") and v.endswith(">"):
            out[k] = v[1:-1]
        else:
            out[k] = v
    return out


def drop_null_bindings(row: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` values from an AQL-derived solution row.

    pyoxigraph omits unbound projection variables from each solution
    (see :func:`oxi_bindings`), so for OPTIONAL / left-join parity the
    Arango side must drop its ``null`` bindings before comparison. For
    fully-bound queries this is a no-op.
    """
    return {k: v for k, v in row.items() if v is not None}


def assert_bindings_equal(
    expected: list[dict[str, str]],
    actual: list[dict[str, str]],
) -> None:
    """Order-insensitive bag equality on SELECT bindings.

    Rows are ordered by ``repr``: one variable can hold a string in one row
    and a number in another (``{ ?s :name ?x } UNION { ?s :age ?x }``), which
    Python cannot order against each other."""
    exp = sorted((tuple(sorted(row.items())) for row in expected), key=repr)
    act = sorted((tuple(sorted(row.items())) for row in actual), key=repr)
    assert exp == act, f"bindings differ:\n  expected={exp}\n  actual={act}"


def assert_bindings_equal_ordered(
    expected: list[dict[str, str]],
    actual: list[dict[str, str]],
) -> None:
    """List-equality on SELECT bindings — preserves row order.

    Used by cross-validation tests that exercise ORDER BY semantics,
    where the ordering itself is the property under test. For tied
    sort keys (e.g. two rows with the same age) we still want a
    deterministic comparison, so the row dicts are turned into
    sorted-tuple keys before the position-by-position equality check.
    """
    exp = [tuple(sorted(row.items())) for row in expected]
    act = [tuple(sorted(row.items())) for row in actual]
    assert exp == act, f"ordered bindings differ:\n  expected={exp}\n  actual={act}"
