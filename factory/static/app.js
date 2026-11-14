"use strict";
const $ = id => document.getElementById(id);
let token = "", current = null, status = null, projectData = null, polling = false, revision = null, editNotice = "";
// Event cursor per project. Without this the dashboard re-fetched the project's entire event
// history every 4 seconds (measured: 63 KB for one project, repeated forever), which dominated
// the poll payload. `seq` is a global autoincrement, so a cursor is all that is needed to ask
// for "what happened since I last looked".
let eventCursor = 0;   // highest seq already rendered
let eventLog = [];     // the events rendered so far, kept so the wave view stays complete
let loadedProject = null;  // which project eventLog/eventCursor belong to
let streamAbort = null, streamActive = false, streamFailures = 0, streamProject = null;
const el = (tag, text, cls) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (cls) node.className = cls; return node; };
const parsed = value => value ? JSON.parse(value) : null;
function notify(text, error = false) { $("message").textContent = text; $("message").className = error ? "error" : ""; $("message").hidden = false; setTimeout(() => $("message").hidden = true, 8000); }
async function api(path, options = {}, override) {
  const response = await fetch("/api" + path, { ...options, headers: { "Authorization": "Bearer " + (override || token), "Content-Type": "application/json" } });
  if (!response.ok) { let body; try { body = await response.json(); } catch { body = {}; } throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || response.statusText)); }
  return response.json();
}
async function action(fn) { try { await fn(); await refresh(); } catch (error) { notify(error.message, true); } }
// States are machine words (`awaiting_plan`, `none`, `changes_requested`). Showing them raw put
// "None" and "Failed" in front of an operator with no indication of whose failure it was, so
// every state the dashboard can render has an Indonesian label. An unmapped state falls back to
// the spaced machine word rather than an empty badge: an ugly label beats a blank one.
const STATE_LABELS = {
  draft: "Draft", planning: "Merencanakan", awaiting_plan: "Menunggu persetujuan",
  building: "Sedang build", ready: "Siap", failed: "Gagal", cancelled: "Dibatalkan",
  interrupted: "Terputus", queued: "Dalam antrean", running: "Berjalan",
  succeeded: "Berhasil", merged: "Sudah di-merge", rejected: "Ditolak",
  awaiting_merge: "Menunggu merge", conflict: "Konflik", blocked: "Terblokir",
  changes_requested: "Perlu perbaikan", approved: "Disetujui", pending: "Menunggu",
  waiting: "Menunggu", implemented: "Terimplementasi", accepted: "Diterima",
  repairing: "Diperbaiki", retried: "Diulang", partial: "Sebagian", incomplete: "Tidak tuntas",
  passed: "Lulus", plan_available: "Rencana tersedia", not_started: "Belum mulai",
  not_configured: "Belum dikonfigurasi", none: "Belum ada",
  preparing: "Menyiapkan", editing: "Menyunting", stopped: "Dihentikan", imported: "Terimpor",
  discarded: "Dibuang", unhealthy: "Bermasalah", restarting: "Menyalakan ulang",
};
function label(state) { return STATE_LABELS[state] || String(state).replaceAll("_", " "); }
function badge(state) { return el("span", label(state), "badge " + state); }
function setView(view) { for (const id of ["login", "workspace", "detail"]) $(id).hidden = id !== view; }
// The token is held for the life of the tab so a reload does not force a re-paste.
//
// `sessionStorage`, not `localStorage`, and deliberately: sessionStorage is cleared when the tab
// closes, is not shared between tabs, and is not sent automatically by the browser, so this does
// not become a cookie-like ambient credential. It is still readable by any script running on
// this origin, which is why the CSP forbids inline and third-party scripts and why there is no
// `innerHTML` anywhere in this file. Do not move it to localStorage: that would leave a bearer
// token for this API on disk, surviving the browser session.
const TOKEN_KEY = "anton.operator.token";
function saveToken(value) { try { sessionStorage.setItem(TOKEN_KEY, value); } catch { /* private mode */ } }
function clearToken() { try { sessionStorage.removeItem(TOKEN_KEY); } catch { /* private mode */ } }
function loadToken() { try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; } }

