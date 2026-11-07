/* Pure projection: only the latest build of the current plan revision is shown. */
function workerGraph(project, events) {
  const plan = project.plan ? JSON.parse(project.plan) : null;
  const build = project.jobs.find(j => ['build', 'verify'].includes(j.kind) && JSON.parse(j.payload).revision === project.revision);
  const relevant = events.filter(e => build && e.job_id === build.id);
  const terminal = build && ['failed', 'cancelled', 'interrupted'].includes(build.state);
  const latest = (task, kinds) => relevant.filter(e => kinds.includes(e.kind) && (!task || JSON.parse(e.detail).task === task)).at(-1);
  const resolve = (state) => terminal && ['running', 'repairing'].includes(state) ? build.state : terminal && state === 'waiting' ? 'blocked' : state;
  const tasks = (plan?.tasks || []).map(task => {
    const event = latest(task.id, ['task.running', 'task.implementing', 'task.reviewing', 'task.reviewed', 'task.accepted']);
    let state = 'waiting', review = 'waiting';
    if (event) {
      const detail = JSON.parse(event.detail);
      if (event.kind === 'task.accepted') { state = 'accepted'; review = 'approved'; }
      else if (event.kind === 'task.reviewing') { state = 'implemented'; review = 'running'; }
      else if (event.kind === 'task.reviewed') { state = detail.approved ? 'implemented' : 'repairing'; review = detail.approved ? 'approved' : 'changes_requested'; }
      else { state = 'running'; }
    }
    return { ...task, state: resolve(state), review: resolve(review), job: build?.id,
      events: relevant.filter(e => JSON.parse(e.detail).task === task.id) };
  });
  const verification = latest(null, ['verification.running', 'verification.passed']);
  const testEvent = relevant.filter(e => e.kind.startsWith('tester.')).at(-1);
  const phase = testEvent?.kind.split('.')[1];
  const tester = testEvent ? terminal && ['preparing', 'waiting_app', 'running'].includes(phase) ? build.state : terminal && phase === 'prepared' ? 'blocked' : phase : plan?.test_flows?.length ? 'waiting' : 'not_configured';
  return { tasks, build, tester, verification: resolve(verification ? verification.kind === 'verification.passed' ? 'passed' : 'running' : 'waiting') };
}
if (typeof module !== 'undefined') module.exports = { workerGraph };
