const API = "";

// ---------- XSS discipline (Day-10 hardening) ----------
// Every value rendered below can originate from the legacy estate or from
// a registry card someone else published (table/column names, DAG owner
// fields, finding text, agent capability strings, audit actor/subject) —
// exactly the "untrusted content" category tools/untrusted_content.py's
// envelope discipline already treats as data-not-instructions on the
// backend. The frontend previously interpolated all of it into innerHTML
// unescaped, which is a real stored-XSS path (a `<` in a table comment,
// a `"` breaking out of an onclick attribute). esc() is the one place
// text enters markup from here on; nothing below builds an inline
// onclick="...('${data}')" attribute anymore — those are the second,
// attribute-breakout injection vector — event handling instead uses
// data-* attributes read by delegated listeners.
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[ch]);
}

// ---------- tab wiring ----------
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "dashboard") loadDashboard();
    if (btn.dataset.tab === "runs") loadRuns();
    if (btn.dataset.tab === "registry") loadRegistry();
  });
});

document.querySelectorAll(".subtab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".subtab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".subtab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("sub-" + btn.dataset.subtab).classList.add("active");
  });
});

function badge(text) {
  const safe = esc(text);
  return `<span class="badge badge-${safe}">${safe}</span>`;
}
function fmtTime(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString();
}

// ---------- Dashboard ----------
async function loadDashboard() {
  const d = await fetch(`${API}/api/dashboard`).then((r) => r.json());

  document.getElementById("stat-row").innerHTML = `
    <div class="stat-card health-${esc(d.fleet_health)}">
      <div class="value">${esc(d.fleet_health)}</div><div class="label">Fleet Health</div>
    </div>
    <div class="stat-card"><div class="value">${esc(d.discovered_pipelines)}</div><div class="label">Discovered Pipelines</div></div>
    <div class="stat-card"><div class="value">${esc(d.total_runs)}</div><div class="label">Migration Runs</div></div>
    <div class="stat-card"><div class="value">${esc(d.completed_runs)}</div><div class="label">Completed</div></div>
    <div class="stat-card"><div class="value">${esc(d.at_risk_runs)}</div><div class="label">At Risk</div></div>
    <div class="stat-card"><div class="value">${esc(d.blocked_runs)}</div><div class="label">Runs w/ Governance Denials</div></div>
  `;

  const run = d.active_run;
  if (!run) {
    document.getElementById("active-run-content").innerHTML = "<p class='hint'>No runs yet.</p>";
    return;
  }
  const findings = d.active_run_findings_sample
    .map((f) => `<div class="finding-card"><span class="ft">${esc(f.finding_type)}</span> — ${esc(f.table_id)} <span class="hint">(${esc(f.severity)})</span></div>`)
    .join("");
  document.getElementById("active-run-content").innerHTML = `
    <p><b>${esc(run.run_id)}</b> — ${esc(run.pipeline_id)} ${badge(run.state)}
      ${run.is_seeded_fixture ? '<span class="fixture-flag">⚠ SEEDED HISTORICAL FIXTURE</span>' : ""}</p>
    <p>Risk score: <b>${esc(d.active_run_risk_score)}/100</b></p>
    ${renderTimeline(run.state_history || [])}
    <h3 style="font-size:13px;color:var(--muted);text-transform:uppercase;">Sample findings</h3>
    ${findings || "<p class='hint'>No risk findings recorded yet for this run.</p>"}
    <p><a href="#" class="view-run-detail" data-run-id="${esc(run.run_id)}">View full run detail →</a></p>
  `;
  document.querySelector("#active-run-content .view-run-detail")?.addEventListener("click", (e) => {
    e.preventDefault();
    openRunsTabAndSelect(e.currentTarget.dataset.runId);
  });
}

function openRunsTabAndSelect(runId) {
  document.querySelector('.tab-btn[data-tab="runs"]').click();
  selectRun(runId);
}

