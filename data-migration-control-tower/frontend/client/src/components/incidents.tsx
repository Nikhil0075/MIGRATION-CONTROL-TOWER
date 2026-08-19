/**
 * Incidents and dead letters.
 *
 * Both of these were real data with no screen. `recovery.py` has written a
 * per-run `incidents` subcollection since the recovery loop was built, and
 * the dead-letter subscription has been forwarding since it was
 * provisioned — but a message that defeated a consumer was visible only by
 * running gcloud, and a root cause the system had already worked out was
 * visible only by opening Firestore.
 *
 * Split out of pages.tsx and lazy-loaded for the same reason
 * estate-wizard.tsx is: this module imports shared primitives FROM
 * pages.tsx, so a static import back would close the cycle at
 * module-evaluation time.
 */

import { h } from "preact";
import { useState } from "preact/hooks";
import { api } from "../api";
import { Icon } from "./icons";
import {
  ActionForm,
  DataTable,
  MetricCard,
  PageHeader,
  PageProps,
  PageState,
  EmptyState,
  Panel,
  estatePath,
  useResource,
} from "./pages";

type Row = Record<string, any>;

export function IncidentsPage(props: PageProps) {
  const [refresh, setRefresh] = useState(0);
  const state = useResource<Row>(estatePath("/api/v1/incidents", props.activeEstateId), refresh);
  const data = state.data as any;

  return (
    <>
      <PageHeader
        title="Incidents"
        description="What went wrong, what caused it, and what was done about it."
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
            <MetricCard label="Open incidents" value={data.open_count} />
            <MetricCard
              label="Recorded incidents"
              value={(data.incidents || []).length}
              detail="Investigated and remediated by the recovery loop"
            />
            <MetricCard
              label="Policy denials"
              value={(data.policy_denials || []).length}
              detail="Deterministic refusals, not model judgement"
            />
          </div>

          <Panel
            title="Failures and root causes"
            subtitle="Root cause is the canonical fact, not the display-wrapped recall narrative."
          >
            {!(data.incidents || []).length ? (
              <EmptyState
                kind="no-incidents"
                title="No incidents recorded"
                detail="One is opened only when validation fails, so an empty list is the healthy state."
              />
            ) : (
              <DataTable
                label="Incidents"
                rows={data.incidents}
                columns={[
                  { key: "signature", label: "Signature" },
                  { key: "table_ref", label: "Table" },
                  { key: "outcome", label: "Outcome", status: true },
                  { key: "root_cause", label: "Root cause" },
                  { key: "explained_by", label: "Explained by" },
                  { key: "fix", label: "Fix" },
                  { key: "run_id", label: "Run" },
                  { key: "opened_at", label: "Opened" },
                ]}
                onRow={(row) => props.onInspect("Incident", row)}
              />
            )}
          </Panel>

          <Panel
            title="Policy denials"
            subtitle="policy_engine.py takes no free-text estate content, so a table comment cannot influence these."
          >
            {!(data.policy_denials || []).length ? (
              <EmptyState kind="no-policy-violations" title="No policy denials in this window" />
            ) : (
              <DataTable
                label="Policy denials"
                rows={data.policy_denials}
                columns={[
                  { key: "agent_id", label: "Agent" },
                  { key: "action", label: "Action" },
                  { key: "resource_class", label: "Resource class" },
                  { key: "run_id", label: "Run" },
                  { key: "decided_at", label: "Decided" },
                ]}
                onRow={(row) => props.onInspect("Policy decision", row)}
              />
            )}
          </Panel>
        </>
      )}
    </>
  );
}

