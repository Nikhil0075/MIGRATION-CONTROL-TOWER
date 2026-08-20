export type Role = "viewer" | "operator" | "approver";

export interface Meta {
  generated_at: string;
  freshness: "live" | "cached" | "stale";
  total?: number | null;
  next_cursor?: string | null;
}

export interface Envelope<T = unknown> {
  data: T;
  meta: Meta;
}

export interface Session {
  uid: string;
  email: string;
  roles: Role[];
  estate_roles: Record<string, Role[]>;
  wildcard_roles: Role[];
  scoped_estates: string[];
}

export interface RuntimeConfig {
  product_name: string;
  build_version: string;
  poll_interval_ms: number;
  progress_poll_interval_ms: number;
  environment: string;
  authentication_configured: boolean;
  firebase: Record<string, string>;
  features?: {
    agent_reasoning: boolean;
    reports: boolean;
    assistant: boolean;
  };
}

export interface EstateSummary {
  estate_id: string;
  display_name: string;
  status: string;
  sources: Array<Record<string, unknown>>;
  pipeline_options?: Array<{ pipeline_id: string; name: string }>;
  execution_readiness?: {
    status: "ready" | "blocked" | "selection_required";
    options: Array<{ source_id: string; pack_id: string; label: string }>;
    blockers: Array<{ code: string; message: string }>;
  };
}

export interface ProgressSnapshot {
  percent: number;
  status: "queued" | "active" | "waiting" | "held" | "failed" | "complete";
  label: string;
  current_stage: string;
  completed_units: number;
  total_units: number;
  run_id?: string | null;
  last_observed_at: string;
}

export interface NavItem {
  route: string;
  label: string;
  icon: string;
  description: string;
}

export interface Column<T = Record<string, unknown>> {
  key: string;
  label: string;
  value?: (row: T) => unknown;
  priority?: "primary" | "secondary";
  status?: boolean;
}