function disconnect() { token = ""; clearToken(); current = null; projectData = null; revision = null; eventCursor = 0; eventLog = []; loadedProject = null; stopStream(); $("token").value = ""; $("release-token").value = ""; $("connection").textContent = "Belum terhubung"; $("logout").hidden = true; setView("login"); }
$("logout").onclick = disconnect;
$("connect-form").onsubmit = event => { event.preventDefault(); token = $("token").value; action(async () => { status = await api("/status"); saveToken(token); $("token").value = ""; $("connection").textContent = "Factory terhubung"; $("logout").hidden = false; setView("workspace"); }); };
// A stored token is re-validated on load rather than trusted: the server may have rotated it,
// and a stale token should land on the login form instead of a broken workspace.
(async () => {
  const saved = loadToken();
  if (!saved) return;
  token = saved;
  try {
    status = await api("/status");
    $("connection").textContent = "Factory terhubung";
    $("logout").hidden = false;
    setView("workspace");
    await refresh();
  } catch { token = ""; clearToken(); setView("login"); }
})();
$("new-project").onclick = () => { $("profile").replaceChildren(...status.profiles.map(name => { const option = el("option", name); option.value = name; return option; })); $("create-dialog").showModal(); };
$("close-create").onclick = () => $("create-dialog").close();
$("create-form").onsubmit = event => { event.preventDefault(); action(async () => { const project = await api("/projects", { method: "POST", body: JSON.stringify({ name: $("name").value, brief: $("brief").value, profile: $("profile").value }) }); $("create-dialog").close(); $("create-form").reset(); current = project.id; revision = null; stopStream(); streamFailures = 0; setView("detail"); }); };
function overview() { current = null; revision = null; eventCursor = 0; eventLog = []; loadedProject = null; stopStream(); setView(token ? "workspace" : "login"); if (token) action(async () => {}); }
$("back").onclick = overview; $("overview").onclick = overview;