function renderTimeline(history) {
  const items = history
    .map((h) => `<li>${badge(h.state)}<span class="ts">${esc(fmtTime(h.at))}</span></li>`)
    .join("");
  return `<ul class="timeline">${items}</ul>`;
}

// ---------- Runs ----------
async function loadRuns() {
  const runs = await fetch(`${API}/api/runs`).then((r) => r.json());
  const tbody = document.querySelector("#runs-table tbody");
  tbody.innerHTML = runs
    .map(
      (r) => `<tr class="clickable" data-run-id="${esc(r.run_id)}">
        <td>${esc(r.run_id)}</td><td>${esc(r.pipeline_id)}</td><td>${badge(r.state)}</td>
        <td>${esc(fmtTime(r.created_at))}</td><td>${r.is_seeded_fixture ? "⚠ fixture" : ""}</td>
      </tr>`
    )
    .join("");
  tbody.querySelectorAll("tr[data-run-id]").forEach((row) => {
    row.addEventListener("click", () => selectRun(row.dataset.runId));
  });
}

async function selectRun(runId) {
  document.getElementById("run-detail-panel").style.display = "block";
  document.getElementById("run-detail-title").textContent = "Run: " + runId;

  const run = await fetch(`${API}/api/runs/${encodeURIComponent(runId)}`).then((r) => r.json());

  const approveBox = document.getElementById("run-approve-box");
  approveBox.innerHTML = "";
  if (run.state === "READY_FOR_APPROVAL") {
    const btn = document.createElement("button");
    btn.className = "approve-btn";
    btn.textContent = "✅ Approve Cutover (human action)";
    btn.addEventListener("click", () => approveRun(runId));
    approveBox.appendChild(btn);
  }

  document.getElementById("sub-timeline").innerHTML = `
    <p>State: ${badge(run.state)} ${run.is_seeded_fixture ? `<span class="fixture-flag">⚠ ${esc(run.fixture_label || "SEEDED FIXTURE")}</span>` : ""}</p>
    <p class="hint">Pinned agent versions: ${esc(JSON.stringify(run.pinned_agents || {}))}</p>
    <p class="hint">Memory refs: ${esc(JSON.stringify(run.memory_refs || []))}</p>
    ${renderTimeline(run.state_history || [])}
  `;

  loadLineage(runId);
  loadReconciliation(runId);
  loadAudit(runId);
}

