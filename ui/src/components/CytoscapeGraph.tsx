import { useEffect, useMemo, useRef, useState, useCallback } from "react";
import cytoscape from "cytoscape";
import type { Core, EventObject, NodeSingular } from "cytoscape";
import { CATEGORICAL, graphPalette, type GraphPalette } from "../theme/graphPalette";
import { useTheme } from "../theme/theme";
import type { ShapedGraph } from "../utils/graphShape";

// CytoscapeGraph — RDF-aware variant of the Cypher UI's CytoscapeGraph.
//
// The key UX delta vs the Cypher project (and the reason this lives in
// `arango-sparql-py` instead of being a verbatim port) is that strict
// RDF promotes every literal value to a node in the graph. Rendering
// SPARQL bindings naively that way produces a hairball of one-off
// literal nodes (every name string, every age integer, etc.) that
// drowns out the actual entity-to-entity edges.
//
// Default render mode is therefore "literal collapse":
//
//   * Each subject IRI becomes one node.
//   * Predicates pointing at literals become key/value pairs on the
//     subject node's data dict (surfaced in the hover tooltip and a
//     side panel).
//   * Predicates pointing at other IRIs become edges between the two
//     subject nodes, labelled with the local name of the predicate.
//
// A "Show literals as nodes" toggle opts back into the strict RDF view
// for power users — see the toggle in the toolbar above the canvas.
//
// Heuristic for converting a SPARQL solution sequence into RDF triples
// without an explicit triple-shaped query:
//
//   1. If every row has the same three projection variables and the
//      column names look triple-shaped (anything matching /^s|p|o$/i,
//      `subject`/`predicate`/`object`, or the first three vars in
//      order), treat them as ?s ?p ?o triples.
//   2. Otherwise, treat each row as one record about a single subject:
//      the first IRI-shaped cell is the subject; every remaining cell
//      becomes a triple where the predicate is the *column name*.
//
// This mirrors what users actually write in practice — mostly
// `SELECT ?person ?name ?friend` rather than the explicit
// `SELECT ?s ?p ?o` shape.

export type SparqlRow = Record<string, unknown>;

// CyNode mirrors the shape ResultsPanel's NodeInspector expects: an
// IRI subject node, its display label, a colour token, and the
// collapsed property bag (literal triples folded onto the subject).
// Edge clicks aren't surfaced today — RDF property names are already
// shown on the edges and the Cypher UI's edge inspector wasn't ported.
export interface CyNode {
  id: string;
  label: string;
  color?: string;
  data: Record<string, unknown>;
}

interface Props {
  bindings: SparqlRow[];
  /** The graph drawn from the query's own patterns (`utils/graphShape.ts`): which values are
   * entities, how they connect, their labels and classes. Without it the rows are drawn by
   * the shape heuristics below. */
  shape?: ShapedGraph;
  onNodeClick?: (node: CyNode) => void;
  onBackgroundClick?: () => void;
}

// Namespace colours: the Arango categorical set (theme-independent mid-tones
// that read on both canvases; brand green first, red reserved for errors).
const NODE_COLORS = CATEGORICAL;

// Literals stay neutral so entity colours carry the meaning (same muted grey
// in day and night palettes).
const LITERAL_COLOR = "#9a9a9a";

// Labels, edges and selection come from the active theme (ported from
// arango-cypher-py's graphStyle) so they stay legible on the canvas.
function graphStyle(p: GraphPalette): cytoscape.StylesheetJson {
  return [
    {
      selector: "node",
      style: {
        label: "data(label)",
        "background-color": "data(color)",
        "text-valign": "bottom",
        "text-halign": "center",
        "font-size": "10px",
        color: p.text,
        "text-margin-y": 6,
        width: 36,
        height: 36,
        "border-width": 2,
        "border-color": "data(color)",
        "border-opacity": 0.4,
        "text-max-width": "120px",
        "text-wrap": "ellipsis",
      },
    },
    {
      selector: "node.literal",
      style: {
        shape: "round-rectangle",
        "background-opacity": 0.6,
        width: "label",
        height: 20,
        padding: "4px",
        "border-width": 1,
        "border-style": "dashed",
        "font-size": "9px",
      },
    },
    {
      selector: "node.selected",
      style: {
        "border-width": 3,
        "border-color": p.accent,
        "border-opacity": 1,
      },
    },
    {
      selector: "edge",
      style: {
        width: 1.5,
        "line-color": p.edge,
        "target-arrow-color": p.edge,
        "target-arrow-shape": "triangle",
        "curve-style": "bezier",
        label: "data(label)",
        "font-size": "9px",
        color: p.textSecondary,
        "text-background-color": p.canvas,
        "text-background-opacity": 0.85,
        "text-background-padding": "3px",
        "text-rotation": "autorotate",
      },
    },
  ];
}

