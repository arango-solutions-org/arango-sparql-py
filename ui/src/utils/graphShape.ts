// The graph a SELECT result draws, from the query's own triple patterns (the server's
// `graph` run: `translate/graph_shape.py`). The rows alone do not say how entities connect;
// `?loan :borrowedBy ?member` does: an edge from each row's loan to its member.
// `?member :name ?memberName` is a property of the member, and `?loan a :Loan` its class.

export type PatternTerm = { var: string } | { iri: string } | { literal: string };

export interface Pattern {
  subject: PatternTerm;
  predicate: PatternTerm;
  object: PatternTerm;
  /** What the schema says the predicate is, when it says: a class (`a :Loan`), a
   * relationship between entities (an edge) or a property (a value on its subject). */
  kind?: "type" | "relationship" | "property";
  /** For a pattern inside OPTIONAL or a UNION branch: for each such group around it, the
   * variables only that group binds. It holds for a row that binds one of each. */
  when?: string[][];
}

export interface GraphShape {
  patterns: Pattern[];
  /** True when the run projected every variable (a plain SELECT). */
  widened: boolean;
}

export interface GraphRun {
  bindings?: Array<Record<string, unknown>>;
  truncated?: boolean;
  graph?: GraphShape | null;
}

export interface DrawnGraph {
  /** The graph run's rows behind the table's rows. */
  rows: Array<Record<string, unknown>>;
  shape: ShapedGraph;
  /** The graph run stopped at the result cap: some entities may be missing. */
  truncated: boolean;
}

export interface ShapeTriple {
  s: string;
  p: string;
  o: unknown;
}

export interface ShapedGraph {
  triples: ShapeTriple[];
  /** Values drawn as nodes: entities, never literals. */
  nodes: Set<string>;
  /** A node's display label: its name, title or label, when the rows carry one. */
  labels: Map<string, string>;
  /** A node's class, from an `a :Class` pattern. */
  classes: Map<string, string>;
}

const RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type";
const LABEL_PROPERTIES = ["name", "title", "label", "fullname", "displayname"];

/** An ArangoDB document id (`members/100`) or an IRI: a value that can name an entity. A
 * literal can look like one too ("Either/Or"), so a value alone decides only when the
 * schema does not (see `shapeGraph`). */
export function isEntity(value: unknown): value is string {
  if (typeof value !== "string" || value.length === 0) return false;
  return isDocumentId(value) || isIri(value);
}

function isDocumentId(value: string): boolean {
  return /^[A-Za-z][A-Za-z0-9_-]*\/[^\s/]+$/.test(value);
}

function isIri(value: string): boolean {
  return /^[A-Za-z][A-Za-z0-9+.-]*:\S+$/.test(value);
}

export function localName(iri: string): string {
  const cut = Math.max(iri.lastIndexOf("#"), iri.lastIndexOf("/"), iri.lastIndexOf(":"));
  return cut >= 0 && cut < iri.length - 1 ? iri.slice(cut + 1) : iri;
}

function varOf(term: PatternTerm): string | null {
  return "var" in term ? term.var : null;
}

const bound = (value: unknown) => value !== undefined && value !== null;

/** The graph a table's rows draw, from the graph run of the same query: `null` when it
 * draws none, and the rows are drawn as they are. Only a run that projected every variable
 * says which entities are behind the rows; of its rows, those behind the table's are kept
 * (the run has no limit, so it holds every solution behind a limited table). */
export function graphFromRun(
  tableRows: Array<Record<string, unknown>>,
  run: GraphRun,
): DrawnGraph | null {
  if (!run.graph?.widened) return null;
  const projected = [...new Set(tableRows.flatMap((r) => Object.keys(r)))];
  const keyOf = (row: Record<string, unknown>) => JSON.stringify(projected.map((v) => row[v] ?? null));
  const shown = new Set(tableRows.map(keyOf));
  const rows = (run.bindings ?? []).filter((r) => shown.has(keyOf(r)));
  const shape = shapeGraph(rows, run.graph.patterns);
  return shape.nodes.size > 0 ? { rows, shape, truncated: run.truncated === true } : null;
}

/** The nodes, edges, properties, labels and classes the rows and patterns draw.
 *
 * A pattern draws for a row only when it holds for it (`when`). Its `kind` decides what its
 * object is. Without one, the object is an entity when its variable has patterns of its own
 * (`?member :name ?n` makes `?member` one) or its value is an IRI; a value that only looks
 * like a document id is a literal, which keeps a title such as "Either/Or" off the canvas. */
export function shapeGraph(rows: Array<Record<string, unknown>>, patterns: Pattern[]): ShapedGraph {
  const subjects = new Set(patterns.map((p) => varOf(p.subject)).filter((v): v is string => v !== null));
  const nodes = new Set<string>();
  const classes = new Map<string, string>();
  const props = new Map<string, Array<{ p: string; value: unknown }>>();
  const triples: ShapeTriple[] = [];
  const seen = new Set<string>();

  const add = (t: ShapeTriple) => {
    const key = JSON.stringify([t.s, t.p, t.o]);
    if (seen.has(key)) return;
    seen.add(key);
    triples.push(t);
  };
  const valueOf = (term: PatternTerm, row: Record<string, unknown>): unknown =>
    "var" in term ? row[term.var] : "iri" in term ? term.iri : term.literal;
  const holds = (pat: Pattern, row: Record<string, unknown>) =>
    (pat.when ?? []).every((group) => group.some((v) => bound(row[v])));
  const entityObject = (pat: Pattern, value: unknown): value is string => {
    if (!isEntity(value)) return false;
    if (pat.kind === "relationship") return true;
    if (pat.kind === "property") return false;
    if ("iri" in pat.object) return true;
    const objectVar = varOf(pat.object);
    return (objectVar !== null && subjects.has(objectVar)) || isIri(value);
  };

  for (const row of rows) {
    for (const pat of patterns) {
      if (!holds(pat, row)) continue; // an OPTIONAL that did not match, another UNION branch
      const s = valueOf(pat.subject, row);
      const predicate = valueOf(pat.predicate, row);
      if (!isEntity(s) || typeof predicate !== "string") continue;
      nodes.add(s);
      if (pat.kind === "type" || (pat.kind === undefined && predicate === RDF_TYPE)) {
        const cls = valueOf(pat.object, row);
        if (typeof cls === "string" && isIri(cls)) classes.set(s, localName(cls));
        continue;
      }
      if ("literal" in pat.object) continue; // a literal constant is a filter, not a fact to draw
      const value = valueOf(pat.object, row);
      if (!bound(value)) continue;
      add({ s, p: predicate, o: value });
      if (entityObject(pat, value)) {
        nodes.add(value);
      } else {
        const list = props.get(s) ?? [];
        list.push({ p: localName(predicate).toLowerCase(), value });
        props.set(s, list);
      }
    }
  }

  const labels = new Map<string, string>();
  for (const [n, list] of props) {
    const named = LABEL_PROPERTIES.map((want) => list.find((x) => x.p === want)).find((x) => x);
    const pick = named ?? list.find((x) => typeof x.value === "string");
    if (pick && bound(pick.value)) labels.set(n, String(pick.value));
  }
  return { triples, nodes, labels, classes };
}
