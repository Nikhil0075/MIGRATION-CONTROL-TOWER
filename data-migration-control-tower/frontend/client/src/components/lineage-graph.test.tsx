import { cleanup, render, screen } from "@testing-library/preact";
import { h } from "preact";
import { afterEach, describe, expect, it } from "vitest";
import { LineageGraph, buildLayout } from "./lineage-graph";

afterEach(cleanup);

const nodes = [
  { id: "a", label: "Sales.Orders", classification: "METADATA" },
  { id: "b", label: "Sales.Customers", classification: "PII" },
  { id: "c", label: "Sales.V_SUMMARY", classification: "METADATA" },
  { id: "lonely", label: "Warehouse.Cold", classification: "METADATA" },
];
const edges = [
  { from: "a", to: "c", relationship: "reads" },
  { from: "b", to: "c", relationship: "reads" },
];

describe("layout", () => {
  it("puts sources left of what they feed", () => {
    const layout = buildLayout(nodes, edges);
    const at = (id: string) => layout.positions.get(id)!;
    expect(at("a").layer).toBe(0);
    expect(at("b").layer).toBe(0);
    expect(at("c").layer).toBe(1);
    expect(at("c").x).toBeGreaterThan(at("a").x);
  });

  it("separates assets with no relationships from the graph", () => {
    // 42 of 48 assets were unconnected in the observed run. Drawing them
    // at the same weight as connected ones is what buried the six edges
    // that are the actual lineage.
    const layout = buildLayout(nodes, edges);
    expect(layout.connected.map((n) => n.id).sort()).toEqual(["a", "b", "c"]);
    expect(layout.isolated.map((n) => n.id)).toEqual(["lonely"]);
  });

  it("keeps an endpoint the catalog does not contain, and marks it", () => {
    // Real case: every edge in one run referenced SH/CO/HR schemas while
    // the catalog held Application/Sales/… A view pointing at something
    // discovery never catalogued is a finding, not a rendering error, so
    // it is drawn rather than dropped.
    const layout = buildLayout(nodes, [...edges, { from: "a", to: "SH.TIMES" }]);
    const external = layout.positions.get("SH.TIMES")!;
    expect(external).toBeTruthy();
    expect(external.unresolved).toBe(true);
    expect(layout.positions.get("a")!.unresolved).toBe(false);
  });

  it("does not hang on a cycle", () => {
    const cyclic = [
      { from: "x", to: "y" },
      { from: "y", to: "x" },
    ];
    expect(() => buildLayout([{ id: "x" }, { id: "y" }], cyclic)).not.toThrow();
  });
});

describe("rendering", () => {
  it("draws one path per relationship", () => {
    const { container } = render(<LineageGraph nodes={nodes} edges={edges} />);
    expect(container.querySelectorAll("path.lineage-edge")).toHaveLength(2);
  });

  it("carries a text alternative, since the graph itself is an image", () => {
    render(<LineageGraph nodes={nodes} edges={edges} />);
    const svg = screen.getByRole("img");
    expect(svg.getAttribute("aria-label")).toMatch(/2 relationships/);
    expect(svg.getAttribute("aria-label")).toMatch(/relationship register/i);
  });

  it("names PII in each node's accessible name, not only by colour", () => {
    render(<LineageGraph nodes={nodes} edges={edges} />);
    expect(screen.getByRole("button", { name: /Sales.Customers, PII/ })).toBeTruthy();
  });

  it("says so plainly when a run catalogued assets but found no links", () => {
    render(<LineageGraph nodes={nodes} edges={[]} />);
    expect(screen.getByText(/No relationships were discovered/)).toBeTruthy();
    expect(screen.getByText(/4 assets were catalogued/)).toBeTruthy();
  });
});