async function approveRun(runId) {
  const idToken = await getIdToken(); // frontend/static/auth.js — Firebase sign-in
  if (!idToken) {
    alert("Sign in required to approve a cutover.");
    return;
  }
  const plan = await fetch(`${API}/api/runs/${encodeURIComponent(runId)}/plan`)
    .then((r) => (r.ok ? r.json() : {}))
    .catch(() => ({}));
  const justification = prompt(
    `Approving run ${runId}\n` +
      `Plan hash: ${plan.plan_hash || "(unknown)"}\n` +
      `Steps: ${(plan.steps || []).length}, scheduled: ${(plan.steps || []).filter((s) => s.scheduled).length}\n\n` +
      `Enter a justification for this approval (required):`
  );
  if (!justification || justification.trim().length < 5) {
    alert("Approval cancelled — a justification (5+ characters) is required.");
    return;
  }

  const res = await fetch(`${API}/api/runs/${encodeURIComponent(runId)}/approve`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${idToken}`,
    },
    body: JSON.stringify({ justification: justification.trim() }),
  });
  const body = await res.json();
  if (!res.ok) {
    alert("Approval failed: " + (body.detail || JSON.stringify(body)));
    return;
  }
  alert(`Approved. Token: ${body.token_id}`);
  selectRun(runId);
}

// ---------- Lineage graph ----------
async function loadLineage(runId) {
  const data = await fetch(`${API}/api/runs/${encodeURIComponent(runId)}/lineage`).then((r) => r.json());
  const colorFor = (n) => {
    if (n.type === "pipeline") return "#a78bfa";
    if (n.classification === "PII") return "#ef4444";
    if (n.classification === "MASKED") return "#f59e0b";
    return "#64748b";
  };
  // vis.DataSet/vis.Network render node/edge "label" as text content, not
  // HTML, in vis-network's default configuration — no esc() needed here,
  // unlike every innerHTML template string above.
  const nodes = new vis.DataSet(
    data.nodes.map((n) => ({
      id: n.id,
      label: n.label,
      shape: n.type === "pipeline" ? "diamond" : "box",
      color: colorFor(n),
      font: { color: "#0f172a" },
    }))
  );
  const edges = new vis.DataSet(
    data.edges.map((e) => ({
      from: e.from,
      to: e.to,
      label: `${e.relationship} (${e.confidence})`,
      arrows: "to",
      font: { size: 9, color: "#94a3b8", strokeWidth: 0 },
      color: { color: e.source === "sql_view_parse" ? "#f59e0b" : "#38bdf8" },
    }))
  );
  new vis.Network(document.getElementById("lineage-graph"), { nodes, edges }, {
    layout: { improvedLayout: true },
    physics: { stabilization: true, barnesHut: { gravitationalConstant: -4000 } },
  });
}

// ---------- Reconciliation ----------
async function loadReconciliation(runId) {
  const checks = await fetch(`${API}/api/runs/${encodeURIComponent(runId)}/reconciliation`).then((r) => r.json());
  const rows = checks
    .map(
      (c) => `<tr>
        <td>${esc(c.check_type)}</td><td>${esc(JSON.stringify(c.source_value))}</td>
        <td>${esc(JSON.stringify(c.target_value))}</td><td>${badge(c.status)}</td>
        <td class="hint">${esc(fmtTime(c.checked_at))}</td>
      </tr>`
    )
    .join("");
  document.getElementById("sub-reconciliation").innerHTML = `
    <table><thead><tr><th>Check</th><th>Source</th><th>Target</th><th>Status</th><th>Checked</th></tr></thead>
    <tbody>${rows || "<tr><td colspan=5 class='hint'>No reconciliation runs yet.</td></tr>"}</tbody></table>
  `;
}

// ---------- Audit trail ----------
async function loadAudit(runId) {
  const events = await fetch(`${API}/api/runs/${encodeURIComponent(runId)}/audit`).then((r) => r.json());
  const rows = events
    .map(
      (e) => `<tr>
        <td class="hint">${esc(fmtTime(e.at))}</td><td>${esc(e.kind)}</td>
        <td>${esc(e.actor || "")}</td><td>${esc(e.subject || "")}</td><td>${badge(e.outcome || "")}</td>
      </tr>`
    )
    .join("");
  document.getElementById("sub-audit").innerHTML = `
    <table><thead><tr><th>Time</th><th>Kind</th><th>Actor</th><th>Subject</th><th>Outcome</th></tr></thead>
    <tbody>${rows || "<tr><td colspan=5 class='hint'>No audit events yet.</td></tr>"}</tbody></table>
  `;
}

// ---------- Registry ----------
async function loadRegistry() {
  const registry = await fetch(`${API}/api/registry`).then((r) => r.json());
  let html = "";
  for (const [agentId, versions] of Object.entries(registry)) {
    const rows = versions
      .map(
        (v) => `<tr>
          <td>${esc(v.version)}</td><td>${badge(v.status)}</td>
          <td>${esc(v.owner.team)} / ${esc(v.owner.department)}</td>
          <td>${esc((v.capabilities || []).join(", "))}</td>
          <td class="hint">published: ${esc(v.published_by)}<br>approved: ${esc(v.approved_by || "—")}</td>
        </tr>`
      )
      .join("");
    html += `<h3>${esc(agentId)}</h3>
      <table><thead><tr><th>Version</th><th>Status</th><th>Owner</th><th>Capabilities</th><th>Governance</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  }
  document.getElementById("registry-content").innerHTML = html || "<p class='hint'>No agents registered yet — run infrastructure/seed_registry.py.</p>";
}

// ---------- boot ----------
loadDashboard();
