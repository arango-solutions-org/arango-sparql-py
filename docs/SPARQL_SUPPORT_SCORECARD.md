# SPARQL 1.1 Support Scorecard — `arango-sparql-py`

**What this is:** a feature-by-feature accounting of how much of SPARQL 1.1 the
`arango-sparql-py` transpiler covers when translating to ArangoDB AQL, backed by
the W3C DAWG conformance harness and the deterministic golden suites.

**As of:** 2026-09-09 (`main` @ `4ef6db1`) · **Measured by:**
`tests/w3c/analyze_coverage.py` + `tests/translate/*` goldens · **Legend:**
✅ Full · 🟡 Partial (works, with documented restrictions) · ⛔ Unsupported (raises
`UnsupportedSparqlError`, never wrong AQL) · ⚪ Out of scope by design

> **Never silently wrong.** Every unsupported construct raises a typed
> `UnsupportedSparqlError` with a stable code at the single dispatch boundary
> (`arango_sparql/translate/visitor.py:216`, `errors.py:28`) — the transpiler
> refuses rather than emitting AQL that would return plausible-but-wrong rows.

---

## Headline

| Dimension | Score | Basis |
| --- | --- | --- |
| **W3C DAWG query-evaluation** | **100%** of in-scope (239/239, 0 fail, 0 xfail) | `tests/w3c/COVERAGE_REPORT.md` |
| **W3C DAWG syntax (positive)** | **100%** (63/63) | " |
| **Query language breadth** | ✅ SELECT/ASK/CONSTRUCT/DESCRIBE, all path operators, all aggregates, 45+ built-ins | §Feature tables below |
| **Storage-model breadth** | ✅ 4 entity models + 3 edge models + hybrids in one query | PRD §6.1 |
| **Live execution (document_edge)** | 64.9% (124/191) — remainder needs RDFS/OWL entailment (out of transpiler scope) | `COVERAGE_REPORT.md` |
| **Federation (SERVICE)** | ⛔ Not supported | — |
| **SPARQL Update** | ⚪ Read-only by design (HTTP 405) | — |

The single hard number to cite is **100% W3C DAWG query-evaluation coverage of
every in-scope case** (239/239) — **zero** in-scope translation gaps and **zero**
hard failures. The only excluded cases are, honestly, not translation tests:
federation (`SERVICE`, 7) dispatches to a remote endpoint with no AQL analog, and
TSV/JSON result-serialization tests (7) check output documents the transpiler
does not emit — both counted out-of-scope alongside the Protocol / Service-Description
/ CSV suites the harness already excluded.

---

## 1. Query forms

| Feature | Status | Notes / evidence |
| --- | --- | --- |
| `SELECT` | ✅ | `visitor.py:233` |
| `ASK` | ✅ | `visitor.py:242` — emits `RETURN LENGTH(...) > 0` |
| `CONSTRUCT` | ✅ | `visitor.py:265`; incl. `CONSTRUCT WHERE {}` short-form. Empty WHERE rejected |
| `DESCRIBE` | ✅ | `visitor.py:385`; bare `DESCRIBE <iri>` and `DESCRIBE ?v WHERE`. Needs a resource list |

## 2. Graph patterns

| Feature | Status | Notes / evidence |
| --- | --- | --- |
| Basic Graph Pattern (BGP) | ✅ | `visitor.py:1139`, incl. empty-BGP identity semantics |
| Join | ✅ | `visitor.py:1057` |
| `UNION` | ✅ | `visitor.py:1091` → `union_paths.py` |
| `MINUS` | ✅ | `visitor.py:1083` → `minus_exists.py` |
| `FILTER` | ✅ | `visitor.py:610` |
| `FILTER EXISTS` / `NOT EXISTS` | ✅ | `filter_builtins.py:121,129` (probe recipe) |
| `GRAPH` (named graphs, IRI + var) | ✅ | `visitor.py:1104` (storage model per ADR-0001) |
| `BIND` | ✅ | `visitor.py:809` |
| `VALUES` (incl. empty) | ✅ | `subselect.py:67,165` |
| Sub-`SELECT` | ✅ | `visitor.py:1133` → `subselect.py:79` |
| `OPTIONAL` (LeftJoin) | 🟡 | `visitor.py:628`. Works for the common bound-subject BGP case. **Restrictions:** body must be a plain BGP (not UNION/SERVICE); predicate must be var/IRI; object restrictions apply; an `OPTIONAL` whose subject is not already bound by the required side is not yet supported. Cross-subject handled via ADR-0002 (`optional_crosssubject.py`) |

## 3. Property paths

All five rdflib path classes are handled (`paths.py`).

