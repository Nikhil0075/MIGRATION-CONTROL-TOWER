import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { cleanup, render, screen } from "@testing-library/preact";
import { h } from "preact";
import { afterEach, describe, expect, it } from "vitest";
import { AGENT_IDENTITY, AgentBadge, agentIdentity } from "./agents";

afterEach(cleanup);

const REPO = resolve(__dirname, "..", "..", "..", "..");

function seededAgentIds(file: string): string[] {
  const source = readFileSync(resolve(REPO, "infrastructure", file), "utf8");
  return [...source.matchAll(/"agent_id":\s*"([a-z-]+)"/g)].map((m) => m[1]);
}

describe("agent identity", () => {
  it("covers every agent the registry actually seeds", () => {
    // The registry is the source of truth for which agents exist. If a
    // department seeds a new one and nobody adds an identity, this fails
    // here rather than shipping a nameless row to an operator.
    const seeded = new Set([
      ...seededAgentIds("seed_registry.py"),
      "finance-impact-agent", // seed_finance_agent.py assigns via a constant
    ]);
    const missing = [...seeded].filter((id) => !(id in AGENT_IDENTITY));
    expect(missing).toEqual([]);
  });

  it("describes no agent that is not seeded", () => {
    // The other direction: a renamed agent leaves an identity pointing at
    // nothing, and the fleet silently loses a card.
    const seeded = new Set([...seededAgentIds("seed_registry.py"), "finance-impact-agent"]);
    const orphans = Object.keys(AGENT_IDENTITY).filter((id) => !seeded.has(id));
    expect(orphans).toEqual([]);
  });

  it("gives every agent a distinct accent, so colour actually distinguishes them", () => {
    const accents = Object.values(AGENT_IDENTITY).map((a) => a.accent);
    expect(new Set(accents).size).toBe(accents.length);
  });

  it("points every agent at a local asset", () => {
    // img-src is 'self'; a remote URL renders as nothing.
    for (const [id, identity] of Object.entries(AGENT_IDENTITY)) {
      expect(identity.art, id).toMatch(/^\/assets\/brand\//);
    }
  });
});

describe("unknown agents", () => {
  it("returns null rather than another agent's identity", () => {
    // icons.tsx resolves an unknown name to PATHS.overview, which is fine
    // for a decorative glyph and wrong for identity — it would let the
    // console confidently label one agent with another's face.
    expect(agentIdentity("nope-agent")).toBeNull();
  });

  it("renders a visible unknown state instead of a plausible wrong one", () => {
    render(<AgentBadge agentId="brand-new-agent" />);
    expect(screen.getByText("brand-new-agent")).toBeTruthy();
    expect(screen.getByText(/Not in the icon set yet/)).toBeTruthy();
  });

  it("shows the human label for a known agent, not the registry id", () => {
    render(<AgentBadge agentId="risk-agent" />);
    expect(screen.getByText("Risk & Compliance")).toBeTruthy();
    expect(screen.queryByText("risk-agent")).toBeNull();
  });
});