// Keyboard shortcuts. Deliberately few and non-destructive: a dashboard that hijacks typing is
// worse than one without shortcuts, so every binding is skipped while a field has focus.
document.addEventListener("keydown", event => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName) || event.target.isContentEditable;
  if (event.key === "Escape") {
    for (const id of ["create-dialog", "evidence-dialog"]) {
      const dialog = $(id);
      if (dialog?.open) { dialog.close(); event.preventDefault(); return; }
    }
    if (!typing && current && !event.metaKey && !event.ctrlKey) { overview(); event.preventDefault(); }
    return;
  }
  if (typing || event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === "n" && token && !current) { $("new-project").click(); event.preventDefault(); }
});
// Planner questions are answered in a form rather than by editing plan JSON: the operator is
// not technical, and an answer has to reach the assumption record, every task instruction and
// every test flow. Hand-editing made that the operator's job to remember, and forgetting the
// test flows once failed a build whose application was correct.
function renderQuestions(plan, active) {
  const box = $("plan-questions");
  if (!plan?.questions?.length) { box.replaceChildren(); return; }
  // Drafts are read *before* clearing, because clear-then-read makes the collection always
  // empty and every keystroke dies on the next 4s poll.
  const draft = {};
  box.querySelectorAll("textarea[data-question]").forEach(saved => {
    const value = saved.value.trim();
    if (value) draft[saved.dataset.question] = value;
  });
  box.replaceChildren();
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
    if (draft[question]) { input.value = draft[question]; }
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
    $("build-status").textContent = health.workers.build_heartbeat ? "Aktif" : "Mati";
    $("release-status").textContent = Object.values(health.profile_features || {}).some(p => p.tester) ? "Sesuai permintaan" : "Belum dikonfigurasi";
    $("memory-status").textContent = health.memorix_enabled ? "Memorix" : "Nonaktif";
    const missing = [];
    if (!health.model_configured) missing.push("model configuration");
    if (!health.pi_installed) missing.push("Pi installation");
    if (!health.workers.build_heartbeat) missing.push("build worker");
    if (!health.profiles.length) missing.push("verification profile");
    $("readiness").hidden = !missing.length;
    $("readiness").textContent = "Perlu disiapkan: " + missing.join(", ") + ". Lihat panduan setup Linux.";
    const list = $("project-list"); list.replaceChildren();
    for (const project of projects) {
      const card = el("button", undefined, "card project-card"), meta = el("div", undefined, "project-meta");
      meta.append(el("span", project.profile.toUpperCase()), badge(project.state));
      card.append(meta, el("h2", project.name), el("p", project.brief.slice(0, 170)), el("div", "Revision " + project.revision + " · Open project →", "project-meta"));
      card.onclick = () => { current = project.id; revision = null; eventCursor = 0; eventLog = []; loadedProject = null; stopStream(); streamFailures = 0; setView("detail"); action(async () => {}); };
      list.append(card);
    }
    if (!projects.length) list.append(el("div", "Your first project starts here. Add a brief to begin.", "empty"));
    if (current) {
      const projectId = current;
      const [project, events] = await Promise.all([api("/projects/" + projectId), loadEvents(projectId)]);
      if (current === projectId) {
        renderProject(project, events); await refreshStudio(project);
        // Started after the history is rendered so the stream resumes from the cursor the
        // history already reached, instead of re-sending events that are on screen.
        if (!streamActive && streamProject !== projectId) { streamFailures = 0; startStream(projectId); }
      }
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
      await api("/projects/" + current + "/plan", { method: "PUT",
        body: JSON.stringify({ revision: projectData.revision, plan: copy }) });
      // Editing a criterion never edits a test flow, so every save widens the gap between what
      // the worker builds and what the tester checks. Saying so is the honest thing to do.
      // Enforcing it would mean task-to-flow linkage, which restricts the planner.
      editNotice = "Kriteria tersimpan. Skenario uji tidak ikut berubah — pastikan tester masih "
                 + "menguji perilaku ini, atau sesuaikan skenario uji lewat Edit plan JSON.";
      notify("Kriteria tersimpan sebagai revisi baru.");
    });
  };
  const buttons = el("div", undefined, "criterion-actions");
  buttons.append(save, cancel);
  row.append(editor, buttons);
  editor.focus();
}

