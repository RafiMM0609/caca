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
    $("release-status").textContent = health.workers.deploy_heartbeat ? "Online" : "Offline";
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
      const [project, events] = await Promise.all([api("/projects/" + projectId), api("/projects/" + projectId + "/events")]);
      if (current === projectId) renderProject(project, events);
    }
  } finally { polling = false; }
}
function renderProject(project, events) {
  projectData = project;
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
