"use strict";
const $ = id => document.getElementById(id);
let token = "", current = null, status = null, projectData = null, polling = false, revision = null;
const el = (tag, text, cls) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (cls) node.className = cls; return node; };
const parsed = value => value ? JSON.parse(value) : null;
function notify(text, error = false) { $("message").textContent = text; $("message").className = error ? "error" : ""; $("message").hidden = false; setTimeout(() => $("message").hidden = true, 8000); }
async function api(path, options = {}, override) {
  const response = await fetch("/api" + path, { ...options, headers: { "Authorization": "Bearer " + (override || token), "Content-Type": "application/json" } });
  if (!response.ok) { let body; try { body = await response.json(); } catch { body = {}; } throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || response.statusText)); }
  return response.json();
}
async function action(fn) { try { await fn(); await refresh(); } catch (error) { notify(error.message, true); } }
function badge(state) { return el("span", state.replaceAll("_", " "), "badge " + state); }
function setView(view) { for (const id of ["login", "workspace", "detail"]) $(id).hidden = id !== view; }
function disconnect() { token = ""; current = null; projectData = null; revision = null; $("token").value = ""; $("release-token").value = ""; $("connection").textContent = "Not connected"; $("logout").hidden = true; setView("login"); }
$("logout").onclick = disconnect;
$("connect-form").onsubmit = event => { event.preventDefault(); token = $("token").value; action(async () => { status = await api("/status"); $("token").value = ""; $("connection").textContent = "Factory connected"; $("logout").hidden = false; setView("workspace"); }); };
$("new-project").onclick = () => { $("profile").replaceChildren(...status.profiles.map(name => { const option = el("option", name); option.value = name; return option; })); $("create-dialog").showModal(); };
$("close-create").onclick = () => $("create-dialog").close();
$("create-form").onsubmit = event => { event.preventDefault(); action(async () => { const project = await api("/projects", { method: "POST", body: JSON.stringify({ name: $("name").value, brief: $("brief").value, profile: $("profile").value }) }); $("create-dialog").close(); $("create-form").reset(); current = project.id; revision = null; setView("detail"); }); };
function overview() { current = null; revision = null; setView(token ? "workspace" : "login"); if (token) action(async () => {}); }
$("back").onclick = overview; $("overview").onclick = overview;
async function refresh() {
  if (!token || polling) return;
  polling = true;
  try {
    const [health, projects] = await Promise.all([api("/status"), api("/projects")]);
    status = health;
    $("project-count").textContent = projects.length;
    $("build-status").textContent = health.workers.build_heartbeat ? "Online" : "Offline";
    $("release-status").textContent = Object.values(health.profile_features || {}).some(p => p.tester) ? "On demand" : "Not configured";
    $("memory-status").textContent = health.memorix_enabled ? "Memorix" : "Disabled";
    const missing = [];
    if (!health.model_configured) missing.push("model configuration");
    if (!health.omp_installed) missing.push("OMP installation");
    if (!health.workers.build_heartbeat) missing.push("build worker");
    if (!health.profiles.length) missing.push("verification profile");
    $("readiness").hidden = !missing.length;
    $("readiness").textContent = "Setup needed: " + missing.join(", ") + ". See the Linux setup guide.";
    const list = $("project-list"); list.replaceChildren();
    for (const project of projects) {
      const card = el("button", undefined, "card project-card"), meta = el("div", undefined, "project-meta");
      meta.append(el("span", project.profile.toUpperCase()), badge(project.state));
      card.append(meta, el("h2", project.name), el("p", project.brief.slice(0, 170)), el("div", "Revision " + project.revision + " · Open project →", "project-meta"));
      card.onclick = () => { current = project.id; revision = null; setView("detail"); action(async () => {}); };
      list.append(card);
    }
    if (!projects.length) list.append(el("div", "Your first project starts here. Add a brief to begin.", "empty"));
    if (current) {
      const projectId = current;
      const [project, events] = await Promise.all([api("/projects/" + projectId), loadEvents(projectId)]);
      if (current === projectId) { renderProject(project, events); await refreshStudio(project); }
    }
  } finally { polling = false; }
}
function renderProject(project, events) {
  projectData = project;
  renderWorkers(project, events);
  const active = project.jobs.some(job => ["queued", "running"].includes(job.state));
  $("project-name").textContent = project.name; $("project-brief").textContent = project.brief;
  $("project-profile").textContent = project.profile.toUpperCase() + " / REVISION " + project.revision;
  $("project-state").replaceWith(Object.assign(badge(project.state), { id: "project-state" }));
  const plan = parsed(project.plan), artifact = parsed(project.artifact);
  $("generate").disabled = active || !status.model_configured || !status.omp_installed;
  $("approve").disabled = active || !plan || plan.questions.length > 0 || !["awaiting_plan", "failed", "interrupted", "cancelled"].includes(project.state);
  $("plan-summary").textContent = plan ? plan.summary : "Ask the planner to turn this brief into an implementation plan, or save a reviewed plan below.";
  $("plan-questions").replaceChildren();
  if (plan?.questions.length) $("plan-questions").append(el("div", "Resolve before approval: " + plan.questions.join(" · "), "notice"));
  if (plan?.assumptions.length) $("plan-questions").append(el("p", "Assumptions: " + plan.assumptions.join(" · "), "muted"));
  $("tasks").replaceChildren();
  for (const task of plan?.tasks || []) { const item = el("div", undefined, "task"), criteria = el("ul"); for (const text of task.acceptance) criteria.append(el("li", text)); item.append(el("small", task.id), el("strong", task.title), criteria); $("tasks").append(item); }
  for (const flow of plan?.test_flows || []) {
    const item = el("div", undefined, "task");
    item.append(el("small", "TESTER FLOW · " + flow.kind.toUpperCase()), el("strong", flow.goal));
    const steps = el("ol"); flow.steps.forEach(text => steps.append(el("li", text))); item.append(steps); $("tasks").append(item);
  }
  if (revision !== project.revision) { $("plan-editor").value = JSON.stringify(plan || { summary: "Describe the approved scope here", assumptions: [], questions: [], tasks: [] }, null, 2); revision = project.revision; }
  $("save-plan").disabled = active;
  $("artifact-info").replaceChildren();
  if (artifact) { $("artifact-info").append(el("span", `${artifact.files} files · ${Math.round(artifact.bytes / 1024)} KB · revision ${artifact.revision}`), el("code", artifact.digest)); } else $("artifact-info").textContent = "Available after review and verification pass.";
  for (const id of ["download", "source"]) $(id).disabled = !artifact;
  $("staging").disabled = !artifact || active || project.state !== "ready" || !status.targets.includes(project.id + ":staging");
  const staged = artifact && project.deployments.some(d => d.environment === "staging" && d.digest === artifact.digest && d.state === "succeeded");
  $("production").disabled = !staged || active || project.state !== "ready" || !status.targets.includes(project.id + ":production");
  $("jobs").replaceChildren();
  for (const job of project.jobs) { const item = el("div", undefined, "job"); item.append(el("strong", job.kind + " "), badge(job.state), el("small", new Date(job.created * 1000).toLocaleString())); if (job.error) item.append(el("p", job.error)); const evidence = el("button", "Evidence", "quiet"); evidence.onclick = () => showEvidence(() => api("/jobs/" + job.id + "/evidence")); item.append(evidence); if (["queued", "running"].includes(job.state)) { const cancel = el("button", "Cancel", "quiet"); cancel.onclick = () => action(() => api("/jobs/" + job.id + "/cancel", { method: "POST" })); item.append(cancel); } $("jobs").append(item); }
  $("events").replaceChildren();
  for (const event of events.slice(-40).reverse()) { const item = el("div", undefined, "event"); item.append(el("time", new Date(event.created * 1000).toLocaleString()), el("strong", event.kind), el("pre", JSON.stringify(parsed(event.detail), null, 2))); $("events").append(item); }
  $("deployments").replaceChildren();
  for (const deployment of project.deployments) { const item = el("div", undefined, "deployment"); item.append(el("span", deployment.environment + " "), badge(deployment.state), el("p", deployment.digest.slice(0, 16))); $("deployments").append(item); }
}
$("generate").onclick = () => action(() => api("/projects/" + current + "/plan", { method: "POST" }));
$("save-plan").onclick = () => action(() => api("/projects/" + current + "/plan", { method: "PUT", body: JSON.stringify({ revision, plan: JSON.parse($("plan-editor").value) }) }));
$("approve").onclick = () => action(() => api("/projects/" + current + "/approve?revision=" + revision, { method: "POST" }));
$("staging").onclick = () => release("staging"); $("production").onclick = () => release("production");
function release(environment) { action(async () => { const releaseToken = $("release-token").value; if (environment === "production" && !releaseToken) throw new Error("Enter the separate production release token."); await api("/projects/" + current + "/deploy", { method: "POST", body: JSON.stringify({ environment, digest: parsed(projectData.artifact).digest }) }, environment === "production" ? releaseToken : undefined); $("release-token").value = ""; notify(environment + " release queued for the verified artifact."); }); }
async function showEvidence(load) { await action(async () => { $("evidence").textContent = JSON.stringify(await load(), null, 2); $("evidence-dialog").showModal(); }); }
$("source").onclick = () => showEvidence(() => api("/projects/" + current + "/source"));
$("close-evidence").onclick = () => $("evidence-dialog").close();
$("download").onclick = () => action(async () => { const response = await fetch("/api/projects/" + current + "/artifact", { headers: { Authorization: "Bearer " + token } }); if (!response.ok) throw new Error("Artifact download failed"); const url = URL.createObjectURL(await response.blob()), link = el("a"); link.href = url; link.download = "artifact-" + parsed(projectData.artifact).digest.slice(0, 12) + ".tar.gz"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); });
setInterval(() => { if (token && !document.hidden) refresh().catch(error => notify(error.message, true)); }, 4000);

