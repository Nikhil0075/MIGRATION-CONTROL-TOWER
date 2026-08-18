import { h } from "preact";
import { useState } from "preact/hooks";
import { Icon } from "./icons";
import {
  DataTable,
  MetricCard,
  PageHeader,
  PageProps,
  PageState,
  Panel,
  StatusPill,
  useResource,
} from "./pages";

type RecordRow = Record<string, any>;

export function LineagePage(props: PageProps) {
  const state = useResource<RecordRow>("/api/v1/lineage");
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

export function EvaluationsPage(props: PageProps) {
  const state = useResource<RecordRow>("/api/v1/evaluations");
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
