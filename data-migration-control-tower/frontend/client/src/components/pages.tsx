import { h } from "preact";
import { ProgressBar } from "oj-c/progress-bar";
import { lazy, Suspense } from "preact/compat";
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { api, idempotencyKey } from "../api";
import { Column, EstateSummary, ProgressSnapshot, Role, Session } from "../models";
import { formatValue, statusTone } from "../status";
import { Icon } from "./icons";
import { AgentBadge } from "./agents";
import { OrchestrationMap } from "./orchestration";

type RecordRow = Record<string, any>;
export type PageProps = {
  route: string;
  session: Session;
  onInspect: (title: string, value: unknown) => void;
  navigate: (route: string) => void;
  activeEstateId?: string | null;
  activeEstate?: EstateSummary | null;
  estateRoles?: Role[];
  onEstateCreated?: (estate: EstateSummary) => void;
  progressPollIntervalMs?: number;
};

export function estatePath(path: string, estateId?: string | null): string {
  if (!estateId) return path;
  const separator = path.includes("?") ? "&" : "?";
  return `${path}${separator}estate_id=${encodeURIComponent(estateId)}`;
}

export function LifecycleProgress({ progress, compact = false }: { progress?: ProgressSnapshot | null; compact?: boolean }) {
  if (!progress) return <span class="unavailable">Not observed</span>;
  return (
    <div class={`lifecycle-progress ${compact ? "compact" : ""}`} aria-label={`${progress.label}: ${progress.percent}%`}>
      <div class="progress-heading">
        <StatusPill value={progress.status} />
        <strong>{progress.percent}%</strong>
      </div>
      <ProgressBar value={progress.percent} max={100} aria-label={progress.label} />
      <div class="progress-caption"><span>{progress.label}</span><small>{progress.completed_units} of {progress.total_units} milestones</small></div>
    </div>
  );
}

function useOperationProgress(operationId: string | null, intervalMs: number) {
  const [progress, setProgress] = useState<ProgressSnapshot | null>(null);
  useEffect(() => {
    if (!operationId) return;
    let active = true;
    let timer: number | undefined;
    const poll = async () => {
      if (document.hidden) {
        timer = window.setTimeout(poll, intervalMs);
        return;
      }
      try {
        const result = await api<any>(`/api/v1/operations/${encodeURIComponent(operationId)}`);
        if (!active) return;
        setProgress(result.data.progress);
        if (!["complete", "failed"].includes(result.data.progress?.status)) timer = window.setTimeout(poll, intervalMs);
      } catch {
        if (active) timer = window.setTimeout(poll, intervalMs);
      }
    };
    void poll();
    return () => { active = false; if (timer) window.clearTimeout(timer); };
  }, [operationId, intervalMs]);
  return progress;
}

export function StatusPill({ value }: { value: unknown }) {
  const label = String(value || "UNKNOWN").replaceAll("_", " ");
  return (
    <span class={`status-pill status-${statusTone(value)}`}>
      <span class="status-dot" aria-hidden="true" />
      <span>{label}</span>
    </span>
  );
}

export function useResource<T = any>(path: string, refreshKey = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [generatedAt, setGeneratedAt] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    api<T>(path)
      .then((result) => {
        if (!active) return;
        setData(result.data);
        setGeneratedAt(result.meta.generated_at);
      })
      .catch((reason) => active && setError(reason.message || String(reason)))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [path, refreshKey]);
  return { data, error, loading, generatedAt };
}

export function PageState({
  loading,
  error,
}: {
  loading: boolean;
  error: string | null;
}) {
  if (loading)
    return (
      <div class="page-state">
        <ProgressBar value={-1} aria-label="Loading operational data" />
        <span>Loading operational data…</span>
      </div>
    );
  if (error)
    return (
      <div class="page-state page-error">
        <strong>Unable to load this workspace.</strong>
        <span>{error}</span>
      </div>
    );
  return null;
}

export function PageHeader({
  title,
  description,
  generatedAt,
  actions,
}: {
  title: string;
  description: string;
  generatedAt?: string | null;
  actions?: any;
}) {
  return (
    <div class="page-header">
      <div>
        <p class="eyebrow">Operational workspace</p>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      <div class="page-actions">
        {generatedAt && (
          <span class="freshness">Updated {formatValue(generatedAt)}</span>
        )}
        {actions}
      </div>
    </div>
  );
}

export function MetricCard({
  label,
  value,
  detail,
  status,
}: {
  label: string;
  value: unknown;
  detail?: string;
  status?: unknown;
}) {
  return (
    <article class="metric-card">
      <div class="metric-label">{label}</div>
      <div class="metric-value">{formatValue(value)}</div>
      <div class="metric-detail">
        {status ? (
          <StatusPill value={status} />
        ) : (
          detail || "Current observed value"
        )}
      </div>
    </article>
  );
}

export function Panel({
  title,
  subtitle,
  children,
  className = "",
}: {
  title: string;
  subtitle?: string;
  children: any;
  className?: string;
}) {
  return (
    <section class={`surface ${className}`}>
      <div class="surface-header">
        <div>
          <h2>{title}</h2>
          {subtitle && <p>{subtitle}</p>}
        </div>
      </div>
      <div class="surface-body">{children}</div>
    </section>
  );
}

