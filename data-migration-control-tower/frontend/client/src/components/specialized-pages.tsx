import { h } from "preact";
import { useState } from "preact/hooks";
import { Icon } from "./icons";
import { LineageGraph } from "./lineage-graph";
import {
  DataTable,
  EmptyState,
  MetricCard,
  PageHeader,
  PageProps,
  PageState,
  Panel,
  StatusPill,
  estatePath,
  useResource,
} from "./pages";

type RecordRow = Record<string, any>;

export function LineagePage(props: PageProps) {
  // Which run is drawn is now an explicit choice rather than an implicit
  // one. The endpoint picks the newest run that actually HAS a catalog;
  // this lets an operator override that, and — just as important — see
  // which run they are looking at. It previously drew the newest run full
  // stop, which is usually a queued one with nothing in it, so the page
  // rendered an empty graph and read as broken.
  const [runId, setRunId] = useState<string>("");
  const basePath = estatePath("/api/v1/lineage", props.activeEstateId);
  const path = runId
    ? `${basePath}${basePath.includes("?") ? "&" : "?"}run_id=${encodeURIComponent(runId)}`
    : basePath;
  const state = useResource<RecordRow>(path);
  const [filter, setFilter] = useState("ALL");
  const availableRuns: any[] = state.data?.available_runs || [];
  const edges = state.data?.edges || [];
  const linked = new Set<string>(edges.flatMap((edge: any) => [edge.from, edge.to]));
  const nodes = (state.data?.nodes || []).filter(
    (node: any) =>
      filter === "ALL" ||
      node.classification === filter ||
      node.type === filter.toLowerCase(),
  );
  const unconnected = nodes.filter((node: any) => !linked.has(node.id));
  const bySchema: Record<string, any[]> = {};
  for (const node of unconnected) {
    const schema = String(node.label || node.id).split(".")[0] || "other";
    (bySchema[schema] ||= []).push(node);
  }
  return (
    <>
      <PageHeader
        title="Lineage"
        description="Asset relationships, provenance and impact evidence."
        generatedAt={state.generatedAt}
        actions={
          <>
            {availableRuns.length > 0 && (
              <select
                value={runId || state.data?.run_id || ""}
                onChange={(event) => setRunId(event.currentTarget.value)}
                aria-label="Lineage run"
              >
                {availableRuns.map((run: any) => (
                  <option key={run.run_id} value={run.run_id}>
                    {run.run_id} · {run.state}
                  </option>
                ))}
              </select>
            )}
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
          </>
        }
      />
      <PageState loading={state.loading} error={state.error} status={state.status} />
      {state.data && (
        <div class="lineage-layout">
          <Panel
            title="Lineage graph"
            subtitle={
              state.data.run_id
                ? `${nodes.length - unconnected.length} related of ${nodes.length} assets · ${edges.length} relationships · run ${state.data.run_id}`
                : "No run in this estate has been discovered yet, so there is nothing to draw."
            }
            className="lineage-panel"
          >
            <LineageGraph
              nodes={nodes}
              edges={edges}
              onSelect={(node: any) =>
                props.onInspect("Lineage asset", {
                  ...node,
                  relationships: edges.filter(
                    (edge: any) => edge.from === node.id || edge.to === node.id,
                  ),
                })
              }
            />
          </Panel>

          {/* The assets with no discovered relationships. Deliberately a
              dense list rather than cards the same size as the graph
              nodes: 42 of 48 tables were unconnected in the observed run,
              and giving each one a large card buried the six edges that
              are the actual lineage. */}
          <Panel
            title="Catalogued, no relationships found"
            subtitle="Discovered assets that nothing in this run links to or from."
          >
            {!unconnected.length ? (
              <p class="empty-state">Every catalogued asset appears in the graph.</p>
            ) : (
              <div class="asset-chips">
                {Object.entries(bySchema).map(([schema, items]: [string, any]) => (
                  <div class="asset-chip-group" key={schema}>
                    <h3>
                      {schema} <small>{items.length}</small>
                    </h3>
                    <ul>
                      {items.map((node: any) => (
                        <li key={node.id}>
                          <button
                            class={`asset-chip ${node.classification === "PII" ? "is-pii" : ""}`}
                            onClick={() => props.onInspect("Lineage asset", node)}
                          >
                            {String(node.label).split(".").slice(1).join(".") || node.label}
                            {node.classification === "PII" && <span class="chip-tag">PII</span>}
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            )}
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

export function EvaluationsPage(props: PageProps) {
  const state = useResource<RecordRow>(estatePath("/api/v1/evaluations", props.activeEstateId));
  const scale = state.data?.scale_metrics;
  // Deploy & Harden Phase 4d: three DISTINCT scale measurements
  // (docs/EVALUATION.md), never folded into one number — control-plane
  // object-count tiers, real rows/bytes actually moved, and concurrent
  // operational load. scale_metrics_by_tier keys are pipeline_count as
  // strings (Firestore document IDs); sorted numerically, largest
  // first, so the flagship tier reads first.
  const tiersByCount: Record<string, RecordRow> = state.data?.scale_metrics_by_tier || {};
  const sortedTiers = Object.values(tiersByCount).sort(
    (a: RecordRow, b: RecordRow) => (b.pipeline_count || 0) - (a.pipeline_count || 0)
  );
  const dataPlane = state.data?.data_plane_metrics;
  const operationalLoad = state.data?.operational_load_metrics;
  return (
    <>
      <PageHeader
        title="Evaluations"
        description="Scenario evidence and three distinct scale measurements — control-plane, data-plane, and operational load."
        generatedAt={state.generatedAt}
      />
      <PageState loading={state.loading} error={state.error} status={state.status} />
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
            title="Control-plane scale"
            subtitle="Metadata object definitions through schema validation, wave scheduling, and policy decisions — moves zero rows, zero bytes"
          >
            {sortedTiers.length ? (
              <div class="workspace-grid">
                {sortedTiers.map((tier: RecordRow) => (
                  <div class="metric-grid" key={tier.pipeline_count}>
                    <MetricCard
                      label="Object definitions"
                      value={tier.pipeline_count?.toLocaleString?.() ?? tier.pipeline_count}
                    />
                    <MetricCard
                      label="Schema throughput"
                      value={`${tier.schema_validation?.throughput_per_sec ?? "—"} rec/sec`}
                    />
                    <MetricCard
                      label="Wave throughput"
                      value={`${tier.wave_scheduling?.throughput_per_sec ?? "—"} items/sec`}
                    />
                    <MetricCard
                      label="Model calls"
                      value={tier.model_calls ?? 0}
                      detail="control-plane-only — never scales with tier"
                    />
                  </div>
                ))}
              </div>
            ) : scale ? (
              <div class="metric-grid">
                <MetricCard label="Pipeline definitions" value={scale.pipeline_count} />
                <MetricCard label="Schema p95" value={`${scale.schema_validation?.p95_ms} ms`} />
                <MetricCard label="Wave total" value={`${scale.wave_scheduling?.total_duration_ms} ms`} />
                <MetricCard label="Policy p95" value={`${scale.policy_decisions?.p95_ms} ms`} />
              </div>
            ) : (
              <EmptyState title="Scale report not configured" detail={state.data.scale_report_reason}>
                <StatusPill value="NOT_CONFIGURED" />
              </EmptyState>
            )}
          </Panel>

          <Panel
            title="Data-plane scale"
            subtitle="Real rows/bytes actually moved through a live migration — distinct from control-plane object counts"
          >
            {dataPlane ? (
              <div class="metric-grid">
                <MetricCard label="Rows moved" value={dataPlane.rows_moved?.toLocaleString?.() ?? dataPlane.rows_moved} />
                <MetricCard label="Executor" value={dataPlane.executor} />
                <MetricCard label="Throughput" value={`${dataPlane.throughput_rows_per_sec ?? "—"} rows/sec`} />
                <MetricCard label="Status" value={dataPlane.status} status />
              </div>
            ) : (
              <EmptyState title="Data-plane report not configured" detail={state.data.data_plane_reason}>
                <StatusPill value="NOT_CONFIGURED" />
              </EmptyState>
            )}
          </Panel>

          <Panel
            title="Operational load"
            subtitle="Concurrent runs, Pub/Sub backlog, and live fleet state under simultaneous load"
          >
            {operationalLoad ? (
              <div class="metric-grid">
                <MetricCard
                  label="Concurrent calls"
                  value={operationalLoad.concurrent_load?.concurrent_runs}
                />
                <MetricCard
                  label="Throughput"
                  value={`${operationalLoad.concurrent_load?.throughput_per_sec ?? "—"} calls/sec`}
                />
                <MetricCard
                  label="Latency p95"
                  value={`${operationalLoad.concurrent_load?.latency_p95_ms ?? "—"} ms`}
                />
                <MetricCard
                  label="Fleet deployed"
                  value={`${operationalLoad.fleet_state?.deployed_service_count ?? 0} / ${operationalLoad.fleet_state?.expected_service_count ?? "—"}`}
                />
              </div>
            ) : (
              <EmptyState title="Operational load report not configured" detail={state.data.operational_load_reason}>
                <StatusPill value="NOT_CONFIGURED" />
              </EmptyState>
            )}
          </Panel>
        </div>
      )}
    </>
  );
}
