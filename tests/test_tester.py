import json
import shutil
import threading

import pytest

from factory.engine import Engine
from factory.models import AcceptanceFlow, Profile, Review
from factory.testing import TestAction as Action, TestSuite as Suite, run_suite
from factory.local_app import LocalApp


FLOWS = [
    AcceptanceFlow(id='health', kind='api', goal='Validate the JSON health endpoint.', steps=['GET /api/health returns {ok:true}']),
    AcceptanceFlow(id='visitor', kind='browser', goal='Visitor fills form and sees confirmation.', steps=['Visit /, fill #name, click button, expect #result text'])
]


def suite(broken=False):
    return Suite(summary='Independent API and browser flow assertions.', cases=[
        {'flow_id':'health','name':'HTTP health contract','kind':'api','actions':[
            {'action':'request','path':'/api/health','json_equals':{'ok':True}}]},
        {'flow_id':'visitor','name':'Visitor form journey','kind':'browser','actions':[
            {'action':'goto'}, {'action':'fill','selector':'#name','value':'Ada'},
            {'action':'click','selector':'button'},
            {'action':'expect_text','selector':'#result','value':'WRONG' if broken else 'Hello Ada'}]}
    ])


SERVER = '''const http=require('node:http');http.createServer((req,res)=>{
if(req.url==='/api/health'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({ok:true}));return;}
res.setHeader('Content-Type','text/html');res.end(`<title>Test app</title><label>Name<input id="name"></label><button onclick="document.querySelector('#result').textContent='Hello '+document.querySelector('#name').value">Submit</button><p id="result"></p>`);
}).listen(Number(process.env.PORT),'127.0.0.1');'''


class ParallelAgent:
    def __init__(self):
        self.preparing = threading.Event()
        self.implementing = threading.Event()

    def generate(self, role, instruction, context, schema, cancelled):
        if role == 'tester':
            self.preparing.set()
            assert self.implementing.wait(5), 'Tester must overlap implementation'
            assert 'diff' not in context  # Independent planner contract, not implementation.
            return suite()
        assert role == 'review', role
        return Review(approved=True, findings=[])

    def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False):
        """Harness double: writes the local application into its worktree."""
        assert self.preparing.wait(5), 'Worker must overlap tester preparation'
        self.implementing.set()
        for name, content in {
            'server.cjs': SERVER,
            'build.cjs': "const fs=require('node:fs');fs.mkdirSync('dist');fs.writeFileSync('dist/index.html','<title>Built</title>');",
        }.items():
            (worktree / name).write_text(content)
        return 'scripted harness worker' 


def browser_env(monkeypatch):
    executable = shutil.which('chromium') or shutil.which('google-chrome')
    if executable:
        monkeypatch.setenv('FACTORY_CHROMIUM', executable)
    monkeypatch.setenv('FACTORY_ALLOW_NATIVE', '1')


def test_parallel_preparation_then_real_api_browser(settings, store, project, plan, monkeypatch):
    browser_env(monkeypatch)
    plan.test_flows = FLOWS
    settings.profiles['static'] = Profile(runner='native', commands=[['node','build.cjs']], tester=True,
                                          serve_argv=['node','server.cjs'], readiness_path='/api/health')
    with store.tx() as db:
        db.execute('UPDATE projects SET plan=? WHERE id=?',(plan.model_dump_json(),project['id']))
    store.enqueue(project['id'], 'build', {'revision':1}, next_state='building')
    job=store.claim()
    agent = ParallelAgent()
    Engine(settings, store, agent=agent, worker=agent).process(job)
    assert store.project(project['id'])['state']=='ready'
    evidence=settings.data/'jobs'/job['id']/'testing'
    report=json.loads((evidence/'report.json').read_text())
    assert report['status']=='passed'
    assert len(report['cases'])==2
    assert (evidence/report['cases'][1]['screenshot']).is_file()
    events=store.all('SELECT * FROM events WHERE job_id=? ORDER BY seq',(job['id'],))
    kinds=[event['kind'] for event in events]
    assert kinds.index('tester.prepared') < kinds.index('tester.running') < kinds.index('verification.passed')


def test_real_browser_failure_keeps_evidence(tmp_path, monkeypatch):
    browser_env(monkeypatch)
    (tmp_path/'server.cjs').write_text(SERVER)
    app=LocalApp(['node','server.cjs'],tmp_path)
    try:
        app.wait_ready('/api/health')
        with pytest.raises(AssertionError):
            run_suite(suite(broken=True),app.url,tmp_path/'testing')
        report=json.loads((tmp_path/'testing'/'report.json').read_text())
        assert report['status']=='failed'
        assert report['cases'][1]['status']=='failed'
        assert (tmp_path/'testing'/'case-2.png').is_file()
    finally:
        app.close()
    assert app.process.poll() is not None


def test_suite_requires_complete_flow_coverage():
    complete=suite()
    complete.validate_coverage(FLOWS)
    complete.cases.pop()
    with pytest.raises(ValueError,match='every planner flow'):
        complete.validate_coverage(FLOWS)


@pytest.mark.parametrize('path',['https://example.com','//localhost:8080','/\\localhost','/\nfoo'])
def test_tester_cannot_choose_external_targets(path):
    with pytest.raises(ValueError):
        Action(action='goto',path=path)