export function DeadLettersPage(props: PageProps) {
  const [refresh, setRefresh] = useState(0);
  const state = useResource<Row>("/api/v1/dead-letters", refresh);
  const canOperate = (props.estateRoles || props.session.roles).includes("operator");
  const data = state.data as any;
  const pending: Row[] = data?.pending || [];

  async function act(messageId: string, action: "replay" | "archive", justification: string) {
    const result = await api("/api/v1/dead-letters/" + encodeURIComponent(messageId) + "/" + action, {
      method: "POST",
      body: JSON.stringify({ justification }),
    });
    setRefresh((value) => value + 1);
    return result;
  }

  return (
    <>
      <PageHeader
        title="Dead letters"
        description="Messages a consumer could not handle after ten delivery attempts."
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
          <Panel
            title="Waiting"
            subtitle="Opening this page does not consume them — each lease is handed straight back."
          >
            {!pending.length ? (
              <EmptyState
                kind="no-dead-letters"
                title="No dead letters visible right now"
                detail="This is a read of a live queue, not a count: a message already held by another reader does not appear here, so an empty list is not by itself proof that nothing failed."
              />
            ) : (
              <>
                <DataTable
                  label="Dead letters"
                  rows={pending}
                  columns={[
                    {
                      key: "source_subscription",
                      label: "Gave up on",
                      // Pub/Sub stamps this itself when IT forwards a
                      // message. A message published straight onto the
                      // dead-letter topic carries the source in its body
                      // instead, and a body is untrusted content — so the
                      // two are never shown as the same kind of fact.
                      value: (row) =>
                        row.source_subscription
                          ? row.source_is_broker_asserted
                            ? row.source_subscription
                            : `${row.source_subscription} (declared by payload)`
                          : null,
                    },
                    { key: "reason", label: "Reason" },
                    { key: "run_id", label: "Run" },
                    { key: "delivery_attempts", label: "Attempts" },
                    { key: "published_at", label: "Published" },
                    { key: "message_id", label: "Message" },
                  ]}
                  onRow={(row) => props.onInspect("Dead letter", row)}
                />
                <div class="action-row">
                  {pending.map((message) => (
                    <span key={message.message_id} class="dead-letter-actions">
                      <ActionForm
                        title={"Replay " + String(message.message_id).slice(0, 8)}
                        description={
                          "Republish this message to the topic it came from (" +
                          (message.source_subscription || "unknown source") +
                          ") and remove it from the queue. The consumer handles it again; " +
                          "handlers are idempotent, so a duplicate is safe."
                        }
                        disabled={!canOperate}
                        onSubmit={(justification) => act(message.message_id, "replay", justification)}
                      />
                      <ActionForm
                        title={"Archive " + String(message.message_id).slice(0, 8)}
                        description={
                          "Keep a durable copy and drop the message. There is no second " +
                          "dead-letter queue behind this one, so archiving is the end of the line."
                        }
                        disabled={!canOperate}
                        onSubmit={(justification) => act(message.message_id, "archive", justification)}
                      />
                    </span>
                  ))}
                </div>
              </>
            )}
          </Panel>

          <Panel title="Replayed and archived" subtitle="Durable record of every action taken here.">
            {!(data.archive || []).length ? (
              <EmptyState kind="no-dead-letters" title="Nothing has been replayed or archived yet" />
            ) : (
              <DataTable
                label="Dead letter archive"
                rows={data.archive}
                columns={[
                  { key: "event", label: "Action", status: true },
                  { key: "source_subscription", label: "Gave up on" },
                  { key: "run_id", label: "Run" },
                  { key: "actor", label: "Actor" },
                  { key: "justification", label: "Justification" },
                  { key: "recorded_at", label: "When" },
                ]}
                onRow={(row) => props.onInspect("Dead letter record", row)}
              />
            )}
          </Panel>
        </>
      )}
    </>
  );
}

export function MemoryBankPage(props: PageProps) {
  const state = useResource<Row>("/api/v1/memory-bank");
  const data = state.data as any;
  const facts: Row[] = data?.facts || [];

  return (
    <>
      <PageHeader
        title="Memory Bank"
        description="Confirmed remediations, kept across runs and cited as evidence by later ones."
        generatedAt={state.generatedAt}
      />
      <PageState loading={state.loading} error={state.error} />
      {data && (
        <>
          <div class="metric-grid">
            <MetricCard
              label="Facts learned"
              value={facts.length}
              detail="One per normalized incident signature"
            />
            <MetricCard
              label="Facts reused"
              value={data.reused_facts}
              detail="Cited as evidence by a later run"
            />
          </div>

          <Panel
            title="Learned remediations"
            subtitle="Exact-match recall on a normalized signature — not semantic similarity, which this project has no embedding infrastructure to do honestly."
          >
            {!facts.length ? (
              <EmptyState
                kind="no-memory"
                title="Nothing learned yet"
                detail="A fact is written only after an incident is confirmed resolved, so this fills up as runs recover from real defects."
              />
            ) : (
              <DataTable
                label="Memory Bank facts"
                rows={facts}
                columns={[
                  { key: "signature", label: "Signature" },
                  { key: "root_cause", label: "Root cause" },
                  { key: "fix", label: "Confirmed fix" },
                  {
                    key: "recalled_by_count",
                    label: "Reused by runs",
                  },
                  { key: "confirmations", label: "Confirmations" },
                  { key: "first_learned_at", label: "First learned" },
                  { key: "last_confirmed_at", label: "Last confirmed" },
                ]}
                onRow={(row) => props.onInspect("Memory Bank fact", row)}
              />
            )}
          </Panel>

          <Panel title="What this is, and is not">
            <ul class="plain-list">
              <li>
                <strong>Reused by runs</strong> counts later runs that cited a fact as
                evidence. <strong>Confirmations</strong> counts how often it was
                re-confirmed after a successful remediation. They are different numbers
                and only the first demonstrates cross-run learning.
              </li>
              <li>
                A recalled fact never replaces the deterministic re-validation that
                follows it. Memory proposes; reconciliation decides.
              </li>
              <li>
                This collection is cross-estate by design — a fact confirmed on one
                estate is available to a later run on another. That is the point, and
                also the reason a signature carries a source table name across an estate
                boundary.
              </li>
            </ul>
          </Panel>
        </>
      )}
    </>
  );
}

