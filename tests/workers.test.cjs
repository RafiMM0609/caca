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