// A served app is the verified build output running on a local port. The port is the thing the
// operator needs, because a Cloudflare hostname has to be pointed at it; the factory does not
// touch the tunnel.
function renderServed(project, artifact, active) {
  const served = project.served;
  const info = $("serve-info");
  info.replaceChildren();
  info.className = "muted";
  const running = served && ["running", "restarting"].includes(served.state);
  $("serve").hidden = running;
  $("serve-stop").hidden = !running;
  $("serve").disabled = !artifact || active;
  $("serve-stop").disabled = active;
  if (!artifact) {
    info.textContent = "Belum ada artifact yang lolos verifikasi untuk dijalankan.";
    return;
  }
  if (!served) {
    info.textContent = "Jalankan untuk menyalakan hasil build di port lokal.";
    return;
  }
  if (served.state === "unhealthy") {
    info.className = "notice";
    info.textContent = "Aplikasi berhenti dan gagal dinyalakan ulang. Periksa evidence, lalu jalankan lagi.";
    return;
  }
  const line = document.createElement("div");
  line.append(
    document.createTextNode("Port " + served.port + " · " + served.state),
    document.createElement("br"),
    document.createTextNode("URL lokal: http://127.0.0.1:" + served.port),
  );
  if (served.restarts) line.append(document.createElement("br"),
    document.createTextNode("Pernah dinyalakan ulang otomatis: " + served.restarts + "x"));
  info.append(line);
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
  renderQuestions(plan, active);
  if (plan?.assumptions.length) $("plan-questions").append(el("p", "Assumptions: " + plan.assumptions.join(" · "), "muted"));
  $("tasks").replaceChildren();
  if (editNotice) {
    const notice = el("div", undefined, "notice");
    notice.append(el("span", editNotice));
    const dismiss = el("button", "Tutup", "quiet");
    dismiss.type = "button";
    dismiss.onclick = () => { editNotice = ""; dismiss.parentElement.remove(); };
    notice.append(dismiss);
    $("tasks").append(notice);
  }
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
  if (artifact) { $("artifact-info").append(el("span", `${artifact.files} files · ${Math.round(artifact.bytes / 1024)} KB · revision ${artifact.revision}`), el("code", artifact.digest)); } else $("artifact-info").textContent = "Tersedia setelah tinjauan dan verifikasi lulus.";
  for (const id of ["download", "source"]) $(id).disabled = !artifact;
  renderServed(project, artifact, active);
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
$("serve").onclick = () => action(async () => {
  await api("/projects/" + current + "/serve", { method: "POST" });
  notify("Aplikasi sedang dinyalakan. Port muncul setelah siap.");
});
$("serve-stop").onclick = () => action(async () => {
  await api("/projects/" + current + "/stop", { method: "POST" });
  notify("Permintaan berhenti dikirim.");
});
$("staging").onclick = () => release("staging"); $("production").onclick = () => release("production");
function release(environment) { action(async () => { const releaseToken = $("release-token").value; if (environment === "production" && !releaseToken) throw new Error("Masukkan token rilis produksi yang terpisah."); await api("/projects/" + current + "/deploy", { method: "POST", body: JSON.stringify({ environment, digest: parsed(projectData.artifact).digest }) }, environment === "production" ? releaseToken : undefined); $("release-token").value = ""; notify("Rilis " + environment + " diantrekan untuk artifact yang sudah terverifikasi."); }); }
async function showEvidence(load) { await action(async () => { $("evidence").textContent = JSON.stringify(await load(), null, 2); $("evidence-dialog").showModal(); }); }
$("source").onclick = () => showEvidence(() => api("/projects/" + current + "/source"));
$("close-evidence").onclick = () => $("evidence-dialog").close();
$("download").onclick = () => action(async () => { const response = await fetch("/api/projects/" + current + "/artifact", { headers: { Authorization: "Bearer " + token } }); if (!response.ok) throw new Error("Unduhan artifact gagal"); const url = URL.createObjectURL(await response.blob()), link = el("a"); link.href = url; link.download = "artifact-" + parsed(projectData.artifact).digest.slice(0, 12) + ".tar.gz"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); });
// The stream carries events; this timer carries everything else (job state, artifact, studio).
// It backs off while a stream is live because the events are no longer the reason to poll, and
// stops entirely while the tab is hidden: a background tab was doing 15 full refreshes a minute.
let pollDelay = 4000, pollTimer = null;
function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    if (token && !document.hidden) {
      try { await refresh(); pollDelay = 4000; }
      catch (error) { notify(error.message, true); pollDelay = Math.min(pollDelay * 2, 30000); }
    }
    schedulePoll();
  }, streamActive ? 8000 : pollDelay);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) { pollDelay = 4000; refresh().catch(error => notify(error.message, true)); } });
schedulePoll();

/* Live events over server-sent events.
 *
 * `EventSource` cannot send an Authorization header, and this API is bearer-only with no cookie
 * auth by design, so the stream is read with fetch()/ReadableStream where the header survives.
 * It is resumable: every reconnect asks for `after=<eventCursor>`, so an interruption costs one
 * frame rather than the whole history. After three failed attempts it falls back to the cursor
 * polling that `loadEvents` already does, which is correct, just slower — so a broken stream
 * degrades the experience instead of breaking the dashboard.
 */

function stopStream() {
  if (streamAbort) { streamAbort.abort(); streamAbort = null; }
  streamActive = false; streamProject = null;
}

