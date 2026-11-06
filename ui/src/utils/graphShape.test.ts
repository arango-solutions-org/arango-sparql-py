import { describe, expect, it } from "vitest";
import { graphFromRun, isEntity, shapeGraph, type Pattern } from "./graphShape";

// The graph a result draws from the query's own patterns: the demo library's question
// "which members borrowed a book", run with every variable projected.
const L = "https://example.org/library#";
const TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type";
const v = (name: string) => ({ var: name });
const iri = (value: string) => ({ iri: value });

const PATTERNS: Pattern[] = [
  { subject: v("loan"), predicate: iri(TYPE), object: iri(L + "Loan"), kind: "type" },
  { subject: v("loan"), predicate: iri(L + "borrowedBy"), object: v("member"), kind: "relationship" },
  { subject: v("loan"), predicate: iri(L + "ofBook"), object: v("book"), kind: "relationship" },
  { subject: v("member"), predicate: iri(L + "name"), object: v("memberName"), kind: "property" },
  { subject: v("book"), predicate: iri(L + "title"), object: v("bookTitle"), kind: "property" },
];
const ROWS = [
  { loan: "loans/1000", member: "members/100", book: "books/10", memberName: "Ada Lovelace", bookTitle: "The Left Hand of Darkness" },
  { loan: "loans/1003", member: "members/100", book: "books/17", memberName: "Ada Lovelace", bookTitle: "Either/Or" },
];

describe("shapeGraph", () => {
  it("draws the entities behind the rows and how they connect", () => {
    const g = shapeGraph(ROWS, PATTERNS);
    expect([...g.nodes].sort()).toEqual(["books/10", "books/17", "loans/1000", "loans/1003", "members/100"]);
    const edges = g.triples.filter((t) => g.nodes.has(t.o as string));
    expect(edges).toContainEqual({ s: "loans/1000", p: L + "borrowedBy", o: "members/100" });
    expect(edges).toContainEqual({ s: "loans/1003", p: L + "ofBook", o: "books/17" });
    expect(edges).toHaveLength(4); // one member, borrowed twice: one node, two edges in
  });

  it("labels a node by its name or title, and gives it its class", () => {
    const g = shapeGraph(ROWS, PATTERNS);
    expect(g.labels.get("members/100")).toBe("Ada Lovelace");
    expect(g.labels.get("books/10")).toBe("The Left Hand of Darkness");
    expect(g.classes.get("loans/1000")).toBe("Loan");
  });

  it("keeps a value that only looks like an id a value", () => {
    // "Either/Or" is shaped like a document id; the schema says title is a property.
    const g = shapeGraph(ROWS, PATTERNS);
    expect(g.nodes.has("Either/Or")).toBe(false);
    // Without the schema's word, a variable with no patterns of its own is a value too.
    const unlabelled = PATTERNS.map((p) => ({ subject: p.subject, predicate: p.predicate, object: p.object }));
    expect(shapeGraph(ROWS, unlabelled).nodes.has("Either/Or")).toBe(false);
    // ... and one with patterns of its own is an entity (positive control).
    expect(shapeGraph(ROWS, unlabelled).nodes.has("members/100")).toBe(true);
  });

  it("skips an OPTIONAL that did not match and a constant that filters", () => {
    const rows = [{ m: "members/101", c: null }];
    const patterns: Pattern[] = [
      { subject: v("m"), predicate: iri(L + "city"), object: v("c"), kind: "property" },
      { subject: v("m"), predicate: iri(L + "name"), object: { literal: "Alan Turing" } },
    ];
    const g = shapeGraph(rows, patterns);
    expect([...g.nodes]).toEqual(["members/101"]);
    expect(g.triples).toHaveLength(0);
  });
});

describe("the schema's word wins over a value's shape", () => {
  it("draws a property's IRI-looking value as a value, and prefers a name for the label", () => {
    const rows = [{ m: "members/100", home: "https://ada.example/", city: "London", name: "Ada Lovelace" }];
    const patterns: Pattern[] = [
      { subject: v("m"), predicate: iri(L + "homepage"), object: v("home"), kind: "property" },
      { subject: v("m"), predicate: iri(L + "city"), object: v("city"), kind: "property" },
      { subject: v("m"), predicate: iri(L + "name"), object: v("name"), kind: "property" },
    ];
    const g = shapeGraph(rows, patterns);
    expect(g.nodes.has("https://ada.example/")).toBe(false);
    expect(g.labels.get("members/100")).toBe("Ada Lovelace"); // name, though city came first
  });
});

describe("isEntity", () => {
  it("knows document ids and IRIs, not plain words or numbers", () => {
    expect(isEntity("members/100")).toBe(true);
    expect(isEntity(L + "Member")).toBe(true);
    expect(isEntity("Ada Lovelace")).toBe(false);
    expect(isEntity("2024-01-03")).toBe(false);
    expect(isEntity(42)).toBe(false);
  });
});

