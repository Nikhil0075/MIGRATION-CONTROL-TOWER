/**
 * Estate onboarding wizard (Day 11 Phase 6, master doc §32.2).
 *
 * The adoption path §32.2 asks for is deliberately boring: describe the
 * estate, bind credentials through secrets, select a Migration Pack, run
 * a read-only assessment. This is that path, in the console, so onboarding
 * a second estate is configuration rather than a repository edit.
 *
 * CREDENTIAL RULE, and it is the reason several things here look
 * roundabout: this wizard collects *references* — a Secret Manager name,
 * or the NAME of an environment variable — never a secret value. There is
 * no password input anywhere in this file, and there must never be one.
 * The API enforces it too (ConnectionProfileModel forbids extra keys, and
 * contracts/metadata_model.json's ConnectionProfile is a closed schema),
 * but the UI must not offer a field the backend would then have to
 * reject.
 *
 * The adapter list and pack list are both fetched, not hardcoded, so
 * registering a new source family (one line in tools/adapters/__init__.py)
 * makes it selectable here with no frontend change.
 */

import { cloneElement } from "preact";
import { useMemo, useState } from "preact/hooks";
import { api, idempotencyKey } from "../api";
import { PageHeader, PageProps, Panel, useResource } from "./pages";

type AdapterType = {
  adapter_type: string;
  capabilities: string[];
  summary?: string;
};

type Pack = {
  pack_id: string;
  version: string;
  display_name?: string;
  default_mode: string;
  execution_supported: boolean;
};

type ValidationResult = {
  status: string;
  detail?: string | null;
  object_count?: number | null;
  latency_ms?: number | null;
};

const STEPS = [
  "Identity",
  "Source",
  "Connection",
  "Pack",
  "Review",
] as const;

const SLUG = /^[a-z0-9][a-z0-9-]*$/;
const SOURCE_SLUG = /^[a-z0-9][a-z0-9._-]*$/;

let fieldSequence = 0;

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: any;
}) {
  // The hint is associated with aria-describedby rather than nested inside
  // the <label>. Wrapping it in the label folds it into the control's
  // accessible NAME, so a screen reader announces "Database, Optional;
  // passed to the adapter as configuration" as the field's name, and two
  // fields whose hints share a word become ambiguous to name-based
  // lookup. Name and description are different things; keep them apart.
  const id = useMemo(() => `wizard-field-${++fieldSequence}`, []);
  const hintId = hint ? `${id}-hint` : undefined;
  const control =
    children && typeof children === "object" && "props" in children
      ? cloneElement(children, { id, "aria-describedby": hintId })
      : children;

  return (
    <div class="wizard-field">
      <label class="wizard-field-label" for={id}>
        {label}
      </label>
      {control}
      {hint && (
        <span class="wizard-field-hint" id={hintId}>
          {hint}
        </span>
      )}
    </div>
  );
}

