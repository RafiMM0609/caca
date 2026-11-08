/* Pure projection of one project's build into something the operator can read.
 *
 * Two execution models exist in the event log and both must stay legible:
 *   1. The parallel model (current): the planner emits a DAG, `wave.start` /
 *      `wave.done` mark each dependency wave, and `worker.*` events plus the
 *      `workers` table record what each worker actually did.
 *   2. The earlier sequential model: `task.*` events for one implementer at a time.
 * A project built before workers became parallel has only the second kind of event,
 * so ignoring it would make every old job look like it never ran.
 *
 * The function is pure: it takes the project row and its events and returns data, so
 * it can be unit tested under `node --test` without a DOM.
 */
function workerGraph(project, events) {
  const plan = project.plan ? JSON.parse(project.plan) : null;
  const jobs = project.jobs || [];
  const revision = project.revision;
  const inRevision = job => {
    try {
      return JSON.parse(job.payload || "{}").revision === revision;
    } catch {
      return false;
    }
  };
  // Pick the most recent build/verify job for this revision. Selection must not depend
  // on the order the caller passes jobs in: a `verify` job runs the gates again without
  // creating workers, so choosing an older failed attempt would report a stale verdict
  // for a project that has since passed.
  const ordered = [...jobs].sort((a, b) => (Number(b.created) || 0) - (Number(a.created) || 0));
  const build = ordered.find(j => ["build", "verify"].includes(j.kind) && inRevision(j));
  // Workers are created by the job that implemented the plan. A later verify job has no
  // worker rows of its own, so fall back to the newest job that actually has some, and
  // only for the same revision.
  const workerJob = build
    ? (project.workers || []).some(row => row.job_id === build.id)
      ? build
      : ordered.find(j => inRevision(j) && (project.workers || []).some(row => row.job_id === j.id))
    : null;
  const relevant = events.filter(e => build && e.job_id === build.id);
  // Execution history (waves and per-task activity) belongs to the job that ran the
  // workers, which may be an earlier job than the newest verify. The selected `build`
  // still supplies the current verdict, because verification is what decides `ready`.
  const historyJob = workerJob || build;
  const history = events.filter(e => historyJob && e.job_id === historyJob.id);
  const terminal = build && ["failed", "cancelled", "interrupted"].includes(build.state);
  const detailOf = event => {
    try {
      return JSON.parse(event.detail);
    } catch {
      return {};
    }
  };
  const latest = (task, kinds) =>
    relevant.filter(e => kinds.includes(e.kind) && (!task || detailOf(e).task === task)).at(-1);
  const historyLatest = (task, kinds) =>
    history.filter(e => kinds.includes(e.kind) && (!task || detailOf(e).task === task)).at(-1);
  const resolve = state =>
    terminal && ["running", "repairing"].includes(state)
      ? build.state
      : terminal && state === "waiting"
        ? "blocked"
        : state;

  // ---- waves -----------------------------------------------------------------
  // A wave is the set of tasks whose dependencies were already satisfied. More than
  // one task in a wave is the parallelism the operator asked to see; a repeated wave
  // is a repair, not a stall.
  const waves = [];
  for (const event of history) {
    const detail = detailOf(event);
    if (event.kind === "wave.start") {
      waves.push({ tasks: detail.tasks || [], completed: detail.completed || [], merged: null, retried: false });
    } else if (event.kind === "wave.retry" && waves.length) {
      waves[waves.length - 1].retried = true;
      waves[waves.length - 1].retryTasks = detail.tasks || [];
    } else if (event.kind === "wave.done" && waves.length) {
      waves[waves.length - 1].merged = detail.merged || [];
      waves[waves.length - 1].completed = detail.completed || waves[waves.length - 1].completed;
    }
  }

  // ---- worker rows -----------------------------------------------------------
  // The table is the authority on outcome because the controller writes it, so it is
  // not inferred from event ordering.
  const rows = new Map();
  for (const row of project.workers || []) {
    if (workerJob && row.job_id !== workerJob.id) continue;
    rows.set(row.task_id, row);
  }

  const tasks = (plan?.tasks || []).map(task => {
    const row = rows.get(task.id);
    const event = historyLatest(task.id, [
      "task.running", "task.implementing", "task.reviewing", "task.reviewed", "task.accepted",
      "worker.running", "worker.finished", "worker.merged", "worker.rejected", "worker.failed",
    ]);
    let state = "waiting";
    let review = "waiting";
    if (row) {
      state = row.state;
      if (row.state === "rejected") review = "changes_requested";
      else if (row.state === "merged") review = "approved";
      else if (["failed", "interrupted", "cancelled"].includes(row.state)) review = row.state;
      else if (row.state === "running") review = "pending";
      else if (row.state === "awaiting_merge" || row.state === "conflict") review = "pending";
    } else if (event) {
      const detail = detailOf(event);
      if (event.kind === "task.accepted") { state = "accepted"; review = "approved"; }
      else if (event.kind === "task.reviewing") { state = "implemented"; review = "running"; }
      else if (event.kind === "task.reviewed") {
        state = detail.approved ? "implemented" : "repairing";
        review = detail.approved ? "approved" : "changes_requested";
      } else { state = "running"; }
    }
    const waveIndex = waves.findIndex(w => w.tasks.includes(task.id));
    const wave = waveIndex >= 0 ? waves[waveIndex] : null;
    return {
      ...task,
      state: resolve(state),
      review: resolve(review),
      job: build?.id,
      wave: waveIndex >= 0 ? waveIndex + 1 : null,
      attempts: row?.attempts ?? null,
      branch: row?.branch || "",
      workerState: row?.state || "",
      retried: Boolean(wave?.retried && (wave.retryTasks || []).includes(task.id)),
      events: history.filter(e => detailOf(e).task === task.id),
    };
  });

  // State plainly whether work actually ran concurrently, rather than implying it.
  const waveSizes = waves.map(w => w.tasks.length);
  const parallelism = {
    waves: waves.length,
    widest: waveSizes.length ? Math.max(...waveSizes) : 0,
    ranConcurrently: waveSizes.some(size => size > 1),
    workers: rows.size,
    retried: waves.filter(w => w.retried).length,
  };

  const verification = latest(null, ["verification.running", "verification.passed"]);
  const testEvent = relevant.filter(e => e.kind.startsWith("tester.")).at(-1);
  const phase = testEvent?.kind.split(".")[1];
  const tester = testEvent
    ? terminal && ["preparing", "waiting_app", "running"].includes(phase)
      ? build.state
      : terminal && phase === "prepared"
        ? "blocked"
        : phase
    : plan?.test_flows?.length
      ? "waiting"
      : "not_configured";

  // A terminal job's own state outranks any event it left behind. A run that failed after
  // emitting `verification.passed` (a later gate, a cancelled publish) must never present
  // as verified: showing "passed" beside "failed" is the kind of contradiction that would
  // let an operator trust an artifact that was never published.
  const verdict = verification
    ? verification.kind === "verification.passed" ? "passed" : "running"
    : "waiting";
  const verificationState = terminal ? (verdict === "passed" ? "blocked" : resolve(verdict)) : verdict;

  return {
    tasks,
    waves,
    parallelism,
    build,
    // The job that actually ran the workers. A later verify job has no waves of its own,
    // so wave completion must be judged against this job's state, not the verify's.
    historyJob,
    tester,
    verification: verificationState,
  };
}

if (typeof module !== "undefined") module.exports = { workerGraph };
