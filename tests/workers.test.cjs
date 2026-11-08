const { test } = require('node:test');
const assert = require('node:assert/strict');
const { workerGraph } = require('../factory/static/workers.js');
const project = {revision: 2, plan: JSON.stringify({tasks:[{id:'ui',title:'UI',depends_on:[],acceptance:[]}]}),jobs:[{id:'new',kind:'build',payload:'{"revision":2}',state:'running'}]};
const event = (kind, detail={}, job='new') => ({kind, detail:JSON.stringify({task:'ui',...detail}),job_id:job});
test('old jobs cannot mark new workers complete', () => assert.equal(workerGraph(project,[event('task.accepted',{},'old')]).tasks[0].state,'waiting'));
test('separate reviewer activity', () => { const task=workerGraph(project,[event('task.reviewing')]).tasks[0]; assert.equal(task.state,'implemented'); assert.equal(task.review,'running'); });
test('interrupted reviewer cannot remain running', () => { const p={...project,jobs:[{...project.jobs[0],state:'interrupted'}]}; assert.equal(workerGraph(p,[event('task.reviewing')]).tasks[0].review,'interrupted'); });
test('verification is separate from task approval', () => { const graph=workerGraph(project,[event('task.accepted')]); assert.equal(graph.tasks[0].state,'accepted'); assert.equal(graph.verification,'waiting'); });
test('new revision does not reuse previous build', () => assert.equal(workerGraph({...project,revision:3},[event('task.accepted')]).tasks[0].state,'waiting'));

test('tester preparation is shown independently', () => assert.equal(workerGraph(project,[event('tester.preparing')]).tester,'preparing'));
test('cancelled tester cannot stay waiting for an app', () => assert.equal(workerGraph({...project,jobs:[{...project.jobs[0],state:'cancelled'}]},[event('tester.waiting_app')]).tester,'cancelled'));
test('content-only verification has its own tester state', () => assert.equal(workerGraph({...project,jobs:[{...project.jobs[0],kind:'verify'}]},[event('tester.passed')]).tester,'passed'));

/* ---- parallel model: waves and the workers table ---- */
const parallel = {
  revision: 3,
  plan: JSON.stringify({tasks:[
    {id:'a',title:'A',depends_on:[],acceptance:[]},
    {id:'b',title:'B',depends_on:[],acceptance:[]},
    {id:'c',title:'C',depends_on:['a','b'],acceptance:[]}]}),
  jobs:[{id:'b1',kind:'build',payload:'{"revision":3}',state:'succeeded',created:100},
        {id:'v1',kind:'verify',payload:'{"revision":3}',state:'failed',created:200}],
  workers:[
    {task_id:'a',job_id:'b1',state:'merged',attempts:1,branch:'worker/a',started:1},
    {task_id:'b',job_id:'b1',state:'merged',attempts:2,branch:'worker/b',started:2},
    {task_id:'c',job_id:'b1',state:'running',attempts:1,branch:'worker/c'}],
};
const ev = (kind, detail, job='b1') => ({kind, detail:JSON.stringify(detail), job_id:job});

test('a wave with two tasks is reported as concurrent', () => {
  const events = [
    ev('wave.start',{tasks:['a','b'],completed:[]}),
    ev('wave.done',{merged:['a','b'],completed:['a','b']}),
    ev('wave.start',{tasks:['c'],completed:['a','b']}),
  ];
  const g = workerGraph(parallel, events);
  assert.equal(g.parallelism.ranConcurrently, true);
  assert.equal(g.parallelism.widest, 2);
  assert.equal(g.parallelism.waves, 2);
});

test('a retried wave is reported as a retry, not a stall', () => {
  const events = [
    ev('wave.start',{tasks:['a'],completed:[]}),
    ev('wave.retry',{tasks:['a']}),
    ev('wave.done',{merged:[],completed:[]}),
  ];
  const g = workerGraph(parallel, events);
  assert.equal(g.parallelism.retried, 1);
  assert.equal(g.tasks.find(t=>t.id==='a').retried, true);
});

test('worker outcome comes from the workers table', () => {
  const g = workerGraph(parallel, []);
  assert.equal(g.tasks.find(t=>t.id==='a').state, 'merged');
  assert.equal(g.tasks.find(t=>t.id==='a').review, 'approved');
  assert.equal(g.tasks.find(t=>t.id==='a').attempts, 1);
  assert.equal(g.tasks.find(t=>t.id==='b').attempts, 2, 'a repaired task reports its attempt count');
});

test('the newest job supplies the verdict, not the worker job', () => {
  // b1 succeeded and v1 failed; the verify job ran the gates again, so it decides.
  // Its failure is read from its own events, not from the older build's success.
  const events = [ev('verification.running',{},'v1'), ev('verification.passed',{},'v1')];
  const g = workerGraph(parallel, events);
  assert.equal(g.build.id, 'v1');
  // v1 failed, so a 'passed' event from it is contradictory input and must not be trusted.
  assert.equal(g.verification, 'blocked');
});

test('job order passed in must not change the selection', () => {
  const reversed = {...parallel, jobs:[...parallel.jobs].reverse()};
  assert.equal(workerGraph(reversed, []).build.id, 'v1');
});

test('waves still come from the job that ran the workers', () => {
  const events = [ev('wave.start',{tasks:['a','b'],completed:[]}), ev('wave.done',{merged:['a','b'],completed:['a','b']})];
  const g = workerGraph(parallel, events);
  assert.equal(g.waves.length, 1, 'a verify job creates no waves, so history must come from the build');
  assert.equal(g.tasks.find(t=>t.id==='a').wave, 1);
});

test('an unfinished wave is incomplete, not running, once its job ended', () => {
  const events = [
    ev('wave.start',{tasks:['a'],completed:[]}),
    ev('wave.done',{merged:['a'],completed:['a']}),
    ev('wave.start',{tasks:['c'],completed:['a']}),
  ];
  // b1 is the job that ran the workers; if it ended, wave 2 never closed.
  const ended = {...parallel, jobs:[{...parallel.jobs[0], state:'succeeded'}, parallel.jobs[1]]};
  const g = workerGraph(ended, events);
  assert.equal(g.historyJob.id, 'b1', 'wave completion must be judged against the job that ran it');
});