/** Human-readable statement of the plan-hash binding. */
const BINDING_NOTE: Record<string, string> = {
  intact: "Bound to the current plan — cutover will be accepted.",
  stale: "The plan changed after approval. Cutover will be REFUSED until re-approved.",
  no_plan: "No migration plan recorded yet, so there is nothing to bind to.",
};

export function ApprovalsPage(props: PageProps) {
  const [refresh, setRefresh] = useState(0);
  const state = useResource<Row>(estatePath("/api/v1/approvals", props.activeEstateId), refresh);
  const data = state.data as any;
  const awaiting: Row[] = data?.awaiting || [];
  const decided: Row[] = data?.decided || [];

  const columns = [
    { key: "run_id", label: "Run" },
    { key: "status", label: "Status", status: true },
    {
      key: "binding",
      label: "Plan binding",
      // The fact this page exists for. A token is issued against a plan
      // hash and consume() refuses the cutover if the plan has moved on —
      // previously discovered at cutover time, long after someone clicked
      // approve.
      value: (row: Row) => BINDING_NOTE[String(row.binding)] || row.binding,
    },
    {
      key: "checks_failed",
      label: "Failed checks",
      value: (row: Row) =>
        row.checks_total ? `${row.checks_failed} of ${row.checks_total}` : null,
    },
    { key: "critical_findings", label: "Critical findings" },
    { key: "approved_by", label: "Approved by" },
    { key: "expires_at", label: "Expires" },
  ];

  return (
    <>
      <PageHeader
        title="Approvals"
        description="The human gate, and the evidence behind each decision."
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
            <MetricCard label="Awaiting approval" value={awaiting.length} />
            <MetricCard
              label="Stale bindings"
              value={data.stale_bindings}
              detail="Approved against a plan that has since changed"
            />
          </div>

          {data.stale_bindings > 0 && (
            <p class="inline-alert warning" role="status">
              {data.stale_bindings} approval(s) are bound to a plan that has since
              changed. Cutover will be refused for these until they are approved again —
              this is the binding working, not a fault.
            </p>
          )}

          <Panel
            title="Awaiting a decision"
            subtitle="Approve from the run page; this inbox shows what to weigh before doing so."
          >
            {!awaiting.length ? (
              <EmptyState kind="no-approvals" title="Nothing is waiting for approval" />
            ) : (
              <DataTable
                label="Awaiting approval"
                rows={awaiting}
                columns={columns}
                onRow={(row) => props.navigate(String(row.route || "").replace(/^\//, ""))}
              />
            )}
          </Panel>

          <Panel title="Decided" subtitle="Append-only history; an approval is never overwritten.">
            {!decided.length ? (
              <EmptyState kind="no-approvals" title="No approvals have been decided yet" />
            ) : (
              <DataTable
                label="Decided approvals"
                rows={decided}
                columns={columns}
                onRow={(row) => props.onInspect("Approval", row)}
              />
            )}
          </Panel>

          <Panel title="What this page cannot do">
            <ul class="plain-list">
              <li>
                It cannot approve anything. The only path from READY_FOR_APPROVAL to
                APPROVED is the authenticated approver endpoint, which is a separate
                identity from every agent — the Cutover Agent cannot approve its own
                work.
              </li>
              <li>
                A token is bound to <strong>run + plan hash</strong>. Changing the plan
                after approval does not silently invalidate the run; it makes the
                cutover refuse, which is what <strong>Plan binding</strong> above
                reports ahead of time.
              </li>
            </ul>
          </Panel>
        </>
      )}
    </>
  );
}
