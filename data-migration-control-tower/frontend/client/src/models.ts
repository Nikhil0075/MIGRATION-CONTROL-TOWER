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
}

export interface RuntimeConfig {
  product_name: string;
  build_version: string;
  poll_interval_ms: number;
  authentication_configured: boolean;
  firebase: Record<string, string>;
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