async function loadEvents(project) {
  const events = [];
  let after = 0;
  while (true) {
    const page = await api("/projects/" + project + "/events?after=" + after);
    events.push(...page);
    if (page.length < 300) return events;
    after = page[page.length - 1].seq;
  }
}
function renderWorkers(project, events) {
  const tree = $("worker-tree"), graph = workerGraph(project, events);
  const opened = new Set([...tree.querySelectorAll('details[open]')].map(n => n.dataset.task));
  tree.replaceChildren();
  const planner = el("div", undefined, "worker-planner");
  planner.append(el("strong", "Planner · " + project.name), badge(project.state === "planning" ? "running" : project.plan ? "plan_available" : "not_started"));
  tree.append(planner);
  if (!graph.tasks.length) tree.append(el("p", "Workers appear when a plan is available. Planned workers have not necessarily started.", "muted"));
  for (const task of (graph.build?.kind === "verify" ? [] : graph.tasks)) {
    const node = el("details", undefined, "worker-node"); node.dataset.task = task.id; node.open = opened.has(task.id);
    const title = el("summary"); title.append(el("strong", "Worker · " + task.title), badge(task.state)); node.append(title);
    node.append(el("p", task.instructions), el("p", "Dependencies: " + (task.depends_on.join(", ") || "None"), "muted"));
    const criteria = el("ul"); task.acceptance.forEach(text => criteria.append(el("li", text))); node.append(criteria);
    const reviewer = el("div", undefined, "worker-review"); reviewer.append(el("span", "Independent reviewer"), badge(task.review)); node.append(reviewer);
    for (const event of task.events) { const detail = JSON.parse(event.detail); node.append(el("p", event.kind + (detail.attempt ? " · attempt " + detail.attempt : ""), "muted")); if (detail.findings?.length) node.append(el("p", detail.findings.join(" · "))); }
    if (task.job) { const button = el("button", "Changes & review evidence", "quiet"); button.onclick = () => showEvidence(() => api("/jobs/" + task.job + "/evidence")); node.append(button); }
    tree.append(node);
  }
  if (graph.tester !== "not_configured") {
    const tester = el("div", undefined, "worker-planner");
    tester.append(el("strong", "Tester agent · API & browser"), badge(graph.tester));
    if (graph.build) { const button = el("button", "Test report & screenshots", "quiet"); button.onclick = () => showTesting(graph.build.id); tester.append(button); }
    tree.append(tester);
  }
  const verifier = el("div", undefined, "worker-planner"); verifier.append(el("strong", "Verifier · configured checks & build"), badge(graph.verification)); tree.append(verifier);
  tree.append(el("p", "Worker acceptance is provisional until project verification passes. Execution uses the configured verifier; native Windows support is not yet implemented.", "muted"));
}