| Operator | Status | Notes / evidence |
| --- | --- | --- |
| Sequence `/` | ✅ | `paths.py:134` |
| Inverse `^` | ✅ | `paths.py:192` |
| Alternative `\|` | ✅ | `paths.py:114` → `union_paths.py:87` |
| Zero/one-or-more `*` `+`, zero-or-one `?` | ✅ | `paths.py:217` (`_emit_mul_path`) |
| Negated property set `!` (incl. inverse arms) | ✅ | `paths.py:317` |

**Caveats:** property paths over **RPT-mapped** subjects are rejected
(`paths.py:182,273,390`); a few deeply-nested combinations inside `*`/`+`/`?` or
`!(...)` are rejected rather than mis-translated.

## 4. Solution modifiers

| Feature | Status | Notes / evidence |
| --- | --- | --- |
| `ORDER BY` | ✅ | `visitor.py:1026` |
| `DISTINCT` | ✅ | `visitor.py:598`. With `LIMIT`/`OFFSET`, the rows are de-duplicated *before* the slice, as SPARQL's `Slice(Distinct(…))` requires (`builder.py:995`; until 2026-10-16 the slice ran first, issue #39) |
| `LIMIT` | ✅ | `visitor.py:602` |
| `OFFSET` | 🟡 | Supported **with** `LIMIT`; `OFFSET` without `LIMIT` is rejected (`visitor.py:612`) |
| `GROUP BY` | ✅ | `visitor.py:878` |
| `HAVING` | ✅ | `visitor.py:616` (Filter after aggregation) |
| Projection / SELECT expressions | ✅ | `visit_Project` / `visit_Extend` |
| `REDUCED` | ⛔ | No handler — falls through to `UnsupportedSparqlError` |

## 5. Aggregates

| Aggregate | Status | Notes / evidence |
| --- | --- | --- |
| `COUNT` (incl. `DISTINCT`, `COUNT(*)`) | ✅ | `visitor.py:870,922` |
| `SUM` / `MIN` / `MAX` / `AVG` | ✅ | `visitor.py:871-874` |
| `SAMPLE` | ✅ | `visitor.py:890` |
| `GROUP_CONCAT` (+ `SEPARATOR`) | ✅ | `visitor.py:949` |

**Caveat:** `DISTINCT` is supported on `COUNT` only; `DISTINCT SUM/AVG/MIN/MAX`
is rejected (`visitor.py:982`).

## 6. Expressions & built-in functions

**Operators — ✅ complete:** relational `= != < <= > >=`, `IN` / `NOT IN`,
logical `&& || !`, arithmetic `+ - * /` (`visitor.py:1941-2079`).

**Built-in functions — 45+ supported** (`filter_builtins.py`):

- **Strings:** `STR` `LCASE` `UCASE` `STRLEN` `CONTAINS` `STRSTARTS` `STRENDS`
  `CONCAT` `SUBSTR` `STRBEFORE` `STRAFTER` `REPLACE` `ENCODE_FOR_URI` `REGEX`
- **Lang/type:** `LANG` `LANGMATCHES` `DATATYPE` `STRDT` `STRLANG`
- **Logic/conditional:** `BOUND` `IF` `COALESCE` `EXISTS` `NOT EXISTS`
- **Term tests:** `isLiteral` `isURI`/`isIRI` `isBLANK` `isNUMERIC`
- **Constructors:** `IRI`/`URI` `BNODE` `UUID` `STRUUID`
- **Numeric:** `ABS` `CEIL` `FLOOR` `ROUND` `RAND`
- **Date/time:** `NOW` `YEAR` `MONTH` `DAY` `HOURS` `MINUTES` `SECONDS` `TZ`
  `TIMEZONE`
- **Hash:** `MD5` `SHA1` `SHA256` `SHA512`
- **Casts:** typed-literal constructor casts via the `Function` node path

**Gaps:** `sameTerm` (⛔ not implemented); `REGEX`/`REPLACE` flags other than
case-insensitive `i` are rejected.

## 7. Term types / RDF

| Feature | Status | Notes / evidence |
| --- | --- | --- |
| IRIs | ✅ | bound with a `uri` hint |
| Blank nodes (BGP-scoped, `_:` skolem) | ✅ | `visitor.py:1216`; RPT `_:` convention |
| Literals (typed + language-tagged) | 🟡 | Parsed and usable in FILTER; PG/LPG **storage flattens language tags** and does not carry a plain-vs-`xsd:string` distinction, so a few live-execution cases diverge (see `COVERAGE_REPORT.md` live divergences) |
| Datatypes | 🟡 | `DATATYPE`/`STRDT`/casts supported; no literal-typing carrier in PG/LPG storage |

## 8. Federation — `SERVICE`