describe("which rows a pattern holds for", () => {
  it("draws a UNION branch's or an OPTIONAL's pattern only for the rows it matched", () => {
    // { ?l :ofBook ?b } UNION { ?l :about ?t } — each branch binds a variable of its own.
    const patterns: Pattern[] = [
      { subject: v("l"), predicate: iri(L + "ofBook"), object: v("b"), kind: "relationship", when: [["b"]] },
      { subject: v("l"), predicate: iri(L + "about"), object: v("t"), kind: "relationship", when: [["t"]] },
    ];
    const g = shapeGraph([{ l: "loans/1", b: "books/1", t: null }, { l: "loans/2", b: null, t: "topics/9" }], patterns);
    expect(g.triples).toEqual([
      { s: "loans/1", p: L + "ofBook", o: "books/1" },
      { s: "loans/2", p: L + "about", o: "topics/9" },
    ]);
  });

  it("needs every group around a nested pattern", () => {
    const pat: Pattern = { subject: v("c"), predicate: iri(L + "in"), object: v("k"), kind: "relationship", when: [["c", "k"], ["x"]] };
    expect(shapeGraph([{ c: "cities/1", k: "countries/1", x: null }], [pat]).triples).toHaveLength(0);
    expect(shapeGraph([{ c: "cities/1", k: "countries/1", x: 1 }], [pat]).triples).toHaveLength(1);
  });

  it("takes one bound variable of a group as its match: a nested OPTIONAL may leave the rest unbound", () => {
    // OPTIONAL { ?m :city ?c OPTIONAL { ?c :in ?k } }: the outer group's own variables are c and k.
    const city: Pattern = { subject: v("m"), predicate: iri(L + "city"), object: v("c"), kind: "relationship", when: [["c", "k"]] };
    expect(shapeGraph([{ m: "members/1", c: "cities/1", k: null }], [city]).triples).toHaveLength(1);
  });
});

describe("terms the query fixes or leaves open", () => {
  it("draws ?s ?p ?o, reading the predicate from the row", () => {
    const pattern: Pattern = { subject: v("s"), predicate: v("p"), object: v("o") };
    const g = shapeGraph([{ s: "http://ex.org/a", p: "http://ex.org/knows", o: "http://ex.org/b" }], [pattern]);
    expect(g.triples).toEqual([{ s: "http://ex.org/a", p: "http://ex.org/knows", o: "http://ex.org/b" }]);
    expect(g.nodes.has("http://ex.org/b")).toBe(true);
  });

  it("draws an edge from a constant subject, and to a relationship's entity with no patterns of its own", () => {
    const pattern: Pattern = { subject: iri(L + "alice"), predicate: iri(L + "knows"), object: v("o"), kind: "relationship" };
    const g = shapeGraph([{ o: "members/7" }, { o: "members/7" }], [pattern]);
    expect(g.triples).toEqual([{ s: L + "alice", p: L + "knows", o: "members/7" }]); // once, though in two rows
    expect(g.nodes.has("members/7")).toBe(true);
  });
});

describe("graphFromRun", () => {
  const widened = (bindings: Array<Record<string, unknown>>, truncated = false) => ({
    bindings,
    truncated,
    graph: { patterns: PATTERNS, widened: true },
  });

  it("draws nothing from a run that could not be widened, so the rows are drawn instead", () => {
    const table = [{ m: "members/100", n: 3 }];
    expect(graphFromRun(table, { bindings: table, graph: { patterns: PATTERNS, widened: false } })).toBeNull();
    expect(graphFromRun(table, { bindings: table, graph: null })).toBeNull();
    expect(graphFromRun(ROWS, widened(ROWS))).not.toBeNull(); // positive control
  });

  it("draws nothing when the patterns find no entity in the rows", () => {
    const run = { bindings: [{ n: 3 }], graph: { patterns: PATTERNS, widened: true } };
    expect(graphFromRun([{ n: 3 }], run)).toBeNull();
  });

  it("keeps only the solutions behind the table's rows", () => {
    // The table is limited to Ada's first loan; the graph run (no limit) has both.
    const table = [{ memberName: "Ada Lovelace", bookTitle: "The Left Hand of Darkness" }];
    const drawn = graphFromRun(table, widened(ROWS));
    expect(drawn?.rows).toEqual([ROWS[0]]);
    expect(drawn?.shape.nodes.has("books/17")).toBe(false);
    expect(drawn?.truncated).toBe(false);
    expect(graphFromRun(table, widened(ROWS, true))?.truncated).toBe(true);
  });
});