let studioProject = null;
let reviewedDraft = null;
async function refreshStudio(project) {
  const state = await api("/projects/" + project.id + "/studio");
  if (current !== project.id) return;
  $("studio-panel").hidden = !state.enabled;
  if (!state.enabled) return;
  if (studioProject !== project.id) { reviewedDraft = null; $("studio-diff-text").textContent = ""; studioProject = project.id; }
  $("studio-state").textContent = state.state;
  const active = project.jobs.some(j => ['queued', 'running'].includes(j.state));
  $("studio-starter").hidden = project.state !== 'draft';
  $("studio-open").disabled = active || project.state !== 'ready' || ['editing', 'preparing'].includes(state.state);
  $("studio-starter").disabled = active;
  $("studio-review").disabled = !['editing', 'stopped', 'failed'].includes(state.state);
  $("studio-apply").disabled = active || reviewedDraft !== state.id || !['stopped', 'failed'].includes(state.state);
  $("studio-stop").disabled = !['editing', 'preparing'].includes(state.state);
  $("studio-discard").disabled = ['none', 'imported', 'discarded'].includes(state.state);
  $("studio-help").textContent = state.state === 'preparing' ? 'Preparing the local editor and dependencies. This can take a few minutes.' : state.state === 'failed' ? 'Preview setup failed. Your draft is retained; check the local Node runtime and dependencies, then reopen.' : state.state === 'editing' ? 'Use the floating Studio button inside the preview. Save edits there, then choose Review changes here.' : state.state === 'imported' ? 'Edits are saved as a new revision. Verification must pass before this becomes the latest successful build.' : 'Open a verified Nuxt Studio project, or initialize the supplied starter for a new project.';
  $("studio-preview").hidden = !state.url;
  if (state.url) {
    if ($("studio-frame").getAttribute('src') !== state.url) $("studio-frame").src = state.url;
    $("studio-tab").href = state.url;
  } else $("studio-frame").removeAttribute('src');
}
async function studioAction(name) { return api('/projects/' + current + '/studio/' + name, {method:'POST'}); }
$("studio-open").onclick = () => action(async () => { reviewedDraft = null; await studioAction('start'); });
$("studio-starter").onclick = () => action(() => studioAction('starter'));
$("studio-stop").onclick = () => action(() => studioAction('stop'));
$("studio-discard").onclick = () => action(async () => { if (!confirm('Discard this draft? Verified builds will be kept.')) return; reviewedDraft = null; await studioAction('discard'); });
$("studio-review").onclick = () => action(async () => {
  const state = await studioAction('stop');
  const diff = await api('/projects/' + current + '/studio/diff');
  $("studio-diff-text").textContent = JSON.stringify(diff.files, null, 2);
  $("studio-diff").open = true;
  reviewedDraft = diff.files.length ? state.id : null;
  if (!diff.files.length) notify('No content changes yet. Reopen the draft to edit.');
});
$("studio-apply").onclick = () => action(async () => { await studioAction('apply'); reviewedDraft = null; notify('New content revision queued for testing. The previous verified build is preserved.'); });
async function showTesting(job) {
  await showEvidence(() => api('/jobs/' + job + '/evidence'));
  const evidence = await api('/jobs/' + job + '/evidence');
  const report = evidence['testing/report.json'] ? JSON.parse(evidence['testing/report.json']) : null;
  for (const item of report?.cases || []) {
    if (!item.screenshot) continue;
    const response = await fetch('/api/jobs/' + job + '/testing/' + item.screenshot, {headers:{Authorization:'Bearer ' + token}});
    if (!response.ok) continue;
    const url = URL.createObjectURL(await response.blob());
    const img = el('img'); img.src = url; img.alt = item.name + ' · ' + item.status; img.className = 'test-screenshot';
    $("evidence").append(img); img.onload = () => URL.revokeObjectURL(url);
  }
}
