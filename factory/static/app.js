"use strict";
const $ = id => document.getElementById(id);
let token = "", current = null, status = null, projectData = null, polling = false, revision = null, editNotice = "";
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
// Planner questions are answered in a form rather than by editing plan JSON: the operator is
// not technical, and an answer has to reach the assumption record, every task instruction and
// every test flow. Hand-editing made that the operator's job to remember, and forgetting the
// test flows once failed a build whose application was correct.
function renderQuestions(plan, active) {
  const box = $("plan-questions");
  if (!plan?.questions?.length) return;
  box.append(el("h3", "Pertanyaan dari planner"));
  box.append(el("p", "Jawab pertanyaan ini sebelum membangun. Jawaban otomatis masuk ke instruksi worker dan skenario pengujian.", "muted"));
  const form = el("form");
  form.className = "question-form";
  for (const [index, question] of plan.questions.entries()) {
    const field = el("div", undefined, "question");
    const label = el("label", question);
    label.htmlFor = "answer-" + index;
    const input = el("textarea");
    input.id = "answer-" + index;
    input.rows = 2;
    input.dataset.question = question;
    input.placeholder = "Tulis jawaban di sini";
    input.disabled = active;
    field.append(label, input);
    form.append(field);
  }
  const submit = el("button", "Simpan jawaban", "primary");
  submit.type = "submit";
  submit.disabled = active;
  form.append(submit);
  form.onsubmit = event => {
    event.preventDefault();
    const answers = {};
    for (const field of form.querySelectorAll("textarea[data-question]")) {
      const value = field.value.trim();
      if (value) answers[field.dataset.question] = value;
    }
    if (!Object.keys(answers).length) { notify("Isi minimal satu jawaban.", true); return; }
    action(async () => {
      await api("/projects/" + current + "/answers", { method: "POST",
        body: JSON.stringify({ revision: projectData.revision, answers }) });
      notify("Jawaban tersimpan. Plan sudah diperbarui.");
    });
  };
  box.append(form);
}

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
    if (!health.pi_installed) missing.push("Pi installation");
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
// Inline editing of one acceptance criterion.
//
// The purpose is narrow: a feature does not match what the user wants, so change the sentence
// and rebuild. Adding or removing a task is deliberately not offered — that is planning work,
// and the answer is to regenerate the plan.
//
// The whole plan is sent on save rather than a patch. `PUT /api/projects/<id>/plan` already
// accepts a complete plan and rejects a stale revision, and patching server-side would mean a
// second write path with its own rules to keep in sync.
function deepCopyPlan(plan) {
  // A shallow spread would share the nested `tasks` and `acceptance` arrays, so mutating the
  // copy would silently mutate `projectData.plan` too and a failed save would leave the screen
  // showing a change that was never stored.
  return JSON.parse(JSON.stringify(plan));
}

function editableCriteria(plan, taskIndex, active) {
  const list = el("ul", undefined, "criteria");
  const task = plan.tasks[taskIndex];
  for (const [index, text] of task.acceptance.entries()) {
    const row = el("li");
    if (active) {
      row.append(el("span", text));
      list.append(row);
      continue;
    }
    const button = el("button", text, "criterion");
    button.type = "button";
    button.title = "Klik untuk mengubah";
    button.onclick = () => startEdit(row, plan, taskIndex, index, text);
    row.append(button);
    list.append(row);
  }
  return list;
}

function resetCriterion(row, plan, taskIndex, index, text) {
  row.replaceChildren();
  const button = el("button", text, "criterion");
  button.type = "button";
  button.title = "Klik untuk mengubah";
  button.onclick = () => startEdit(row, plan, taskIndex, index, text);
  row.append(button);
}