export function EstateWizard({ session, navigate }: PageProps) {
  const canOperate = session.roles?.includes("operator");
  const adapters = useResource<AdapterType[]>("/api/v1/adapter-types");
  const assessments = useResource<any>("/api/v1/assessments");
  const packs: Pack[] = assessments.data?.packs || [];

  const [step, setStep] = useState(0);
  const [estateId, setEstateId] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [sourceId, setSourceId] = useState("");
  const [adapter, setAdapter] = useState("");
  const [database, setDatabase] = useState("");
  const [hostEnv, setHostEnv] = useState("SQLSERVER_HOST");
  const [portEnv, setPortEnv] = useState("SQLSERVER_PORT");
  const [userEnv, setUserEnv] = useState("SQLSERVER_USER");
  const [secretRef, setSecretRef] = useState("");
  const [passwordEnv, setPasswordEnv] = useState("SQLSERVER_PASSWORD");
  const [packId, setPackId] = useState("");
  const [datasetEnv, setDatasetEnv] = useState("BQ_DATASET");
  const [justification, setJustification] = useState("");

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState(false);
  const [validation, setValidation] = useState<ValidationResult | null>(null);

  const selectedAdapter = useMemo(
    () => (adapters.data || []).find((a) => a.adapter_type === adapter),
    [adapters.data, adapter],
  );

  // A static-file source (the Oracle DDL corpus, DAG artifacts) has no
  // server to reach, so the connection step is skipped rather than shown
  // with fields that mean nothing.
  const needsConnection = Boolean(
    selectedAdapter && selectedAdapter.capabilities.includes("health"),
  );

  const identityValid =
    SLUG.test(estateId) && estateId.length >= 2 && displayName.trim().length >= 2;
  const sourceValid = SOURCE_SLUG.test(sourceId) && Boolean(adapter);
  const connectionValid =
    !needsConnection || Boolean(secretRef.trim() || passwordEnv.trim());
  const reviewValid = justification.trim().length >= 8;

  const stepValid = [
    identityValid,
    sourceValid,
    connectionValid,
    true, // pack selection is optional — an estate may be assessed first
    reviewValid,
  ][step];

  function buildPayload() {
    return {
      estate_id: estateId,
      display_name: displayName,
      sources: [
        {
          source_id: sourceId,
          adapter,
          config: database ? { database } : {},
          pack_id: packId || null,
          connection_profile: needsConnection
            ? {
                host_env: hostEnv || null,
                port_env: portEnv || null,
                user_env: userEnv || null,
                // References only. Never a value.
                password_secret_ref: secretRef.trim() || null,
                password_env: passwordEnv.trim() || null,
              }
            : null,
        },
      ],
      target: { system: "bigquery", dataset_env: datasetEnv || null },
      justification,
    };
  }

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await api("/api/v1/estates", {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey("estate-create") },
        body: JSON.stringify(buildPayload()),
      });
      setCreated(true);
    } catch (err: any) {
      setError(err?.message || "The estate could not be created.");
    } finally {
      setBusy(false);
    }
  }

  async function validateConnection() {
    setBusy(true);
    setError(null);
    try {
      const result = await api<ValidationResult>(
        `/api/v1/estates/${encodeURIComponent(estateId)}/sources/${encodeURIComponent(
          sourceId,
        )}/validate`,
        { method: "POST" },
      );
      setValidation(result.data);
    } catch (err: any) {
      setError(err?.message || "The connection could not be validated.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Onboard an estate"
        description="Describe the estate, bind credential references, pick a Migration Pack, then validate."
      />

      <ol class="wizard-steps" aria-label="Onboarding steps">
        {STEPS.map((label, index) => (
          <li
            class={`wizard-step${index === step ? " is-current" : ""}${
              index < step ? " is-done" : ""
            }`}
            aria-current={index === step ? "step" : undefined}
          >
            <span class="wizard-step-index">{index + 1}</span>
            <span class="wizard-step-label">{label}</span>
          </li>
        ))}
      </ol>

      {!canOperate && (
        <p class="notice notice-warning" role="status">
          Onboarding an estate requires the operator role. You can review the
          steps but not save.
        </p>
      )}

      {step === 0 && (
        <Panel title="Identity" subtitle="How this estate is named everywhere else">
          <Field
            label="Estate ID"
            hint="Lowercase letters, digits and hyphens. Used as the scope key on runs, wave capacity, connection health and role grants — it cannot be changed later."
          >
            <input
              type="text"
              value={estateId}
              placeholder="acme-finance"
              onInput={(e) => setEstateId((e.target as HTMLInputElement).value)}
            />
          </Field>
          {estateId && !SLUG.test(estateId) && (
            <p class="notice notice-error" role="alert">
              An estate ID may contain only lowercase letters, digits and
              hyphens, and must start with a letter or digit.
            </p>
          )}
          <Field label="Display name">
            <input
              type="text"
              value={displayName}
              placeholder="ACME Finance (production)"
              onInput={(e) => setDisplayName((e.target as HTMLInputElement).value)}
            />
          </Field>
        </Panel>
      )}

      {step === 1 && (
        <Panel
          title="Source"
          subtitle="Which system this estate migrates from"
        >
          <Field label="Source ID" hint="Unique within this estate. Also the Wave Manager concurrency key.">
            <input
              type="text"
              value={sourceId}
              placeholder="finance-sqlserver"
              onInput={(e) => setSourceId((e.target as HTMLInputElement).value)}
            />
          </Field>
          <Field
            label="Adapter"
            hint="Source families this deployment can talk to. Registering a new adapter makes it appear here with no frontend change."
          >
            <select
              value={adapter}
              onChange={(e) => setAdapter((e.target as HTMLSelectElement).value)}
            >
              <option value="">Select an adapter…</option>
              {(adapters.data || []).map((item) => (
                <option value={item.adapter_type}>
                  {item.adapter_type} · {item.capabilities.join(", ")}
                </option>
              ))}
            </select>
          </Field>
          {selectedAdapter && !selectedAdapter.capabilities.includes("transfer") && (
            <p class="notice notice-warning" role="status">
              {selectedAdapter.adapter_type} supports discovery only — this
              source can be assessed, but not migrated. That is a property of
              the adapter, not a configuration mistake.
            </p>
          )}
          <Field label="Database" hint="Optional; passed to the adapter as configuration.">
            <input
              type="text"
              value={database}
              placeholder="FinanceDW"
              onInput={(e) => setDatabase((e.target as HTMLInputElement).value)}
            />
          </Field>
        </Panel>
      )}

      {step === 2 && (
        <Panel
          title="Connection"
          subtitle="References only — no credential is entered, stored or displayed here"
        >
          {!needsConnection ? (
            <p class="notice" role="status">
              {adapter || "This adapter"} reads static files and has no server
              to connect to. Nothing to configure.
            </p>
          ) : (
            <>
              <p class="notice" role="status">
                Every field below names <strong>where a value lives</strong>,
                never the value itself. The control plane resolves them at
                connect time; secrets never reach this console.
              </p>
              <Field label="Host from environment variable">
                <input
                  type="text"
                  value={hostEnv}
                  onInput={(e) => setHostEnv((e.target as HTMLInputElement).value)}
                />
              </Field>
              <Field label="Port from environment variable">
                <input
                  type="text"
                  value={portEnv}
                  onInput={(e) => setPortEnv((e.target as HTMLInputElement).value)}
                />
              </Field>
              <Field label="User from environment variable">
                <input
                  type="text"
                  value={userEnv}
                  onInput={(e) => setUserEnv((e.target as HTMLInputElement).value)}
                />
              </Field>
              <Field
                label="Password — Secret Manager reference"
                hint="A secret NAME, e.g. finance-db-password, or a full projects/…/secrets/…/versions/latest path."
              >
                <input
                  type="text"
                  value={secretRef}
                  placeholder="finance-db-password"
                  onInput={(e) => setSecretRef((e.target as HTMLInputElement).value)}
                />
              </Field>
              <Field
                label="Password — environment variable fallback"
                hint="Used only when the Secret Manager reference cannot be resolved. A NAME, never a value. The control plane logs a warning whenever this path is taken."
              >
                <input
                  type="text"
                  value={passwordEnv}
                  onInput={(e) => setPasswordEnv((e.target as HTMLInputElement).value)}
                />
              </Field>
            </>
          )}
        </Panel>
      )}

      {step === 3 && (
        <Panel
          title="Migration Pack"
          subtitle="Source-family rules: classification, dialect notes, data types, scheduled tables"
        >
          <Field
            label="Pack"
            hint="Optional. Without one, the plan derives its targets from discovered metadata."
          >
            <select
              value={packId}
              onChange={(e) => setPackId((e.target as HTMLSelectElement).value)}
            >
              <option value="">No pack — derive from the catalog</option>
              {packs.map((pack) => (
                <option value={pack.pack_id}>
                  {pack.pack_id} · v{pack.version} ·{" "}
                  {pack.execution_supported ? "execution" : "assessment only"}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Target dataset from environment variable">
            <input
              type="text"
              value={datasetEnv}
              onInput={(e) => setDatasetEnv((e.target as HTMLInputElement).value)}
            />
          </Field>
        </Panel>
      )}

      {step === 4 && (
        <Panel title="Review" subtitle="Nothing is saved until you confirm">
          <dl class="wizard-summary">
            <dt>Estate</dt>
            <dd>
              {displayName} <code>{estateId}</code>
            </dd>
            <dt>Source</dt>
            <dd>
              <code>{sourceId}</code> via {adapter}
              {database ? ` (${database})` : ""}
            </dd>
            <dt>Credential</dt>
            <dd>
              {needsConnection
                ? secretRef
                  ? `Secret Manager reference ${secretRef}`
                  : `environment variable ${passwordEnv}`
                : "Not required"}
            </dd>
            <dt>Pack</dt>
            <dd>{packId || "None — derive from the catalog"}</dd>
          </dl>

          <Field label="Justification" hint="Recorded in the operation audit trail. At least 8 characters.">
            <textarea
              rows={3}
              value={justification}
              onInput={(e) => setJustification((e.target as HTMLTextAreaElement).value)}
            />
          </Field>

          {!created ? (
            <button
              type="button"
              class="button button-primary"
              disabled={!canOperate || busy || !reviewValid}
              onClick={save}
            >
              {busy ? "Saving…" : "Create estate"}
            </button>
          ) : (
            <>
              <p class="notice notice-success" role="status">
                Estate <code>{estateId}</code> created.
              </p>
              {needsConnection && (
                <button
                  type="button"
                  class="button"
                  disabled={busy}
                  onClick={validateConnection}
                >
                  {busy ? "Validating…" : "Validate connection"}
                </button>
              )}
              {validation && (
                <dl class="wizard-summary" aria-live="polite">
                  <dt>Status</dt>
                  <dd>{validation.status}</dd>
                  <dt>Detail</dt>
                  <dd>{validation.detail || "—"}</dd>
                  <dt>Objects</dt>
                  <dd>{validation.object_count ?? "—"}</dd>
                  <dt>Latency</dt>
                  <dd>
                    {validation.latency_ms == null ? "—" : `${validation.latency_ms} ms`}
                  </dd>
                </dl>
              )}
              <button
                type="button"
                class="button"
                onClick={() => navigate("estates")}
              >
                Back to estates
              </button>
            </>
          )}
        </Panel>
      )}

      {error && (
        <p class="notice notice-error" role="alert">
          {error}
        </p>
      )}

      <div class="wizard-actions">
        <button
          type="button"
          class="button"
          disabled={step === 0 || created}
          onClick={() => setStep(Math.max(0, step - 1))}
        >
          Back
        </button>
        <button
          type="button"
          class="button button-primary"
          disabled={step === STEPS.length - 1 || !stepValid}
          onClick={() => {
            // Skip the connection step entirely for static-file sources
            // rather than showing fields that would mean nothing.
            const next = step + 1;
            setStep(next === 2 && !needsConnection ? 3 : next);
          }}
        >
          Next
        </button>
        <button
          type="button"
          class="button"
          onClick={() => navigate("estates")}
        >
          Cancel
        </button>
      </div>
    </>
  );
}
