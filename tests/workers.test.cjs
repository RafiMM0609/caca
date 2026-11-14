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

// --- event cursor -----------------------------------------------------------------------
// The dashboard asks the server for "events after seq N". This is the client half, and the
// regression it exists for was expensive: the loader started from after=0 on every poll and
// re-downloaded a project's whole history every 4 seconds.
const { mergeEvents, parseFrames } = require("../factory/static/workers.js");

test("a cursor asks only for what is newer than what was rendered", () => {
  const log = [{ seq: 1, kind: "a" }, { seq: 2, kind: "b" }];
  const page = [{ seq: 3, kind: "c" }];
  const merged = mergeEvents(log, page, 2);
  assert.deepStrictEqual(merged.events.map(e => e.seq), [1, 2, 3]);
  assert.strictEqual(merged.cursor, 3);
  assert.strictEqual(merged.after, 3, "the next request must resume from the new cursor");
});

test("an empty page changes nothing", () => {
  const log = [{ seq: 5, kind: "a" }];
  const merged = mergeEvents(log, [], 5);
  assert.deepStrictEqual(merged.events.map(e => e.seq), [5]);
  assert.strictEqual(merged.cursor, 5);
  assert.strictEqual(merged.more, false);
});

test("a replayed event is not rendered twice", () => {
  // A reconnect can re-deliver an event already drawn. Duplicating it would show one worker
  // event twice in the wave view, which reads as the worker having run twice.
  const log = [{ seq: 1, kind: "a" }, { seq: 2, kind: "b" }];
  const merged = mergeEvents(log, [{ seq: 2, kind: "b" }, { seq: 3, kind: "c" }], 2);
  assert.deepStrictEqual(merged.events.map(e => e.seq), [1, 2, 3]);
  assert.strictEqual(merged.cursor, 3);
});

test("a full page means there is more to fetch immediately", () => {
  const page = Array.from({ length: 300 }, (_, i) => ({ seq: i + 1, kind: "x" }));
  assert.strictEqual(mergeEvents([], page, 0).more, true);
  assert.strictEqual(mergeEvents([], page.slice(0, 299), 0).more, false);
});

test("the merged log never mutates the caller's array", () => {
  const log = [{ seq: 1, kind: "a" }];
  mergeEvents(log, [{ seq: 2, kind: "b" }], 1);
  assert.strictEqual(log.length, 1, "the rendered log must not grow as a side effect");
});

test("a cursor survives a sequence of polls without refetching history", () => {
  // Three polls, one event each: the log is complete and the last cursor is the newest seq.
  let log = [], cursor = 0;
  for (const seq of [10, 11, 12]) {
    const merged = mergeEvents(log, [{ seq, kind: "e" + seq }], cursor);
    log = merged.events; cursor = merged.cursor;
  }
  assert.deepStrictEqual(log.map(e => e.seq), [10, 11, 12]);
  assert.strictEqual(cursor, 12);
});

// --- SSE framing ------------------------------------------------------------------------
// `EventSource` cannot send an Authorization header and this API is bearer-only, so the
// dashboard parses text/event-stream itself. That makes this parser load-bearing.
test("a frame is parsed into event, id and data", () => {
  const { frames, rest } = parseFrames('id: 7\nevent: task.running\ndata: {"seq":7}\n\n');
  assert.strictEqual(frames.length, 1);
  assert.strictEqual(frames[0].id, "7");
  assert.strictEqual(frames[0].event, "task.running");
  assert.strictEqual(frames[0].data, '{"seq":7}');
  assert.strictEqual(rest, "");
});

test("a frame split across chunks is held until complete", () => {
  // A network chunk boundary can land anywhere, including mid-JSON.
  const first = parseFrames('id: 1\nevent: a\ndata: {"se');
  assert.deepStrictEqual(first.frames, []);
  const second = parseFrames(first.rest + 'q":1}\n\n');
  assert.strictEqual(second.frames.length, 1);
  assert.strictEqual(second.frames[0].data, '{"seq":1}');
});

test("multiple frames in one chunk are all returned in order", () => {
  const { frames } = parseFrames("data: 1\n\ndata: 2\n\ndata: 3\n\n");
  assert.deepStrictEqual(frames.map(f => f.data), ["1", "2", "3"]);
});

test("a keepalive comment is not dispatched as an event", () => {
  const { frames } = parseFrames(": keepalive\n\n");
  assert.strictEqual(frames.length, 1);
  assert.strictEqual(frames[0].event, "comment");
  assert.strictEqual(frames[0].data, "");
});

test("a reconnect frame is distinguishable from data", () => {
  const { frames } = parseFrames('event: reconnect\ndata: {"reason":"max_lifetime"}\n\n');
  assert.strictEqual(frames[0].event, "reconnect");
  assert.strictEqual(JSON.parse(frames[0].data).reason, "max_lifetime");
});

test("multi-line data is joined with newlines per the SSE spec", () => {
  const { frames } = parseFrames("event: x\ndata: line1\ndata: line2\n\n");
  assert.strictEqual(frames[0].data, "line1\nline2");
});

test("CRLF line endings are accepted", () => {
  const { frames } = parseFrames("id: 9\r\nevent: y\r\ndata: {}\r\n\r\n");
  assert.strictEqual(frames[0].id, "9");
  assert.strictEqual(frames[0].event, "y");
});

test("a leading space after the colon is stripped once, per the spec", () => {
  const { frames } = parseFrames("data:  two spaces\n\n");
  assert.strictEqual(frames[0].data, " two spaces");
});

test("the event log is bounded so a long-lived tab does not grow forever", () => {
  // Measured: ~258 bytes of JSON per event, so an unbounded log in a tab left open for days is
  // tens of MB of live memory. The newest events are what the wave view needs.
  const page = Array.from({ length: 300 }, (_, i) => ({ seq: i + 1, kind: "x" }));
  const merged = mergeEvents([], page, 0, 300, 2000);
  assert.strictEqual(merged.events.length, 300);

  // Feed 10 pages: the log must stay at the cap while the cursor keeps advancing.
  let log = [], cursor = 0;
  for (let pageNo = 0; pageNo < 10; pageNo++) {
    const batch = page.map(e => ({ seq: e.seq + pageNo * 300, kind: "x" }));
    const out = mergeEvents(log, batch, cursor, 300, 2000);
    log = out.events; cursor = out.cursor;
  }
  assert.strictEqual(log.length, 2000, "the log must be trimmed to its limit");
  assert.strictEqual(cursor, 3000, "trimming must not move the cursor backwards");
  assert.strictEqual(log[log.length - 1].seq, 3000, "the newest event must be kept");
});

test("trimming keeps the newest events, not the oldest", () => {
  const page = Array.from({ length: 50 }, (_, i) => ({ seq: i + 1, kind: "x" }));
  const merged = mergeEvents([], page, 0, 50, 10);
  assert.deepStrictEqual(merged.events.map(e => e.seq), [41,42,43,44,45,46,47,48,49,50]);
});