export function DataTable({
  rows,
  columns,
  onRow,
  label,
}: {
  rows: RecordRow[];
  columns: Column<RecordRow>[];
  onRow?: (row: RecordRow) => void;
  label: string;
}) {
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState(columns[0]?.key || "");
  const [sortDirection, setSortDirection] = useState<"asc" | "desc">("asc");
  const [page, setPage] = useState(0);
  const [visible, setVisible] = useState(
    () => new Set(columns.map((column) => column.key)),
  );
  const pageSize = 15;
  const value = (row: RecordRow, column: Column<RecordRow>) =>
    column.value ? column.value(row) : row[column.key];
  const filtered = useMemo(() => {
    const term = search.trim().toLowerCase();
    const result = term
      ? rows.filter((row) =>
          columns.some((column) =>
            formatValue(value(row, column)).toLowerCase().includes(term),
          ),
        )
      : [...rows];
    const column = columns.find((item) => item.key === sortKey);
    if (column)
      result.sort((a, b) =>
        formatValue(value(a, column)).localeCompare(
          formatValue(value(b, column)),
          undefined,
          { numeric: true },
        ),
      );
    if (sortDirection === "desc") result.reverse();
    return result;
  }, [rows, search, sortKey, sortDirection]);
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const pageRows = filtered.slice(page * pageSize, page * pageSize + pageSize);
  const shown = columns.filter((column) => visible.has(column.key));

  function sort(column: Column<RecordRow>) {
    if (sortKey === column.key)
      setSortDirection(sortDirection === "asc" ? "desc" : "asc");
    else {
      setSortKey(column.key);
      setSortDirection("asc");
    }
  }

  return (
    <div class="table-tool">
      <div class="table-controls">
        <label class="search-control">
          <Icon name="search" size={16} />
          <span class="sr-only">Filter {label}</span>
          <input
            value={search}
            onInput={(event) => {
              setSearch(event.currentTarget.value);
              setPage(0);
            }}
            placeholder={`Filter ${label.toLowerCase()}`}
          />
        </label>
        <details class="column-control">
          <summary>Columns</summary>
          <div>
            {columns.map((column) => (
              <label>
                <input
                  type="checkbox"
                  checked={visible.has(column.key)}
                  onChange={() =>
                    setVisible((current) => {
                      const next = new Set(current);
                      next.has(column.key)
                        ? next.delete(column.key)
                        : next.add(column.key);
                      return next;
                    })
                  }
                />{" "}
                {column.label}
              </label>
            ))}
          </div>
        </details>
        <span class="row-count">{filtered.length} records</span>
      </div>
      <div
        class="table-scroll"
        tabIndex={0}
        aria-label={`${label} table, horizontally scrollable`}
      >
        <table class="data-table">
          <thead>
            <tr>
              {shown.map((column) => (
                <th
                  scope="col"
                  class={
                    column.priority === "secondary" ? "secondary-column" : ""
                  }
                >
                  <button onClick={() => sort(column)}>
                    {column.label}
                    <span aria-hidden="true">
                      {sortKey === column.key
                        ? sortDirection === "asc"
                          ? " ↑"
                          : " ↓"
                        : ""}
                    </span>
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {pageRows.map((row) => (
              <tr
                class={onRow ? "selectable-row" : ""}
                tabIndex={onRow ? 0 : undefined}
                onClick={() => onRow?.(row)}
                onKeyDown={(event) => {
                  if (onRow && (event.key === "Enter" || event.key === " ")) {
                    event.preventDefault();
                    onRow(row);
                  }
                }}
              >
                {shown.map((column) => (
                  <td
                    class={
                      column.priority === "secondary" ? "secondary-column" : ""
                    }
                  >
                    {column.status ? (
                      <StatusPill value={value(row, column)} />
                    ) : (
                      formatValue(value(row, column))
                    )}
                  </td>
                ))}
              </tr>
            ))}
            {!pageRows.length && (
              <tr>
                <td colSpan={shown.length || 1}>
                  <div class="empty-state">
                    No records match the current filter.
                  </div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div class="pagination">
        <button
          disabled={page === 0}
          onClick={() => setPage(Math.max(0, page - 1))}
        >
          Previous
        </button>
        <span>
          Page {Math.min(page + 1, pages)} of {pages}
        </span>
        <button
          disabled={page + 1 >= pages}
          onClick={() => setPage(Math.min(pages - 1, page + 1))}
        >
          Next
        </button>
      </div>
    </div>
  );
}

export function ActionForm({
  title,
  description,
  disabled,
  onSubmit,
  progressPollIntervalMs = 2000,
}: {
  title: string;
  description: string;
  disabled?: boolean;
  onSubmit: (justification: string) => Promise<any>;
  progressPollIntervalMs?: number;
}) {
  const [open, setOpen] = useState(false);
  const [justification, setJustification] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [operationId, setOperationId] = useState<string | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLFormElement>(null);
  const progress = useOperationProgress(operationId, progressPollIntervalMs);
  useEffect(() => {
    if (!open) return;
    const close = (event: KeyboardEvent | MouseEvent) => {
      if (event instanceof KeyboardEvent && event.key !== "Escape") return;
      if (event instanceof MouseEvent && panelRef.current?.contains(event.target as Node)) return;
      if (event instanceof MouseEvent && triggerRef.current?.contains(event.target as Node)) return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("keydown", close);
    document.addEventListener("mousedown", close);
    return () => { document.removeEventListener("keydown", close); document.removeEventListener("mousedown", close); };
  }, [open]);
  async function submit(event: Event) {
    event.preventDefault();
    setBusy(true);
    setMessage(null);
    try {
      const result = await onSubmit(justification);
      setOperationId(result?.data?.operation_id || null);
      setMessage("Operation accepted and queued.");
      setJustification("");
    } catch (reason: any) {
      setMessage(reason.message || String(reason));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div class="action-form">
      <button
        ref={triggerRef}
        class="button button-primary"
        disabled={disabled}
        onClick={() => setOpen(!open)}
      >
        {title}
      </button>
      {open && (
        <form ref={panelRef} onSubmit={submit} role="dialog" aria-label={title}>
          <p>{description}</p>
          <label>
            Justification
            <textarea
              required
              minLength={8}
              maxLength={2000}
              value={justification}
              onInput={(event) => setJustification(event.currentTarget.value)}
            />
          </label>
          <div class="form-actions">
            <button type="button" class="button" onClick={() => setOpen(false)}>
              Cancel
            </button>
            <button
              class="button button-primary"
              disabled={busy || justification.length < 8}
            >
              {busy ? "Submitting…" : "Confirm"}
            </button>
          </div>
          {message && (
            <p class="form-message" role="status">
              {message}
            </p>
          )}
          {operationId && <LifecycleProgress progress={progress} />}
        </form>
      )}
    </div>
  );
}

function OverviewPage({ onInspect, activeEstateId }: PageProps) {
  const [refresh, setRefresh] = useState(0);
  const state = useResource<RecordRow>(estatePath("/api/v1/overview", activeEstateId), refresh);
  const data = state.data;
  return (
    <>
      <PageHeader
        title="Overview"
        description="Fleet posture, migration throughput, risk and evidence across the active estate."
        generatedAt={state.generatedAt}
        actions={
          <button class="button" onClick={() => setRefresh(refresh + 1)}>
            <Icon name="refresh" size={16} />
            Refresh
          </button>
        }
      />
      <PageState loading={state.loading} error={state.error} />
      {data && (
        <>
          <div class="metric-grid">
            <MetricCard
              label="Fleet health"
              value={data.fleet_health}
              status={data.fleet_health}
            />
            <MetricCard
              label="Estate objects"
              value={data.estate?.objects}
              detail={`${data.estate?.pipelines || 0} pipelines`}
            />
            <MetricCard
              label="Row transfer"
              value={
                data.runs?.migrated_percent === null
                  ? null
                  : `${data.runs?.migrated_percent}%`
              }
              detail={`${data.runs?.complete || 0} completed runs`}
            />
            <MetricCard
              label="Active work"
              value={data.runs?.active}
              detail={`${data.waves?.queued_operations || 0} queued`}
            />
            <MetricCard
              label="Policy denials"
              value={data.policy_denials}
              detail="Deterministic enforcement events"
            />
            <MetricCard
              label="Recovery rate"
              value={
                data.recovery_rate === null
                  ? null
                  : `${Math.round(data.recovery_rate * 100)}%`
              }
              detail={`${data.human_interventions || 0} human interventions`}
            />
          </div>
          <Panel title="Latest lifecycle" subtitle="Measured durable milestones; row transfer is reported separately">
            <LifecycleProgress progress={data.runs?.latest?.progress} />
            {/* The same derivation as the run page, at a glance. /overview
                already returns the whole latest run document, so this
                needs no extra request and no new endpoint. */}
            <OrchestrationMap run={data.runs?.latest} compact />
          </Panel>
          <div class="workspace-grid two-one">
            <Panel
              title="Current estate"
              subtitle="Sanitized source and target posture"
            >
              <DataTable
                label="Estate sources"
                rows={data.estate?.sources || []}
                columns={[
                  { key: "source_id", label: "Source", priority: "primary" },
                  { key: "adapter", label: "Adapter" },
                  { key: "objects", label: "Objects" },
                  { key: "health", label: "Health", status: true },
                  {
                    key: "last_observed_at",
                    label: "Last observed",
                    priority: "secondary",
                  },
                ]}
                onRow={(row) => onInspect("Source evidence", row)}
              />
            </Panel>
            <Panel
              title="Cost and volume evidence"
              subtitle="Unavailable measurements are explicit"
            >
              <div class="availability-list">
                {[
                  ["Estimated cost", data.estimated_cost],
                  ["Actual cost", data.actual_cost],
                  ["Estimated bytes", data.estimated_bytes],
                ].map(([label, item]: any) => (
                  <button onClick={() => onInspect(String(label), item)}>
                    <StatusPill value={item.status} />
                    <span>
                      <strong>{label}</strong>
                      <small>{item.reason}</small>
                    </span>
                  </button>
                ))}
              </div>
            </Panel>
          </div>
          <Panel
            title="Stage latency"
            subtitle="Derived from durable state-transition timestamps"
          >
            <div class="latency-grid">
              {Object.entries(data.latency || {}).map(
                ([stage, metric]: any) => (
                  <button
                    class="latency-card"
                    onClick={() => onInspect(`${stage} latency`, metric)}
                  >
                    <strong>{stage.replaceAll("_", " ")}</strong>
                    <span>p50 {formatValue(metric.p50_ms)} ms</span>
                    <span>p95 {formatValue(metric.p95_ms)} ms</span>
                    <small>{metric.samples} samples</small>
                  </button>
                ),
              )}
            </div>
          </Panel>
        </>
      )}
    </>
  );
}

function EstatesPage({ onInspect, session, navigate, activeEstateId, estateRoles }: PageProps) {
  const state = useResource<any[]>("/api/v1/estates");
  const estates = state.data || [];
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const canOperate = (estateRoles || session.roles).includes("operator");

  // Select the first estate once loaded, but let the operator switch.
  // Before Day 11 Phase 6 this page rendered data[0] unconditionally,
  // which was correct only while exactly one estate could exist.
  const estate =
    estates.find((item) => item.estate_id === selectedId) ||
    estates.find((item) => item.estate_id === activeEstateId) || estates[0];

  return (
    <>
      <PageHeader
        title="Estates"
        description="Inventory, sanitized connection profiles, ownership and freshness."
        generatedAt={state.generatedAt}
        actions={
          <button
            type="button"
            class="button button-primary"
            disabled={!canOperate}
            title={
              canOperate
                ? "Describe a new estate and validate its connection"
                : "Onboarding an estate requires the operator role"
            }
            onClick={() => navigate("estates/new")}
          >
            Onboard estate
          </button>
        }
      />
      <PageState loading={state.loading} error={state.error} />
      {estates.length > 1 && (
        <Panel title="Registered estates" subtitle="Select one to inspect">
          <DataTable
            label="Estates"
            rows={estates}
            columns={[
              { key: "estate_id", label: "Estate" },
              { key: "display_name", label: "Name" },
              { key: "status", label: "Status", status: true },
              { key: "objects", label: "Objects" },
              { key: "pipelines", label: "Pipelines" },
              { key: "origin", label: "Authored" },
              { key: "last_run_at", label: "Last run" },
            ]}
            onRow={(row: any) => setSelectedId(row.estate_id)}
          />
        </Panel>
      )}
      {!state.loading && estates.length === 0 && (
        <Panel title="No estates registered">
          <p>
            Nothing is registered yet. Seed the committed estates with{" "}
            <code>python infrastructure/seed_estates.py</code>, or onboard one
            here.
          </p>
        </Panel>
      )}
      {estate && (
        <>
          <div class="metric-grid">
            <MetricCard label="Estate" value={estate.display_name} />
            <MetricCard label="Objects" value={estate.objects} />
            <MetricCard label="Pipelines" value={estate.pipelines} />
            <MetricCard
              label="Target"
              value={estate.target?.system}
              detail={estate.target?.dataset_env}
            />
          </div>
          <Panel
            title="Source inventory"
            subtitle="Credentials are never exposed to this service"
          >
            <DataTable
              label="Sources"
              rows={estate.sources}
              columns={[
                { key: "source_id", label: "Source" },
                { key: "adapter", label: "Adapter" },
                { key: "objects", label: "Objects" },
                { key: "health", label: "Connection", status: true },
                { key: "last_observed_at", label: "Last observed" },
                {
                  key: "credential",
                  label: "Credential",
                  value: (row) =>
                    row.connection?.credential_source || "Not required",
                },
              ]}
              onRow={(row) => onInspect("Connection profile", row)}
            />
          </Panel>
        </>
      )}
    </>
  );
}

function AssessmentsPage(props: PageProps) {
  const [refresh, setRefresh] = useState(0);
  const state = useResource<RecordRow>(estatePath("/api/v1/assessments", props.activeEstateId), refresh);
  const [packId, setPackId] = useState("");
  const canOperate = (props.estateRoles || props.session.roles).includes("operator");
  useEffect(() => {
    if (!packId && state.data?.packs?.length)
      setPackId(state.data.packs[0].pack_id);
  }, [state.data]);
  return (
    <>
      <PageHeader
        title="Assessments"
        description="Evaluate Migration Packs without crossing the execution boundary."
        generatedAt={state.generatedAt}
        actions={
          <div class="header-action-cluster">
            <select
              aria-label="Migration Pack"
              value={packId}
              onChange={(event) => setPackId(event.currentTarget.value)}
            >
              {(state.data?.packs || []).map((pack: any) => (
                <option value={pack.pack_id}>
                  {pack.pack_id} · v{pack.version}
                </option>
              ))}
            </select>
            <ActionForm
              title="Start assessment"
              description={`Start an assessment with ${packId}.`}
              disabled={!canOperate || !packId}
              onSubmit={async (justification) => {
                const result = await api("/api/v1/assessments", {
                  method: "POST",
                  headers: { "Idempotency-Key": idempotencyKey("assessment") },
                  body: JSON.stringify({
                    pack_id: packId,
                    estate_id: props.activeEstateId,
                    justification,
                  }),
                });
                setRefresh(refresh + 1);
                return result;
              }}
              progressPollIntervalMs={props.progressPollIntervalMs}
            />
          </div>
        }
      />
      <PageState loading={state.loading} error={state.error} />
      {state.data && (
        <div class="workspace-grid">
          <Panel title="Assessment history">
            <DataTable
              label="Assessments"
              rows={state.data.runs || []}
              columns={[
                { key: "run_id", label: "Run" },
                { key: "pipeline_id", label: "Pack" },
                { key: "state", label: "State", status: true },
                { key: "lifecycle", label: "Lifecycle", value: (row) => `${row.progress?.percent ?? 0}% · ${row.progress?.label || "Queued"}` },
                { key: "created_at", label: "Created" },
                { key: "last_transition_at", label: "Updated" },
              ]}
              onRow={(row) => props.onInspect("Assessment report", row)}
            />
          </Panel>
          <Panel title="Migration Packs">
            <DataTable
              label="Migration Packs"
              rows={state.data.packs || []}
              columns={[
                { key: "pack_id", label: "Pack" },
                { key: "version", label: "Version" },
                { key: "source_id", label: "Source" },
                { key: "execution_supported", label: "Execution supported" },
              ]}
              onRow={(row) => props.onInspect("Migration Pack", row)}
            />
          </Panel>
        </div>
      )}
    </>
  );
}

function WavesPage(props: PageProps) {
  const [refresh, setRefresh] = useState(0);
  const state = useResource<RecordRow>(estatePath("/api/v1/waves", props.activeEstateId), refresh);
  const [sourceId, setSourceId] = useState("");
  const [overrideState, setOverrideState] = useState("HOLD");
  const canOperate = (props.estateRoles || props.session.roles).includes("operator");
  const estateSources = (props.activeEstate?.sources || []) as any[];
  useEffect(() => {
    if (!estateSources.some((source) => source.source_id === sourceId)) setSourceId(estateSources[0]?.source_id || "");
  }, [props.activeEstateId, props.activeEstate]);
  const running = Object.entries(
    state.data?.state?.running_by_source || {},
  ).flatMap(([source, items]: any) =>
    items.map((item: string) => ({
      source_id: source,
      item_id: item,
      state: "RUNNING",
    })),
  );
  return (
    <>
      <PageHeader
        title="Wave Manager"
        description="Concurrency, backlog, critical-risk capacity and operator holds."
        generatedAt={state.generatedAt}
        actions={
          <div class="header-action-cluster">
            <select
              value={sourceId}
              onChange={(event) => setSourceId(event.currentTarget.value)}
            >
              {estateSources.map((source) => <option value={source.source_id}>{source.source_id}</option>)}
            </select>
            <select
              value={overrideState}
              onChange={(event) => setOverrideState(event.currentTarget.value)}
            >
              <option>HOLD</option>
              <option>OPEN</option>
            </select>
            <ActionForm
              title="Apply override"
              description={`Apply ${overrideState} to ${sourceId}.`}
              disabled={!canOperate || !sourceId}
              onSubmit={async (justification) => {
                const result = await api(
                  `/api/v1/waves/${encodeURIComponent(sourceId)}/override`,
                  {
                    method: "PUT",
                    headers: { "Idempotency-Key": idempotencyKey("wave") },
                    body: JSON.stringify({
                      state: overrideState,
                      justification,
                      estate_id: props.activeEstateId,
                    }),
                  },
                );
                setRefresh(refresh + 1);
                return result;
              }}
              progressPollIntervalMs={props.progressPollIntervalMs}
            />
          </div>
        }
      />
      <PageState loading={state.loading} error={state.error} />
      {state.data && (
        <>
          <div class="metric-grid">
            <MetricCard label="Running transfers" value={running.length} />
            <MetricCard
              label="Critical running"
              value={state.data.state?.running_critical?.length || 0}
            />
            <MetricCard
              label="Critical cap"
              value={state.data.limits?.max_concurrent_critical}
            />
            <MetricCard
              label="Oldest backlog"
              value={
                state.data.oldest_backlog_age_ms === null
                  ? null
                  : `${Math.round(state.data.oldest_backlog_age_ms / 60000)} min`
              }
              detail={`${state.data.queued?.length || 0} queued · ${state.data.blocked?.length || 0} blocked`}
            />
          </div>
          <div class="workspace-grid">
            <Panel title="Active reservations">
              <DataTable
                label="Reservations"
                rows={running}
                columns={[
                  { key: "source_id", label: "Source" },
                  { key: "item_id", label: "Run" },
                  { key: "state", label: "State", status: true },
                ]}
                onRow={(row) => props.onInspect("Reservation", row)}
              />
            </Panel>
            <Panel title="Overrides">
              <DataTable
                label="Overrides"
                rows={state.data.overrides || []}
                columns={[
                  { key: "source_id", label: "Source" },
                  { key: "state", label: "State", status: true },
                  { key: "actor", label: "Actor" },
                  { key: "updated_at", label: "Updated" },
                  { key: "expires_at", label: "Expires" },
                ]}
                onRow={(row) => props.onInspect("Wave override", row)}
              />
            </Panel>
          </div>
          <Panel
            title="Queued and blocked work"
            subtitle={`Escalates after ${state.data.limits?.backlog_age_escalation_minutes} minutes · approval window ${state.data.limits?.approval_window?.enabled ? "enabled" : "disabled"}`}
          >
            <DataTable
              label="Wave backlog"
              rows={[...(state.data.blocked || []), ...(state.data.queued || [])]}
              columns={[
                { key: "created_at", label: "Requested" },
                { key: "kind", label: "Operation" },
                { key: "actor", label: "Actor" },
                { key: "status", label: "Status", status: true },
                { key: "backlog_age_ms", label: "Backlog age ms" },
                { key: "error", label: "Failure", priority: "secondary" },
              ]}
              onRow={(row) => props.onInspect("Queued operation", row)}
            />
          </Panel>
          <Panel title="Wave event history">
            <DataTable
              label="Wave events"
              rows={[...(state.data.events || [])].reverse()}
              columns={[
                { key: "recorded_at", label: "Time" },
                { key: "event", label: "Event", status: true },
                { key: "source_id", label: "Source" },
                { key: "item_id", label: "Run" },
                { key: "risk_class", label: "Risk" },
                { key: "reason", label: "Reason", priority: "secondary" },
              ]}
              onRow={(row) => props.onInspect("Wave evidence", row)}
            />
          </Panel>
        </>
      )}
    </>
  );
}

function RunsPage(props: PageProps) {
  const runId = props.route.replace(/^\/+|\/+$/g, "").split("/")[1];
  if (runId) return <RunDetailPage {...props} runId={runId} />;
  const state = useResource<any[]>(estatePath("/api/v1/runs?limit=100", props.activeEstateId));
  const canOperate = (props.estateRoles || props.session.roles).includes("operator");
  const readiness = props.activeEstate?.execution_readiness;
  const options = readiness?.options || [];
  const [selectedExecution, setSelectedExecution] = useState("");
  useEffect(() => {
    setSelectedExecution(options.length === 1 ? `${options[0].source_id}::${options[0].pack_id}` : "");
  }, [props.activeEstateId, options.map((option) => `${option.source_id}:${option.pack_id}`).join("|")]);
  const selected = options.find(
    (option) => `${option.source_id}::${option.pack_id}` === selectedExecution,
  );
  const readinessLoading = Boolean(props.activeEstateId && !props.activeEstate);
  const disabledReason = readinessLoading
    ? "Loading execution readiness…"
    : !canOperate
      ? "Operator permission is required."
      : readiness?.status === "blocked"
        ? readiness.blockers[0]?.message || "No executable Migration Pack is assigned."
        : readiness?.status === "selection_required" && !selected
          ? "Select a source and Migration Pack."
          : !selected
            ? "No executable Migration Pack is assigned."
            : null;
  return (
    <>
      <PageHeader
        title="Runs"
        description="Execution state, duration, ownership and evidence across migration runs."
        generatedAt={state.generatedAt}
        actions={
          <div class="header-action-cluster">
            {options.length > 1 && (
              <select
                aria-label="Source and Migration Pack"
                value={selectedExecution}
                onChange={(event) => setSelectedExecution(event.currentTarget.value)}
              >
                <option value="">Select source and pack</option>
                {options.map((option) => (
                  <option value={`${option.source_id}::${option.pack_id}`}>{option.label}</option>
                ))}
              </select>
            )}
            <ActionForm
              title="Start migration"
              description={selected ? `Start ${selected.pack_id} on ${selected.source_id}.` : disabledReason || "Start the selected Migration Pack."}
              disabled={Boolean(disabledReason) || !props.activeEstateId}
              onSubmit={async (justification) => {
                return api("/api/v1/runs", {
                  method: "POST",
                  headers: { "Idempotency-Key": idempotencyKey("migration") },
                  body: JSON.stringify({
                    source_id: selected?.source_id,
                    pack_id: selected?.pack_id,
                    estate_id: props.activeEstateId,
                    justification,
                  }),
                });
              }}
              progressPollIntervalMs={props.progressPollIntervalMs}
            />
          </div>
        }
      />
      {disabledReason && (
        <div class={`inline-alert ${readinessLoading ? "" : "warning"}`} role="status">
          {disabledReason}
        </div>
      )}
      <PageState loading={state.loading} error={state.error} />
      {state.data && (
        <Panel title="Migration runs" subtitle="Select a row for full evidence">
          <DataTable
            label="Runs"
            rows={state.data}
            columns={[
              { key: "run_id", label: "Run", priority: "primary" },
              { key: "pipeline_id", label: "Pipeline" },
              { key: "mode", label: "Mode" },
              { key: "state", label: "State", status: true },
              { key: "progress", label: "Lifecycle", value: (row) => `${row.progress?.percent ?? 0}% · ${row.progress?.label || "Queued"}` },
              { key: "created_at", label: "Created" },
              { key: "last_transition_at", label: "Updated" },
              { key: "attempt", label: "Attempt", priority: "secondary" },
            ]}
            onRow={(row) => props.navigate(`runs/${row.run_id}`)}
          />
        </Panel>
      )}
    </>
  );
}

function RunDetailPage(props: PageProps & { runId: string }) {
  const [refresh, setRefresh] = useState(0);
  const state = useResource<RecordRow>(
    `/api/v1/runs/${encodeURIComponent(props.runId)}`,
    refresh,
  );
  const data = state.data;
  const canRetry =
    (props.estateRoles || props.session.roles).includes("operator") &&
    ["PLANNED", "FAILED"].includes(data?.run?.state);
  const canApprove =
    (props.estateRoles || props.session.roles).includes("approver") &&
    data?.run?.state === "READY_FOR_APPROVAL";
  const history = data?.run?.state_history || [];
  return (
    <>
      <PageHeader
        title={data?.run?.pipeline_id || "Run detail"}
        description={props.runId}
        generatedAt={state.generatedAt}
        actions={
          <div class="header-action-cluster">
            <button class="button" onClick={() => props.navigate("runs")}>
              Back to runs
            </button>
            {canRetry && (
              <ActionForm
                title="Retry run"
                description={`Retry from ${data?.run?.state}.`}
                onSubmit={async (justification) => {
                  const result = await api(
                    `/api/v1/runs/${encodeURIComponent(props.runId)}/retry`,
                    {
                      method: "POST",
                      headers: { "Idempotency-Key": idempotencyKey("retry") },
                      body: JSON.stringify({ justification }),
                    },
                  );
                  setRefresh(refresh + 1);
                  return result;
                }}
                progressPollIntervalMs={props.progressPollIntervalMs}
              />
            )}
            {canApprove && (
              <ActionForm
                title="Approve cutover"
                description="Approve the recorded plan scope for cutover."
                onSubmit={async (justification) => {
                  const result = await api(
                    `/api/v1/runs/${encodeURIComponent(props.runId)}/approve`,
                    {
                      method: "POST",
                      headers: { "Idempotency-Key": idempotencyKey("approve") },
                      body: JSON.stringify({ justification }),
                    },
                  );
                  setRefresh(refresh + 1);
                  return result;
                }}
                progressPollIntervalMs={props.progressPollIntervalMs}
              />
            )}
          </div>
        }
      />
      <PageState loading={state.loading} error={state.error} />
      {data && (
        <>
          <div class="metric-grid">
            <MetricCard
              label="Current stage"
              value={data.run.state}
              status={data.run.state}
            />
            <MetricCard label="Mode" value={data.run.mode || "execution"} />
            <MetricCard label="Attempt" value={data.run.attempt} />
            <MetricCard
              label="Plan hash"
              value={data.migration_plan?.[0]?.plan_hash?.slice(0, 12)}
            />
            <MetricCard
              label="Pinned agents"
              value={Object.keys(data.run.pinned_agents || {}).length}
            />
            <MetricCard label="Trace" value={data.run.trace_id} />
          </div>
          <Panel title="Lifecycle progress" subtitle="Governance and cutover milestones, not elapsed-time estimation">
            <LifecycleProgress progress={data.run.progress} />
          </Panel>
          <Panel
            title="Agent orchestration"
            subtitle="Derived from this run's own state history — no stage is inferred or estimated."
          >
            <OrchestrationMap run={data.run} />
          </Panel>
          <Panel title="Stage timeline">
            <ol class="stage-timeline">
              {history.map((item: any, index: number) => (
                <li class={index === history.length - 1 ? "current" : "done"}>
                  <span>{index + 1}</span>
                  <div>
                    <strong>{item.state.replaceAll("_", " ")}</strong>
                    <small>{formatValue(item.at)}</small>
                  </div>
                </li>
              ))}
            </ol>
          </Panel>
          <Panel title="Stage execution evidence">
            <DataTable
              label="Stage metrics"
              rows={data.stage_metrics || []}
              columns={[
                { key: "stage", label: "Stage" },
                { key: "status", label: "Status", status: true },
                { key: "duration_ms", label: "Duration ms" },
                { key: "attempt", label: "Attempt" },
                { key: "agent_id", label: "Agent" },
                { key: "version", label: "Version" },
                { key: "model", label: "Model" },
                { key: "trace_id", label: "Trace", priority: "secondary" },
              ]}
              onRow={(row) => props.onInspect("Stage evidence", row)}
            />
          </Panel>
          <div class="workspace-grid">
            <Panel title="Migration plan">
              <DataTable
                label="Plan steps"
                rows={data.migration_plan?.[0]?.steps || []}
                columns={[
                  { key: "step_id", label: "Step" },
                  { key: "source_table", label: "Source" },
                  { key: "target_table", label: "Target" },
                  { key: "status", label: "Status", status: true },
                  { key: "notes", label: "Notes" },
                ]}
                onRow={(row) => props.onInspect("Plan step", row)}
              />
            </Panel>
            <Panel title="Data-plane jobs">
              <DataTable
                label="Data-plane jobs"
                rows={data.migration_executions || []}
                columns={[
                  { key: "data_plane_job_id", label: "Job" },
                  { key: "executor", label: "Executor" },
                  { key: "status", label: "Status", status: true },
                  { key: "source_count", label: "Source rows" },
                  { key: "target_count", label: "Target rows" },
                  {
                    key: "row_transfer",
                    label: "Row transfer",
                    value: (row) => row.source_count ? `${Math.min(100, Math.round(100 * (row.target_count || 0) / row.source_count))}%` : "Not available",
                  },
                  { key: "bytes_read", label: "Bytes read" },
                  { key: "bytes_written", label: "Bytes written" },
                  { key: "duration_ms", label: "Duration ms" },
                ]}
                onRow={(row) => props.onInspect("Execution manifest", row)}
              />
            </Panel>
          </div>
          <Panel title="Reconciliation evidence">
            <DataTable
              label="Reconciliation"
              rows={data.reconciliation || []}
              columns={[
                { key: "check_type", label: "Check" },
                { key: "table", label: "Table" },
                { key: "status", label: "Status", status: true },
                { key: "source_value", label: "Source" },
                { key: "target_value", label: "Target" },
                { key: "tolerance", label: "Tolerance" },
                {
                  key: "evidence_hash",
                  label: "Evidence hash",
                  priority: "secondary",
                },
              ]}
              onRow={(row) => props.onInspect("Reconciliation evidence", row)}
            />
          </Panel>
          <div class="workspace-grid">
            <Panel title="Recovery and memory">
              <DataTable
                label="Incidents"
                rows={data.incidents || []}
                columns={[
                  { key: "signature", label: "Signature" },
                  { key: "outcome", label: "Outcome", status: true },
                  { key: "root_cause_generated_by", label: "Provenance" },
                  { key: "root_cause", label: "Root cause" },
                ]}
                onRow={(row) => props.onInspect("Incident", row)}
              />
            </Panel>
            <Panel title="Policy evidence">
              <DataTable
                label="Policy evidence"
                rows={data.policy_decisions || []}
                columns={[
                  { key: "policy_id", label: "Policy" },
                  { key: "agent_id", label: "Identity" },
                  { key: "tool_name", label: "Tool" },
                  { key: "decision", label: "Decision", status: true },
                  {
                    key: "evidence_hash",
                    label: "Evidence",
                    priority: "secondary",
                  },
                ]}
                onRow={(row) => props.onInspect("Policy decision", row)}
              />
            </Panel>
          </div>
        </>
      )}
    </>
  );
}

function LineagePage(props: PageProps) {
  const state = useResource<RecordRow>(estatePath("/api/v1/lineage", props.activeEstateId));
  const [filter, setFilter] = useState("ALL");
  const edges = state.data?.edges || [];
  const nodes = (state.data?.nodes || []).filter(
    (node: any) =>
      filter === "ALL" ||
      node.classification === filter ||
      node.type === filter.toLowerCase(),
  );
  return (
    <>
      <PageHeader
        title="Lineage"
        description="Asset relationships, provenance and impact evidence."
        generatedAt={state.generatedAt}
        actions={
          <select
            value={filter}
            onChange={(event) => setFilter(event.currentTarget.value)}
            aria-label="Filter lineage"
          >
            <option>ALL</option>
            <option>PII</option>
            <option>METADATA</option>
            <option>PIPELINE</option>
          </select>
        }
      />
      <PageState loading={state.loading} error={state.error} />
      {state.data && (
        <div class="lineage-layout">
          <Panel
            title="Lineage graph"
            subtitle={`${nodes.length} visible nodes · ${edges.length} relationships`}
            className="lineage-panel"
          >
            <div class="lineage-canvas" role="list" aria-label="Lineage assets">
              {nodes.map((node: any) => (
                <button
                  role="listitem"
                  class={`lineage-node node-${node.type}`}
                  onClick={() =>
                    props.onInspect("Lineage asset", {
                      ...node,
                      relationships: edges.filter(
                        (edge: any) =>
                          edge.from === node.id || edge.to === node.id,
                      ),
                    })
                  }
                >
                  <Icon name={node.type === "pipeline" ? "runs" : "estates"} />
                  <strong>{node.label}</strong>
                  <small>{node.classification || node.type}</small>
                </button>
              ))}
            </div>
          </Panel>
          <Panel title="Relationship register">
            <DataTable
              label="Lineage relationships"
              rows={edges}
              columns={[
                { key: "from", label: "From" },
                { key: "to", label: "To" },
                { key: "relationship", label: "Relationship" },
                { key: "confidence", label: "Confidence" },
                { key: "source", label: "Evidence source" },
              ]}
              onRow={(row) => props.onInspect("Lineage relationship", row)}
            />
          </Panel>
        </div>
      )}
    </>
  );
}

function ReconciliationPage(props: PageProps) {
  const state = useResource<any[]>(estatePath("/api/v1/reconciliation", props.activeEstateId));
  return (
    <>
      <PageHeader
        title="Reconciliation"
        description="Cross-run validation deltas, tolerances and immutable evidence."
        generatedAt={state.generatedAt}
      />
      <PageState loading={state.loading} error={state.error} />
      {state.data && (
        <Panel title="Validation checks">
          <DataTable
            label="Reconciliation checks"
            rows={state.data}
            columns={[
              { key: "run_id", label: "Run" },
              { key: "check_type", label: "Check" },
              { key: "table", label: "Table" },
              { key: "status", label: "Status", status: true },
              { key: "source_value", label: "Source" },
              { key: "target_value", label: "Target" },
              { key: "tolerance", label: "Tolerance" },
              { key: "checked_at", label: "Checked" },
            ]}
            onRow={(row) => props.onInspect("Reconciliation evidence", row)}
          />
        </Panel>
      )}
    </>
  );
}

function PoliciesPage(props: PageProps) {
  const state = useResource<RecordRow>(estatePath("/api/v1/policies", props.activeEstateId));
  return (
    <>
      <PageHeader
        title="Policies & Approvals"
        description="Deterministic decisions, separation of duties and approval history."
        generatedAt={state.generatedAt}
      />
      <PageState loading={state.loading} error={state.error} />
      {state.data && (
        <div class="workspace-grid">
          <Panel title="Policy decisions">
            <DataTable
              label="Policy decisions"
              rows={state.data.decisions || []}
              columns={[
                { key: "decided_at", label: "Time" },
                { key: "policy_id", label: "Policy" },
                { key: "agent_id", label: "Identity" },
                { key: "tool_name", label: "Tool" },
                { key: "decision", label: "Decision", status: true },
                { key: "reason", label: "Reason", priority: "secondary" },
              ]}
              onRow={(row) => props.onInspect("Policy evidence", row)}
            />
          </Panel>
          <Panel title="Approval inbox and history">
            <DataTable
              label="Approvals"
              rows={state.data.approvals || []}
              columns={[
                { key: "recorded_at", label: "Time" },
                { key: "run_id", label: "Run" },
                { key: "event", label: "Event", status: true },
                { key: "approved_by", label: "Approver" },
                { key: "justification", label: "Justification" },
              ]}
              onRow={(row) => props.onInspect("Approval record", row)}
            />
          </Panel>
        </div>
      )}
    </>
  );
}

/** One card per agent: the newest APPROVED version.
 *
 * The registry keeps every version, and an agent legitimately has several
 * approved at once — bumping Discovery to 1.1.0 while 1.0.0 stays approved
 * is the documented way to roll a capability forward without breaking runs
 * pinned to the old card. Rendering the raw list therefore shows the same
 * agent twice, which reads as a duplicate rather than as version history.
 * Versions are compared numerically per segment so 1.10.0 sorts above
 * 1.9.0, which a string compare gets backwards.
 */
function latestApprovedPerAgent(cards: RecordRow[]): RecordRow[] {
  const rank = (version: unknown) =>
    String(version || "0")
      .split(".")
      .map((part) => Number(part) || 0);
  const newest = new Map<string, RecordRow>();
  for (const card of cards) {
    if (card.status !== "APPROVED") continue;
    const id = String(card.agent_id);
    const held = newest.get(id);
    if (!held) {
      newest.set(id, card);
      continue;
    }
    const [a, b] = [rank(card.version), rank(held.version)];
    for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
      if ((a[i] || 0) === (b[i] || 0)) continue;
      if ((a[i] || 0) > (b[i] || 0)) newest.set(id, card);
      break;
    }
  }
  return [...newest.values()].sort((x, y) => String(x.agent_id).localeCompare(String(y.agent_id)));
}

function AgentsPage(props: PageProps) {
  const state = useResource<RecordRow>(estatePath("/api/v1/agents", props.activeEstateId));
  const pinnedRunCounts = state.data?.pinned_run_counts || {};
  return (
    <>
      <PageHeader
        title="Agents"
        description="Registry versions, ownership, capabilities and runtime usage."
        generatedAt={state.generatedAt}
      />
      <PageState loading={state.loading} error={state.error} />
      {state.data && (
        <>
        {/* The fleet, before the registry detail. Every agent used to be an
            identical table row, so "who is doing this and what for" meant
            decoding an id string. Cards answer it at a glance; the table
            below still carries the version, ownership and capability
            detail an operator needs after that. */}
        <Panel title="The fleet" subtitle="Seven specialists, resolved by capability — never by direct import.">
          <div class="agent-grid">
            {latestApprovedPerAgent(state.data.cards || []).map((card: RecordRow) => (
                <button
                  key={card.agent_id}
                  type="button"
                  class="agent-card"
                  onClick={() => props.onInspect("Agent card", card)}
                >
                  <AgentBadge agentId={String(card.agent_id)} />
                  <span class="agent-card-meta">
                    <StatusPill value={card.status} />
                    <small>
                      v{card.version} · {pinnedRunCounts[card.agent_id] || 0} pinned
                    </small>
                  </span>
                </button>
              ))}
          </div>
        </Panel>
        <Panel title="Agent registry">
          <DataTable
            label="Agents"
            rows={state.data.cards || []}
            columns={[
              { key: "agent_id", label: "Agent" },
              { key: "version", label: "Version" },
              { key: "status", label: "Status", status: true },
              { key: "owner", label: "Owner" },
              { key: "model", label: "Model" },
              { key: "framework", label: "Framework" },
              { key: "capabilities", label: "Capabilities" },
              {
                key: "pinned",
                label: "Pinned runs",
                value: (row) => pinnedRunCounts[row.agent_id] || 0,
              },
            ]}
            onRow={(row) => props.onInspect("Agent card", row)}
          />
        </Panel>
        </>
      )}
    </>
  );
}

function EvaluationsPage(props: PageProps) {
  const state = useResource<RecordRow>(estatePath("/api/v1/evaluations", props.activeEstateId));
  const scale = state.data?.scale_metrics;
  return (
    <>
      <PageHeader
        title="Evaluations"
        description="Scenario evidence and bounded scale measurements."
        generatedAt={state.generatedAt}
      />
      <PageState loading={state.loading} error={state.error} />
      {state.data && (
        <div class="workspace-grid">
          <Panel title="Evaluation runs">
            <DataTable
              label="Evaluation runs"
              rows={state.data.runs || []}
              columns={[
                { key: "harness_run_id", label: "Evaluation" },
                {
                  key: "status",
                  label: "Status",
                  value: (row) => (row.failed ? "FAILED" : "PASSED"),
                  status: true,
                },
                { key: "started_at", label: "Started" },
                { key: "finished_at", label: "Completed" },
                { key: "passed", label: "Passed" },
                { key: "failed", label: "Failed" },
              ]}
              onRow={(row) => props.onInspect("Evaluation evidence", row)}
            />
          </Panel>
          <Panel
            title="Scale metrics"
            subtitle="Durable measured control-plane evidence"
          >
            {scale ? (
              <div class="metric-grid">
                <MetricCard
                  label="Pipeline definitions"
                  value={scale.pipeline_count}
                />
                <MetricCard
                  label="Schema p95"
                  value={`${scale.schema_validation?.p95_ms} ms`}
                />
                <MetricCard
                  label="Wave total"
                  value={`${scale.wave_scheduling?.total_duration_ms} ms`}
                />
                <MetricCard
                  label="Policy p95"
                  value={`${scale.policy_decisions?.p95_ms} ms`}
                />
              </div>
            ) : (
              <div class="empty-state">
                <StatusPill value="NOT_CONFIGURED" />
                <p>{state.data.scale_report_reason}</p>
              </div>
            )}
          </Panel>
        </div>
      )}
    </>
  );
}

// The panel that turns "queued, but nothing is happening" from a mystery
// into a glance. Before the in-process consumers existed that state meant
// "go run a script in a terminal"; now it means one of three specific,
// visible things — a consumer is paused, this instance is on standby
// behind another one, or a handler is failing — and each is named here.
function WorkersPanel(props: PageProps) {
  const [refresh, setRefresh] = useState(0);
  const state = useResource<RecordRow>("/api/v1/workers", refresh);
  const canOperate = (props.estateRoles || props.session.roles).includes("operator");
  const data = state.data as any;
  const consumers: RecordRow[] = data?.consumers || [];
  const lease = data?.lease || {};

  async function control(name: string, action: "pause" | "resume", justification: string) {
    const result = await api(`/api/v1/workers/${name}/${action}`, {
      method: "POST",
      body: JSON.stringify({ justification }),
    });
    setRefresh((value) => value + 1);
    return result;
  }

  return (
    <Panel
      title="Event consumers"
      subtitle="Every console action publishes a command; these are what consume them."
    >
      <PageState loading={state.loading} error={state.error} />
      {data && !data.enabled && (
        <p class="form-message" role="status">
          In-process workers are not running here: {data.reason}. Queued
          operations will stay queued until a process with workers enabled
          picks them up.
        </p>
      )}
      {data?.enabled && !lease.held && (
        <p class="form-message" role="status">
          Standby — {lease.standby_reason}. This is normal for a second
          instance: only the lease holder consumes, so the same message is
          never handled twice.
        </p>
      )}
      {data?.enabled && (
        <>
          <DataTable
            label="Consumers"
            rows={consumers}
            columns={[
              { key: "name", label: "Consumer" },
              { key: "subscription", label: "Subscription" },
              { key: "state", label: "State", status: true },
              { key: "last_message_at", label: "Last message" },
              { key: "processed_count", label: "Handled" },
              { key: "error_count", label: "Errors" },
              // Backlog is deliberately null from the API: real queue depth
              // needs google-cloud-monitoring and an IAM role this project
              // does not grant. formatValue renders that as "Not available",
              // which is honest — a zero would read as "nothing queued".
              { key: "backlog", label: "Backlog" },
            ]}
            onRow={(row) => props.onInspect("Consumer detail", row)}
          />
          <div class="action-row">
            {consumers.map((consumer: any) => (
              <ActionForm
                key={consumer.name}
                title={
                  consumer.state === "paused"
                    ? `Resume ${consumer.name}`
                    : `Pause ${consumer.name}`
                }
                description={
                  consumer.state === "paused"
                    ? `Resume ${consumer.name} so it consumes ${consumer.subscription} again.`
                    : `Pause ${consumer.name}. It stops taking new messages from ${consumer.subscription}; anything already in flight finishes. This affects EVERY estate on that subscription, not just the one selected.`
                }
                disabled={!canOperate}
                onSubmit={(justification) =>
                  control(
                    consumer.name,
                    consumer.state === "paused" ? "resume" : "pause",
                    justification,
                  )
                }
              />
            ))}
          </div>
        </>
      )}
    </Panel>
  );
}

function HealthPage(props: PageProps) {
  const [refresh, setRefresh] = useState(0);
  const state = useResource<RecordRow>(estatePath("/api/v1/system-health", props.activeEstateId), refresh);
  return (
    <>
      <PageHeader
        title="System Health"
        description="Deployment, data services, event transport and observability freshness."
        generatedAt={state.generatedAt}
        actions={
          <button class="button" onClick={() => setRefresh(refresh + 1)}>
            <Icon name="refresh" size={16} />
            Refresh
          </button>
        }
      />
      <PageState loading={state.loading} error={state.error} />
      {state.data && (
        <>
          <div class="metric-grid">
            <MetricCard
              label="Build version"
              value={state.data.build_version}
            />
            <MetricCard
              label="Services observed"
              value={
                (state.data.services || []).filter((item: any) =>
                  ["HEALTHY", "OBSERVED"].includes(item.status),
                ).length
              }
              detail={`${state.data.services?.length || 0} total services`}
            />
          </div>
          <Panel title="Service health">
            <DataTable
              label="Services"
              rows={state.data.services || []}
              columns={[
                { key: "service", label: "Service" },
                { key: "status", label: "Status", status: true },
                { key: "last_observed_at", label: "Last observed" },
                { key: "detail", label: "Evidence" },
              ]}
              onRow={(row) => props.onInspect("Service evidence", row)}
            />
          </Panel>
        </>
      )}
      {/* Outside the system-health guard on purpose. If that request is
          slow or failing, this panel is precisely the one an operator
          needs — hiding it behind another endpoint's success would take
          the answer away exactly when the question is being asked. */}
      <WorkersPanel {...props} />
    </>
  );
}

const LazyLineagePage = lazy(() =>
  import("./specialized-pages").then((module) => ({
    default: module.LineagePage,
  })),
);
const LazyEvaluationsPage = lazy(() =>
  import("./specialized-pages").then((module) => ({
    default: module.EvaluationsPage,
  })),
);
// Lazy for two reasons: onboarding is a rare, operator-only flow that
// should not weigh on every page load, and estate-wizard.tsx imports
// shared components from this module — a static import here would close
// that cycle at module-evaluation time.
// Lazy for the cycle reason above: incidents.tsx imports the shared
// primitives from this module.
const LazyIncidentsPage = lazy(() =>
  import("./incidents").then((module) => ({ default: module.IncidentsPage })),
);
const LazyDeadLettersPage = lazy(() =>
  import("./incidents").then((module) => ({ default: module.DeadLettersPage })),
);
const LazyMemoryBankPage = lazy(() =>
  import("./incidents").then((module) => ({ default: module.MemoryBankPage })),
);
const LazyApprovalsPage = lazy(() =>
  import("./incidents").then((module) => ({ default: module.ApprovalsPage })),
);
const LazyEstateWizard = lazy(() =>
  import("./estate-wizard").then((module) => ({
    default: module.EstateWizard,
  })),
);

export function PageRouter(props: PageProps) {
  const root =
    props.route.replace(/^\/+|\/+$/g, "").split("/")[0] || "overview";
  const segments = props.route.replace(/^\/+|\/+$/g, "").split("/");
  if (root === "overview") return <OverviewPage {...props} />;
  if (root === "estates" && segments[1] === "new")
    return (
      <Suspense
        fallback={
          <div class="page-state">
            <ProgressBar value={-1} aria-label="Loading onboarding" />
            Loading onboarding…
          </div>
        }
      >
        <LazyEstateWizard {...props} />
      </Suspense>
    );
  if (root === "estates") return <EstatesPage {...props} />;
  if (root === "assessments") return <AssessmentsPage {...props} />;
  if (root === "waves") return <WavesPage {...props} />;
  if (root === "runs") return <RunsPage {...props} />;
  if (root === "lineage")
    return (
      <Suspense
        fallback={
          <div class="page-state">
            <ProgressBar value={-1} aria-label="Loading lineage module" />
            Loading lineage module…
          </div>
        }
      >
        <LazyLineagePage {...props} />
      </Suspense>
    );
  if (root === "reconciliation") return <ReconciliationPage {...props} />;
  if (root === "policies") return <PoliciesPage {...props} />;
  if (root === "agents") return <AgentsPage {...props} />;
  if (root === "evaluations")
    return (
      <Suspense
        fallback={
          <div class="page-state">
            <ProgressBar value={-1} aria-label="Loading evaluations module" />
            Loading evaluation module…
          </div>
        }
      >
        <LazyEvaluationsPage {...props} />
      </Suspense>
    );
  if (root === "incidents")
    return (
      <Suspense fallback={<PageState loading error={null} />}>
        <LazyIncidentsPage {...props} />
      </Suspense>
    );
  if (root === "dead-letters")
    return (
      <Suspense fallback={<PageState loading error={null} />}>
        <LazyDeadLettersPage {...props} />
      </Suspense>
    );
  if (root === "memory")
    return (
      <Suspense fallback={<PageState loading error={null} />}>
        <LazyMemoryBankPage {...props} />
      </Suspense>
    );
  if (root === "approvals")
    return (
      <Suspense fallback={<PageState loading error={null} />}>
        <LazyApprovalsPage {...props} />
      </Suspense>
    );
  if (root === "system-health") return <HealthPage {...props} />;
  return <OverviewPage {...props} />;
}