function startEdit(row, plan, taskIndex, index, text) {
  row.replaceChildren();
  const editor = el("textarea");
  editor.value = text;
  editor.rows = 2;
  editor.className = "criterion-editor";
  const save = el("button", "Simpan", "primary");
  save.type = "button";
  const cancel = el("button", "Batal", "quiet");
  cancel.type = "button";
  // Re-render through the same path as the first click. Attaching the handler by hand left the
  // row dead after one cancel, because it ran once and was never reattached.
  cancel.onclick = () => { resetCriterion(row, plan, taskIndex, index, text); };
  save.onclick = () => {
    const value = editor.value.trim();
    if (!value) { notify("Kriteria tidak boleh kosong.", true); return; }
    if (value === text) { cancel.onclick(); return; }
    save.disabled = true;
    action(async () => {
      const copy = deepCopyPlan(plan);
      copy.tasks[taskIndex].acceptance[index] = value;
      // The answer to "did anyone notice the tests no longer match" is a line of text, not a
      // mechanism. Enforcing it would mean task-to-flow linkage, which restricts the planner.
      const instructionsChanged = copy.tasks[taskIndex].instructions !== plan.tasks[taskIndex].instructions;
      editNotice = instructionsChanged
        ? "Instruksi task berubah, tetapi skenario uji tidak. Periksa apakah tester masih menguji hal yang benar."
        : "";
      await api("/projects/" + current + "/plan", { method: "PUT",
        body: JSON.stringify({ revision: projectData.revision, plan: copy }) });
      editNotice = "";
      notify("Kriteria tersimpan sebagai revisi baru.");
    });
  };
  const buttons = el("div", undefined, "criterion-actions");
  buttons.append(save, cancel);
  row.append(editor, buttons);
  editor.focus();
}

