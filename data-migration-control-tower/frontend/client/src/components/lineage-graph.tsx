/**
 * A lineage graph that actually draws the lineage.
 *
 * The page used to render every catalogued table as an identical card in
 * a flat grid, and print "N relationships" above it without drawing a
 * single one. That is an inventory, not lineage — and with 48 tables it
 * was 48 identical rectangles, which is why it read as noise.
 *
 * Two properties of the real data drive the design:
 *
 *   1. **Most tables have no relationships.** 42 of 48 in the observed
 *      run. Giving an isolated table the same visual weight as a
 *      connected one buries the six edges that matter. Connected assets
 *      go in the graph; the rest go in a dense inventory below it, which
 *      is also the honest statement that nothing was found linking them.
 *
 *   2. **Edges can point at assets the catalog does not contain.** In the
 *      observed run every edge referenced SH/CO/HR schemas while the
 *      catalog held Application/Purchasing/Sales/Warehouse — the
 *      dependencies came from a SQL-view corpus and the catalog from a
 *      different source. Those endpoints are drawn as `unresolved`
 *      rather than dropped: a view referencing something discovery never
 *      catalogued is a finding, not a rendering error.
 *
 * Laid out by hand rather than with a graph library. The repo has no
 * charting dependency and this needs a deterministic layout, not a
 * force simulation that settles differently every render.
 */

import { h } from "preact";
import { useMemo, useState } from "preact/hooks";

export type LineageNode = {
  id: string;
  label?: string;
  type?: string;
  classification?: string;
};

export type LineageEdge = {
  from: string;
  to: string;
  relationship?: string;
  confidence?: number;
  source?: string;
};

type Placed = LineageNode & { x: number; y: number; layer: number; unresolved: boolean };

const NODE_W = 188;
const NODE_H = 46;
const GAP_X = 78;
const GAP_Y = 16;
const PAD = 22;

/** Longest-path layering: every edge points strictly left to right. */
function layerOf(id: string, incoming: Map<string, string[]>, seen: Set<string>): number {
  if (seen.has(id)) return 0; // a cycle cannot deepen forever
  const parents = incoming.get(id) || [];
  if (!parents.length) return 0;
  seen.add(id);
  const depth = 1 + Math.max(...parents.map((parent) => layerOf(parent, incoming, seen)));
  seen.delete(id);
  return depth;
}

export function buildLayout(nodes: LineageNode[], edges: LineageEdge[]) {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const touched = new Set<string>();
  for (const edge of edges) {
    touched.add(edge.from);
    touched.add(edge.to);
  }

  const incoming = new Map<string, string[]>();
  for (const edge of edges) {
    incoming.set(edge.to, [...(incoming.get(edge.to) || []), edge.from]);
  }

  const connected: Placed[] = [];
  const byLayer = new Map<number, Placed[]>();
  for (const id of [...touched].sort()) {
    const layer = layerOf(id, incoming, new Set());
    const known = byId.get(id);
    const placed: Placed = {
      id,
      label: known?.label || id,
      type: known?.type,
      classification: known?.classification,
      layer,
      unresolved: !known,
      x: 0,
      y: 0,
    };
    byLayer.set(layer, [...(byLayer.get(layer) || []), placed]);
    connected.push(placed);
  }

  for (const [layer, members] of byLayer) {
    members.forEach((node, index) => {
      node.x = PAD + layer * (NODE_W + GAP_X);
      node.y = PAD + index * (NODE_H + GAP_Y);
    });
  }

  const width = PAD * 2 + (Math.max(0, byLayer.size - 1) * (NODE_W + GAP_X)) + NODE_W;
  const height =
    PAD * 2 + Math.max(1, ...[...byLayer.values()].map((m) => m.length)) * (NODE_H + GAP_Y);

  return {
    connected,
    isolated: nodes.filter((node) => !touched.has(node.id)),
    positions: new Map(connected.map((node) => [node.id, node])),
    width,
    height,
  };
}