⛔ **Unsupported (explicit).** `ServiceGraphPattern` has no visitor and hits the
`UnsupportedSparqlError` boundary. Federation dispatches to a *remote* SPARQL
endpoint at run time — no AQL analog — and is explicitly out of scope in
`docs/architecture/proposals/federation-entry-point.md`. The W3C `service/`
suite (7 cases) is therefore counted out-of-scope, like the Protocol /
Service-Description suites, not as a translation gap.

## 9. SPARQL Update

⚪ **Read-only by design.** `INSERT` / `DELETE` / `LOAD` / `CLEAR` / `CREATE` /
`DROP` / `COPY` / `MOVE` / `ADD` are detected and rejected with **HTTP 405** at
the protocol layer (`arango_sparql/service/protocol/update_detect.py`). No update
algebra exists. The W3C Update suites (93 evaluation + 55 syntax) are counted as
out-of-scope, not failures.

---

## 10. Physical / storage-model coverage (the differentiator)

Unlike a fixed triple-store, the transpiler emits AQL against **whatever ArangoDB
layout the data already uses** — and can mix layouts in a single query
(PRD §6.1).

| Model | Status | Notes |
| --- | --- | --- |
| **PG** — `COLLECTION` (one collection per class) | ✅ | `bgp_select.yml` |
| **LPG** — `LABEL` (shared collection + `typeField`) | ✅ | `edge_traversal.yml`, cross tests |
| **RPT** — `_triples` (subject/predicate/object rows) | ✅ reads · 🟡 no property paths on RPT subjects | `rpt.yml` |
| **DOCUMENT** — plain, no discriminator | ✅ | derived from collection name |
| **Hybrid** — ≥2 models joined in one BGP | ✅ | `hybrid.yml` — a capability the legacy Foxx service could not do |
| **Edge styles** — DEDICATED / GENERIC_WITH_TYPE / RPT_EDGE | ✅ | `edge_traversal.yml` |
| **Sharding / multitenancy** | ✅ | `sharding.yml`, `multitenancy.yml` (cross-tenant joins rejected) |

---

## Known limitations & roadmap

The W3C harness buckets every remaining gap by what it would take to close it.

**In-scope algebra gaps: none.** Every W3C query-evaluation case that is a
translation test now translates (239/239). The former 9 "algebra xfails"
resolved to 7 federation (`SERVICE`) cases + 2 TSV/JSON result-format cases —
none of which are translation tests — now correctly counted out-of-scope.

**Future capability (not a W3C gap):** cross-subject `OPTIONAL` on the PG/LPG
Document model (ADR-0002 Option B/C — where the OPTIONAL's subject is bound only
as a *value* by the required side) is deferred until a real query needs it; RPT
already supports it. It is not required by any in-scope W3C case.

**Also unsupported (deliberate refusals):** `REDUCED`, `sameTerm`, `OFFSET`
without `LIMIT`, `DISTINCT` on non-`COUNT` aggregates, property paths on RPT
subjects, `REGEX` flags beyond `i`.

**Not the transpiler's job (`rdflib` = 14 xfails, live entailment gaps):** the 14
negative-syntax xfails are rdflib parser disagreements. The live-execution
remainder is dominated by **RDFS/OWL entailment** (subClassOf, subPropertyOf,
domain/range reasoning) and RDF language-tag semantics — a reasoning layer, not a
translation layer, and outside v1 scope.

**Out of scope by design:** SPARQL 1.1 Update, Protocol conformance beyond the
query endpoint, Service Description, federation (`SERVICE`), and result-format
serialization tests (CSV / TSV / JSON output). Result serialization is a
service-layer concern (content negotiation, PRD §3.2), not a translation one.

---

## Methodology & reproduction

```bash
# Translation-only W3C coverage (no DB needed) — regenerates COVERAGE_REPORT.md
python tests/w3c/analyze_coverage.py --write

# Full W3C pytest run
pytest -q tests/w3c -m w3c

# Deterministic feature goldens (one YAML per feature area)
pytest -q tests/translate

# Live execution against a real ArangoDB (canonical baseline)
RUN_INTEGRATION=1 python tests/w3c/analyze_coverage.py --live --profile document_edge --write
```

A feature counts as **supported** when the visitor produces non-empty AQL without
raising `UnsupportedSparqlError` **and** a golden or W3C case pins the emitted
AQL / binding result. `pyoxigraph` is the W3C ground truth for cross-validation:
the same SPARQL runs against pyoxigraph and against the transpiled AQL, and the
bindings are compared. Sources of truth: `tests/w3c/COVERAGE_REPORT.md`,
`tests/translate/*.yml`, PRD §3.1 / §6.1.