function renderProject(project, events) {
  projectData = project;
  renderWaveView(project, events);
  const active = project.jobs.some(job => ["queued", "running"].includes(job.state));
  $("project-name").textContent = project.name; $("project-brief").textContent = project.brief;
  $("project-profile").textContent = project.profile.toUpperCase() + " / REVISION " + project.revision;
  $("project-state").replaceWith(Object.assign(badge(project.state), { id: "project-state" }));
  const plan = parsed(project.plan), artifact = parsed(project.artifact);
  $("generate").disabled = active || !status.model_configured || !status.pi_installed;
  $("approve").disabled = active || !plan || plan.questions.length > 0 || !["awaiting_plan", "failed", "interrupted", "cancelled"].includes(project.state);
  $("plan-summary").textContent = plan ? plan.summary : "Ask the planner to turn this brief into an implementation plan, or save a reviewed plan below.";
  $("plan-questions").replaceChildren();
  renderQuestions(plan, active);
  if (plan?.assumptions.length) $("plan-questions").append(el("p", "Assumptions: " + plan.assumptions.join(" · "), "muted"));
  $("tasks").replaceChildren();
  if (editNotice) { $("tasks").append(el("div", editNotice, "notice")); }
  for (const [index, task] of (plan?.tasks || []).entries()) {
    const item = el("div", undefined, "task");
    item.append(el("small", task.id), el("strong", task.title));
    // Instructions were never displayed, so a wrong sentence could only be found by scanning
    // the JSON textarea. Collapsed by default: readable when wanted, not a wall of text.
    if (task.instructions) {
      const details = el("details", undefined, "instructions");
      details.append(el("summary", "Instruksi"));
      details.append(el("pre", task.instructions));
      item.append(details);
    }
    item.append(editableCriteria(plan, index, active));
    $("tasks").append(item);
  }
  for (const flow of plan?.test_flows || []) {
    const item = el("div", undefined, "task");
    item.append(el("small", "TESTER FLOW · " + flow.kind.toUpperCase()), el("strong", flow.goal));
    const steps = el("ol"); flow.steps.forEach(text => steps.append(el("li", text))); item.append(steps);
    if (Object.keys(flow.selectors || {}).length) {
      const table = el("div", undefined, "selectors");
      for (const [name, selector] of Object.entries(flow.selectors)) {
        const row = el("div", undefined, "selector");
        row.append(el("code", name), el("code", selector));
        table.append(row);
      }
      const details = el("details", undefined, "instructions");
      details.append(el("summary", "Selector (" + Object.keys(flow.selectors).length + ")"));
      details.append(table);
      item.append(details);
    }
    $("tasks").append(item);
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
/* Wave view: what the planner assigned, what ran together, and what each worker did.
 * The point of this panel is the operator's original mental model — a planner handing
 * work to workers — so it leads with waves and worker outcomes, not with prose. */
function renderWaveView(project, events) {
  const tree = $("worker-tree"), graph = workerGraph(project, events);
  const opened = new Set([...tree.querySelectorAll('details[open]')].map(n => n.dataset.task));
  tree.replaceChildren();

  const planner = el("div", undefined, "worker-planner");
  planner.append(el("strong", "Planner · " + project.name), badge(project.state === "planning" ? "running" : project.plan ? "plan_available" : "not_started"));
  tree.append(planner);

  if (!graph.tasks.length) {
    tree.append(el("p", "Workers appear once a plan is approved. A planned worker has not necessarily started.", "muted"));
    return;
  }

  // Execution summary. State concurrency plainly instead of implying it.
  const p = graph.parallelism;
  const summary = el("div", undefined, "exec-summary");
  const stats = [
    ["Waves", p.waves || "—"],
    ["Workers", p.workers || "—"],
    ["Widest wave", p.widest ? p.widest + (p.widest > 1 ? " tasks · parallel" : " task · sequential") : "—"],
    ["Repairs", p.retried || 0],
  ];
  for (const [label, value] of stats) {
    const stat = el("div", undefined, "exec-stat");
    stat.append(el("small", label), el("strong", String(value)));
    summary.append(stat);
  }
  tree.append(summary);

  if (graph.waves.length) {
    const list = el("div", undefined, "wave-list");
    const byId = new Map(graph.tasks.map(t => [t.id, t]));
    graph.waves.forEach((wave, index) => {
      const row = el("div", undefined, "wave-row");
      const head = el("div", undefined, "wave-head");
      head.append(el("span", "Wave " + (index + 1), "wave-index"));
      const names = wave.tasks.map(id => (byId.get(id)?.title || id));
      head.append(el("span", names.join(" + "), "wave-tasks"));
      const merged = wave.merged || [];
      // A wave with no `wave.done` only means the run ended before it closed; it must not
      // claim "running" beside a worker that already reports interrupted or failed.
      const ranJob = graph.historyJob || graph.build;
      const terminalJob = ranJob && ["failed", "cancelled", "interrupted", "succeeded"].includes(ranJob.state);
      const state = wave.retried ? "retried"
        : wave.merged === null ? (terminalJob ? "incomplete" : "running")
        : merged.length === wave.tasks.length ? "merged"
        : merged.length ? "partial" : (terminalJob ? "incomplete" : "pending");
      head.append(badge(state));
      row.append(head);
      if (wave.tasks.length > 1) row.append(el("p", wave.tasks.length + " workers ran concurrently in this wave.", "wave-note"));
      if (wave.retried) row.append(el("p", "A worker was rejected by review; the wave was retried.", "wave-note"));
      list.append(row);
    });
    tree.append(list);
  }

  // Per-worker detail, grouped by wave so the reading order matches execution.
  const order = graph.tasks.slice().sort((a, b) => (a.wave || 999) - (b.wave || 999));
  for (const task of (graph.build?.kind === "verify" && !graph.waves.length ? [] : order)) {
    const node = el("details", undefined, "worker-node");
    node.dataset.task = task.id;
    node.open = opened.has(task.id);
    const title = el("summary");
    const left = el("span");
    left.append(el("strong", "Worker · " + task.title));
    if (task.wave) left.append(el("span", " wave " + task.wave, "wave-tag"));
    title.append(left, badge(task.state));
    node.append(title);

    const meta = el("div", undefined, "worker-meta");
    if (task.branch) meta.append(el("code", task.branch));
    if (task.attempts) meta.append(el("span", task.attempts > 1 ? task.attempts + " attempts" : "1 attempt"));
    if (task.retried) meta.append(el("span", "repaired after review", "warn"));
    node.append(meta);

    node.append(el("p", task.instructions), el("p", "Dependencies: " + (task.depends_on.join(", ") || "None"), "muted"));
    const criteria = el("ul");
    task.acceptance.forEach(text => criteria.append(el("li", text)));
    node.append(criteria);

    const reviewer = el("div", undefined, "worker-review");
    reviewer.append(el("span", "Independent reviewer"), badge(task.review));
    node.append(reviewer);

    for (const event of task.events) {
      const detail = JSON.parse(event.detail);
      node.append(el("p", event.kind + (detail.attempt ? " · attempt " + detail.attempt : ""), "muted"));
      if (detail.findings?.length) node.append(el("p", detail.findings.join(" · ")));
    }
    if (task.job) {
      const button = el("button", "Changes & review evidence", "quiet");
      button.onclick = () => showEvidence(() => api("/jobs/" + task.job + "/evidence"));
      node.append(button);
    }
    tree.append(node);
  }

  const tester = el("div", undefined, "worker-planner");
  tester.append(el("strong", "Tester agent · API & browser"), badge(graph.tester));
  if (graph.build) {
    const button = el("button", "Test report & screenshots", "quiet");
    button.onclick = () => showTesting(graph.build.id);
    tester.append(button);
  }
  tree.append(tester);

  const verifier = el("div", undefined, "worker-planner");
  verifier.append(el("strong", "Verifier · configured checks & build"), badge(graph.verification));
  tree.append(verifier);
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