function isIri(value: unknown): value is string {
  if (typeof value !== "string") return false;
  return (
    value.startsWith("http://") ||
    value.startsWith("https://") ||
    value.startsWith("urn:") ||
    /^[a-zA-Z][a-zA-Z0-9+.-]*:[^\s]/.test(value)
  );
}

function localName(iri: string): string {
  const hashIdx = iri.lastIndexOf("#");
  if (hashIdx >= 0 && hashIdx < iri.length - 1) return iri.slice(hashIdx + 1);
  const slashIdx = iri.lastIndexOf("/");
  if (slashIdx >= 0 && slashIdx < iri.length - 1) return iri.slice(slashIdx + 1);
  const colonIdx = iri.lastIndexOf(":");
  if (colonIdx >= 0 && colonIdx < iri.length - 1) return iri.slice(colonIdx + 1);
  return iri;
}

function literalRepr(value: unknown): string {
  if (value === null) return "null";
  if (value === undefined) return "—";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

interface Triple {
  s: string; // subject IRI
  p: string; // predicate (IRI when known, else SPARQL variable name)
  o: unknown; // object — IRI string or literal value
}

// Return the Triple list extracted from a row sequence. Heuristic
// selection of the triple shape lives here so the rendering layer can
// stay schema-agnostic — it just walks Triples and decides which
// nodes/edges to materialise based on the literal-collapse toggle.
function extractTriples(rows: SparqlRow[]): Triple[] {
  if (rows.length === 0) return [];
  const cols = Object.keys(rows[0]);

  // Triple-shaped query: ?s ?p ?o (any case, or `subject`/`predicate`/`object`)
  const tripleShaped =
    cols.length === 3 &&
    /^(s|subject|src|source)$/i.test(cols[0]) &&
    /^(p|predicate|prop|property|edge)$/i.test(cols[1]) &&
    /^(o|object|tgt|target|value)$/i.test(cols[2]);

  if (tripleShaped) {
    const [sCol, pCol, oCol] = cols;
    const triples: Triple[] = [];
    for (const row of rows) {
      const s = row[sCol];
      const p = row[pCol];
      const o = row[oCol];
      if (typeof s === "string" && (typeof p === "string" || typeof p === "number" || isIri(p))) {
        triples.push({ s, p: String(p), o });
      }
    }
    return triples;
  }

  // Record-per-row shape: pick first IRI-valued cell as subject, every
  // other column becomes a predicate (column name) → object triple.
  // If no IRI is present in a row, synthesise a blank-node-style id
  // so the row still renders as a single subject node.
  const triples: Triple[] = [];
  rows.forEach((row, rowIdx) => {
    let subject: string | null = null;
    for (const col of cols) {
      if (isIri(row[col])) {
        subject = row[col] as string;
        break;
      }
    }
    if (!subject) {
      subject = `_:row${rowIdx}`;
    }
    for (const col of cols) {
      const value = row[col];
      if (value === undefined || value === null) continue;
      if (value === subject) continue; // skip the subject column itself
      triples.push({ s: subject, p: col, o: value });
    }
  });
  return triples;
}

interface CyElement {
  data: Record<string, unknown>;
  classes?: string;
}

interface BuildResult {
  elements: CyElement[];
  // Keyed by node id; populated for IRI subjects so the hover tooltip
  // and side panel can surface collapsed literal properties without
  // walking the full triple list again.
  literalProps: Map<string, Array<{ predicate: string; value: unknown }>>;
}

interface BuildOptions {
  /** Whether a value is drawn as a node; by default, an IRI. */
  isNode?: (value: unknown) => value is string;
  labels?: Map<string, string>;
  /** A node's class: it colours the node, in place of its namespace. */
  classes?: Map<string, string>;
}

function buildElements(
  triples: Triple[],
  collapseLiterals: boolean,
  { isNode = isIri, labels, classes }: BuildOptions = {},
): BuildResult {
  // Subject IRIs and any IRI that appears as object should be a node.
  const subjectIris = new Set<string>();
  const literalNodes = new Map<string, { predicate: string; value: unknown }>();
  const literalProps = new Map<string, Array<{ predicate: string; value: unknown }>>();
  const collColors = new Map<string, string>();
  let colorIdx = 0;

  for (const t of triples) {
    subjectIris.add(t.s);
    if (isNode(t.o)) subjectIris.add(t.o);
  }

  const colorFor = (iri: string): string => {
    // group by host or namespace prefix so nodes from the same
    // ontology share a colour family — matches the Cypher graph's
    // "collection → colour" intuition.
    const ns = classes?.get(iri) ?? iri.replace(/[^/#]+$/, "");
    if (!collColors.has(ns)) {
      collColors.set(ns, NODE_COLORS[colorIdx++ % NODE_COLORS.length]);
    }
    return collColors.get(ns)!;
  };

  const elements: CyElement[] = [];

  for (const iri of subjectIris) {
    elements.push({
      data: {
        id: iri,
        label: labels?.get(iri) ?? (classes?.get(iri) ? `${classes.get(iri)} ${localName(iri)}` : localName(iri)),
        iri,
        color: colorFor(iri),
        kind: "iri",
      },
    });
  }

  triples.forEach((t, idx) => {
    const predLabel = isIri(t.p) ? localName(t.p) : t.p;
    if (isNode(t.o)) {
      elements.push({
        data: {
          id: `e${idx}`,
          source: t.s,
          target: t.o,
          label: predLabel,
          predicate: t.p,
        },
      });
      return;
    }
    if (collapseLiterals) {
      const list = literalProps.get(t.s) ?? [];
      list.push({ predicate: predLabel, value: t.o });
      literalProps.set(t.s, list);
      return;
    }
    // strict RDF mode — materialise the literal as its own node
    const litId = `_lit:${idx}`;
    literalNodes.set(litId, { predicate: predLabel, value: t.o });
    elements.push({
      data: {
        id: litId,
        label: literalRepr(t.o),
        color: LITERAL_COLOR,
        kind: "literal",
      },
      classes: "literal",
    });
    elements.push({
      data: {
        id: `e${idx}`,
        source: t.s,
        target: litId,
        label: predLabel,
        predicate: t.p,
      },
    });
  });

  // Stamp collapsed literal props back onto the subject nodes so the
  // tooltip can read them directly from cy.data() without an external
  // lookup.
  for (const el of elements) {
    if (el.data.kind !== "iri") continue;
    const props = literalProps.get(el.data.id as string);
    if (props && props.length > 0) {
      el.data.props = props;
    }
  }

  return { elements, literalProps };
}

const COSE_LAYOUT = {
  name: "cose",
  animate: false,
  nodeRepulsion: () => 8000,
  idealEdgeLength: () => 120,
  gravity: 0.3,
  padding: 40,
} as cytoscape.LayoutOptions;

export default function CytoscapeGraph({
  bindings,
  shape,
  onNodeClick,
  onBackgroundClick,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const [collapseLiterals, setCollapseLiterals] = useState(true);
  const [hoverInfo, setHoverInfo] = useState<{
    x: number;
    y: number;
    iri: string;
    label: string;
    props: Array<{ predicate: string; value: unknown }>;
  } | null>(null);

  const onNodeClickRef = useRef(onNodeClick);
  onNodeClickRef.current = onNodeClick;
  const onBackgroundClickRef = useRef(onBackgroundClick);
  onBackgroundClickRef.current = onBackgroundClick;

  const [theme] = useTheme();
  const palette = graphPalette(theme);
  // The canvas is built once (mount effect); the ref hands it the palette
  // current at build time, and the restyle effect below keeps it fresh.
  const paletteRef = useRef(palette);

  const triples = useMemo(() => shape?.triples ?? extractTriples(bindings), [bindings, shape]);
  const { elements } = useMemo(
    () =>
      buildElements(
        triples,
        collapseLiterals,
        shape
          ? {
              isNode: (value: unknown): value is string => typeof value === "string" && shape.nodes.has(value),
              labels: shape.labels,
              classes: shape.classes,
            }
          : {},
      ),
    [triples, collapseLiterals, shape],
  );

  const showHover = useCallback((node: NodeSingular) => {
    const pos = node.renderedPosition();
    const props = (node.data("props") as
      | Array<{ predicate: string; value: unknown }>
      | undefined) ?? [];
    setHoverInfo({
      x: pos.x,
      y: pos.y,
      iri: (node.data("iri") as string) || "",
      label: node.data("label") as string,
      props,
    });
  }, []);

  useEffect(() => {
    if (!containerRef.current) return;
    const cy = cytoscape({
      container: containerRef.current,
      elements: [],
      layout: COSE_LAYOUT,
      style: graphStyle(paletteRef.current),
      minZoom: 0.15,
      maxZoom: 5,
    });

    cy.on("tap", "node", (evt: EventObject) => {
      const node = evt.target as NodeSingular;
      cy.nodes().removeClass("selected");
      node.addClass("selected");
      showHover(node);
      const d = node.data() as Record<string, unknown>;
      // Strip cytoscape-internal fields before surfacing to the
      // inspector — the panel renders Object.entries(node.data) so
      // we want it to see the user-meaningful keys, not `id`/`label`/
      // `color` (which it already gets at the top level).
      const cyNode: CyNode = {
        id: String(d.id),
        label: String(d.label),
        color: typeof d.color === "string" ? d.color : undefined,
        data: d,
      };
      onNodeClickRef.current?.(cyNode);
    });

    cy.on("mouseover", "node", (evt: EventObject) => {
      const node = evt.target as NodeSingular;
      showHover(node);
      containerRef.current!.style.cursor = "pointer";
    });

    cy.on("mouseout", "node", () => {
      setHoverInfo(null);
      containerRef.current!.style.cursor = "default";
    });

    cy.on("tap", (evt: EventObject) => {
      if (evt.target === cy) {
        cy.nodes().removeClass("selected");
        setHoverInfo(null);
        onBackgroundClickRef.current?.();
      }
    });

    cyRef.current = cy;
    return () => {
      cy.destroy();
      cyRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A theme toggle restyles in place: positions, zoom and selection survive.
  useEffect(() => {
    paletteRef.current = palette;
    cyRef.current?.style(graphStyle(palette));
  }, [palette]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.elements().remove();
    cy.add(elements);
    cy.layout(COSE_LAYOUT).run();
  }, [elements]);

  const isEmpty = triples.length === 0;

  return (
    <div className="relative w-full h-full">
      <div className="absolute top-2 left-2 z-20 flex items-center gap-2">
        <label
          className="flex items-center gap-1.5 px-2 py-1 rounded bg-gray-800/80 border border-gray-700 text-xs text-gray-300 cursor-pointer select-none"
          title="When ON, RDF literal values become properties on the subject node. When OFF, every literal becomes its own node (strict RDF view)."
        >
          <input
            type="checkbox"
            checked={collapseLiterals}
            onChange={(e) => setCollapseLiterals(e.target.checked)}
            className="w-3.5 h-3.5 rounded border-gray-600 bg-gray-900 text-indigo-500 focus:ring-1 focus:ring-indigo-500 focus:ring-offset-0 cursor-pointer"
          />
          Collapse literals
        </label>
        <span className="px-2 py-1 rounded bg-gray-800/60 border border-gray-700 text-[10px] text-gray-500 tabular-nums">
          {bindings.length} row{bindings.length === 1 ? "" : "s"} ·{" "}
          {triples.length} triple{triples.length === 1 ? "" : "s"}
        </span>
      </div>
      <div
        ref={containerRef}
        className="w-full h-full bg-gray-900 rounded border border-gray-800"
      />
      {isEmpty && (
        <div className="absolute inset-0 flex items-center justify-center text-xs text-gray-500 pointer-events-none">
          No graph to render — execute a SPARQL query to see the result graph.
        </div>
      )}
      {hoverInfo && (
        <div
          className="absolute pointer-events-none px-2 py-1.5 rounded bg-gray-800 text-xs text-gray-200 border border-gray-700 shadow-lg z-10 max-w-xs"
          style={{ left: hoverInfo.x + 20, top: hoverInfo.y - 10 }}
        >
          <div className="font-semibold text-indigo-300 truncate">
            {hoverInfo.label}
          </div>
          {hoverInfo.iri && (
            <div className="text-[10px] text-gray-500 break-all">
              {hoverInfo.iri}
            </div>
          )}
          {hoverInfo.props.length > 0 && (
            <div className="mt-1 pt-1 border-t border-gray-700 space-y-0.5">
              {hoverInfo.props.slice(0, 8).map((p, i) => (
                <div key={i} className="flex items-baseline gap-1.5">
                  <span className="text-gray-500">{p.predicate}:</span>
                  <span className="text-gray-200 truncate">
                    {literalRepr(p.value)}
                  </span>
                </div>
              ))}
              {hoverInfo.props.length > 8 && (
                <div className="text-[10px] text-gray-500 italic">
                  …{hoverInfo.props.length - 8} more
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// Internal helpers exported for unit tests — not part of the public API.
export const __test__ = { extractTriples, buildElements, isIri, localName };