function shorten(label: string): string {
  // "Application.Countries_Archive" -> "Countries_Archive". The schema is
  // already the column heading; repeating it eats the width that the
  // distinguishing half of the name needs.
  const tail = label.includes(".") ? label.slice(label.indexOf(".") + 1) : label;
  return tail.length > 24 ? `${tail.slice(0, 23)}…` : tail;
}

export function LineageGraph({
  nodes,
  edges,
  onSelect,
}: {
  nodes: LineageNode[];
  edges: LineageEdge[];
  onSelect?: (node: LineageNode) => void;
}) {
  const [focus, setFocus] = useState<string | null>(null);
  const layout = useMemo(() => buildLayout(nodes, edges), [nodes, edges]);

  if (!layout.connected.length) {
    return (
      <p class="empty-state">
        <strong>No relationships were discovered in this run</strong>
        <span>
          {nodes.length} assets were catalogued, but nothing links them yet. Lineage
          appears once dependency evidence is found.
        </span>
      </p>
    );
  }

  const related = new Set<string>();
  if (focus) {
    related.add(focus);
    for (const edge of edges) {
      if (edge.from === focus) related.add(edge.to);
      if (edge.to === focus) related.add(edge.from);
    }
  }
  const dim = (id: string) => Boolean(focus) && !related.has(id);

  return (
    <div class="lineage-graph-scroll">
      <svg
        class="lineage-graph"
        width={layout.width}
        height={layout.height}
        // Deliberately NO viewBox. With one, the browser scales the
        // coordinate system to the element box and centres what is left
        // over — which pushed a small graph into the middle of a wide
        // panel with dead space either side. `preserveAspectRatio` is the
        // documented cure and did not take, because SVG attribute names
        // are case-sensitive and can be lowercased on the way to the DOM.
        // Without a viewBox there is nothing to scale or centre: the
        // coordinates are the pixels, and the scroll container handles
        // anything wider than the panel.
        style={{ width: layout.width, height: layout.height }}
        role="img"
        aria-label={`Lineage graph: ${layout.connected.length} related assets and ${edges.length} relationships. The relationship register below lists them as text.`}
      >
        <defs>
          <marker id="lineage-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
            <path d="M0 0 L8 4 L0 8 z" fill="currentColor" />
          </marker>
        </defs>

        {edges.map((edge) => {
          const from = layout.positions.get(edge.from);
          const to = layout.positions.get(edge.to);
          if (!from || !to) return null;
          const x1 = from.x + NODE_W;
          const y1 = from.y + NODE_H / 2;
          const x2 = to.x;
          const y2 = to.y + NODE_H / 2;
          const mid = (x1 + x2) / 2;
          const active = focus && (edge.from === focus || edge.to === focus);
          return (
            <path
              key={`${edge.from}->${edge.to}`}
              class={`lineage-edge ${active ? "is-active" : ""} ${focus && !active ? "is-dim" : ""}`}
              d={`M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`}
              markerEnd="url(#lineage-arrow)"
            />
          );
        })}

        {layout.connected.map((node) => (
          <g
            key={node.id}
            class={`lineage-g-node ${node.unresolved ? "is-unresolved" : ""} ${
              node.classification === "PII" ? "is-pii" : ""
            } ${dim(node.id) ? "is-dim" : ""}`}
            transform={`translate(${node.x},${node.y})`}
            tabIndex={0}
            role="button"
            aria-label={`${node.label}${node.classification ? `, ${node.classification}` : ""}${
              node.unresolved ? ", referenced but not catalogued" : ""
            }`}
            onMouseEnter={() => setFocus(node.id)}
            onMouseLeave={() => setFocus(null)}
            onFocus={() => setFocus(node.id)}
            onBlur={() => setFocus(null)}
            onClick={() => onSelect?.(node)}
            onKeyDown={(event: any) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onSelect?.(node);
              }
            }}
          >
            <rect width={NODE_W} height={NODE_H} rx="6" />
            <text class="lineage-g-label" x="12" y="20">
              {shorten(String(node.label))}
            </text>
            <text class="lineage-g-meta" x="12" y="35">
              {node.unresolved
                ? "referenced, not catalogued"
                : node.classification || node.type || "asset"}
            </text>
          </g>
        ))}
      </svg>
    </div>
  );
}
