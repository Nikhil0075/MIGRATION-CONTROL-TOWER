/**
 * Per-agent visual identity.
 *
 * The registry treats agents as interchangeable capability providers,
 * which is correct for dispatch and useless for an operator: every agent
 * rendered as an identical table row, so "which agent is doing this and
 * what is it for" was answerable only by reading an id string. This gives
 * each one a face, a colour and one line of purpose.
 *
 * Deliberately NOT modelled on `Icon` in icons.tsx, which resolves an
 * unknown name to `PATHS.overview` and renders something plausible. That
 * is a reasonable default for a decorative glyph and a bad one for
 * identity: an unregistered agent would silently borrow another agent's
 * face, and the console would confidently mislabel it. `agentIdentity`
 * returns null instead, and the badge renders a visible unknown state.
 *
 * Accents are literal hex rather than tokens because they belong to the
 * agents, not to the theme — the theme has three brand colours and there
 * are seven agents. Each is chosen to clear WCAG AA against the white and
 * off-white surfaces these badges sit on.
 */

import { h } from "preact";

export type AgentIdentity = {
  /** Short human name. The registry id ("risk-agent") is not it. */
  label: string;
  /** One line: what this agent is for, in an operator's words. */
  blurb: string;
  accent: string;
  art: string;
};

const ART = "/assets/brand/v1/agents";

export const AGENT_IDENTITY: Record<string, AgentIdentity> = {
  "discovery-agent": {
    label: "Discovery",
    blurb: "Catalogues the estate: tables, columns, pipelines.",
    accent: "#0f766e",
    art: `${ART}/discovery-agent.png`,
  },
  "lineage-agent": {
    label: "Lineage",
    blurb: "Maps what depends on what, and how confidently.",
    accent: "#1d6fd0",
    art: `${ART}/lineage-agent.png`,
  },
  "risk-agent": {
    label: "Risk & Compliance",
    blurb: "Classifies sensitive data and flags what could go wrong.",
    accent: "#b45309",
    art: `${ART}/risk-agent.png`,
  },
  "planner-agent": {
    label: "Planner",
    blurb: "Derives the migration plan from discovered metadata.",
    accent: "#4338ca",
    art: `${ART}/planner-agent.png`,
  },
  "validation-agent": {
    label: "Validation",
    blurb: "Proves source and target match, deterministically.",
    accent: "#15803d",
    art: `${ART}/validation-agent.png`,
  },
  "cutover-agent": {
    label: "Cutover",
    blurb: "Requests human approval, then switches over.",
    accent: "#0e7490",
    art: `${ART}/cutover-agent.png`,
  },
  "finance-impact-agent": {
    label: "Finance Impact",
    blurb: "A different department's agent, assessing reporting impact.",
    accent: "#7e22ce",
    art: `${ART}/finance-impact-agent.png`,
  },
};

/** Null for an unregistered agent — never another agent's identity. */
export function agentIdentity(agentId: string): AgentIdentity | null {
  return AGENT_IDENTITY[agentId] ?? null;
}

export function AgentBadge({
  agentId,
  size = 48,
  showBlurb = true,
}: {
  agentId: string;
  size?: number;
  showBlurb?: boolean;
}) {
  const identity = agentIdentity(agentId);

  if (!identity) {
    // Says what it is rather than guessing. An agent can legitimately be
    // here without art — a new department seeds its own card — and that
    // should read as "not styled yet", not as a broken page.
    return (
      <span class="agent-badge agent-badge-unknown">
        <span class="agent-badge-art" style={{ width: size, height: size }} aria-hidden="true" />
        <span class="agent-badge-text">
          <strong>{agentId}</strong>
          {showBlurb && <small>Not in the icon set yet.</small>}
        </span>
      </span>
    );
  }

  return (
    <span class="agent-badge" style={{ "--agent-accent": identity.accent }}>
      <img
        class="agent-badge-art"
        src={identity.art}
        alt=""
        aria-hidden="true"
        width={size}
        height={size}
      />
      <span class="agent-badge-text">
        <strong>{identity.label}</strong>
        {showBlurb && <small>{identity.blurb}</small>}
      </span>
    </span>
  );
}