async function startStream(project) {
  if (streamProject === project) return;
  stopStream();
  streamProject = project;
  while (token && current === project && streamFailures < 3) {
    const controller = new AbortController();
    streamAbort = controller;
    try {
      const response = await fetch("/api/projects/" + project + "/stream?after=" + eventCursor, {
        headers: { Authorization: "Bearer " + token }, signal: controller.signal,
      });
      if (!response.ok || !response.body) throw new Error("stream tidak tersedia");
      streamActive = true;
      streamFailures = 0;
      const reader = response.body.getReader(), decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const { frames, rest } = parseFrames(buffer);
        buffer = rest;
        let changed = false;
        for (const frame of frames) {
          if (frame.event === "comment" || frame.event === "reconnect") continue;
          let payload; try { payload = JSON.parse(frame.data); } catch { continue; }
          if (!payload || typeof payload.seq !== "number") continue;
          const merged = mergeEvents(eventLog, [payload], eventCursor);
          if (merged.cursor !== eventCursor) { eventLog = merged.events; eventCursor = merged.cursor; changed = true; }
        }
        if (changed) refresh().catch(() => {});
      }
      streamActive = false;
    } catch (error) {
      streamActive = false;
      if (controller.signal.aborted) return;
      streamFailures += 1;
      await new Promise(resolve => setTimeout(resolve, 500 * streamFailures));
    }
  }
  // Falling back is silent by design: polling is still correct, just less immediate.
  streamActive = false;
}

// Fetch only what arrived since the last render.
//
// This used to start from `after=0` on every poll, so a project with 226 events re-downloaded
// all 63 KB of them every 4 seconds. The cursor is the highest `seq` already rendered, which is
// why `eventLog` is kept: the wave view needs the whole history, but the network only needs the
// delta. A project switch resets both, because another project's seq are meaningless here.
async function loadEvents(project) {
  if (project !== loadedProject) { loadedProject = project; eventCursor = 0; eventLog = []; }
  while (true) {
    const page = await api("/projects/" + project + "/events?after=" + eventCursor);
    const merged = mergeEvents(eventLog, page, eventCursor);
    eventLog = merged.events;
    eventCursor = merged.cursor;
    if (!merged.more) break;
  }
  return eventLog;
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
  $("studio-state").textContent = label(state.state);
  const active = project.jobs.some(j => ['queued', 'running'].includes(j.state));
  $("studio-starter").hidden = project.state !== 'draft';
  $("studio-open").disabled = active || project.state !== 'ready' || ['editing', 'preparing'].includes(state.state);
  $("studio-starter").disabled = active;
  $("studio-review").disabled = !['editing', 'stopped', 'failed'].includes(state.state);
  $("studio-apply").disabled = active || reviewedDraft !== state.id || !['stopped', 'failed'].includes(state.state);
  $("studio-stop").disabled = !['editing', 'preparing'].includes(state.state);
  $("studio-discard").disabled = ['none', 'imported', 'discarded'].includes(state.state);
  // One sentence per state, so the operator is told what to do next rather than what went wrong.
  $("studio-help").textContent = {
    preparing: 'Menyiapkan editor lokal dan dependensinya. Ini bisa memakan beberapa menit.',
    failed: 'Persiapan pratinjau gagal. Draft Anda tetap disimpan; periksa runtime Node lokal dan dependensinya, lalu buka kembali.',
    editing: 'Pakai tombol Studio di dalam pratinjau. Simpan suntingan di sana, lalu pilih Tinjau perubahan di sini.',
    imported: 'Suntingan tersimpan sebagai revisi baru. Verifikasi harus lulus sebelum ini menjadi build terverifikasi terbaru.',
  }[state.state] || 'Buka proyek Nuxt Studio yang sudah terverifikasi, atau siapkan starter bawaan untuk proyek baru.';
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
$("studio-discard").onclick = () => action(async () => { if (!confirm("Buang draft ini? Build yang sudah terverifikasi tetap disimpan.")) return; reviewedDraft = null; await studioAction('discard'); });
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
