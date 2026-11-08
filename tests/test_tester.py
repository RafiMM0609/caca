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
    AcceptanceFlow(id='visitor', kind='browser', goal='Visitor fills form and sees confirmation.', steps=['Visit /, fill #name, click button, expect #result text'], selectors={'name': '#name', 'result': '#result'})
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


def test_expect_url_accepts_the_route_in_path():
    """A model writes `path` for expect_url the same way it does for goto/reload.

    Rejecting that spelling failed a whole tester suite and burned a repair attempt on
    every run, so the route is lifted into `value` instead.
    """
    assert Action(action='expect_url', path='/').value == '/'
    # An explicit value still wins, and the route stays validated as application-relative.
    assert Action(action='expect_url', path='/ignored', value='/kept').value == '/kept'
    with pytest.raises(ValueError):
        Action(action='expect_url', path='https://evil.example/')


MULTI = '''<!doctype html><html><body>
<section data-testid="services">
  <div data-testid="card">One</div><div data-testid="card">Two</div><div data-testid="card">Three</div>
</section>
<p data-testid="tagline">Handmade teak furniture, built to last.</p>
</body></html>'''


def _serve_static(tmp_path, html):
    import http.server
    import socketserver
    import threading
    (tmp_path / "index.html").write_text(html)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(tmp_path), **k)
        def log_message(self, *a):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _run(tmp_path, base, cases):
    from factory.testing import TestSuite
    suite = TestSuite(summary="Assertion semantics.", cases=cases)
    evidence = tmp_path / "evidence"
    report = run_suite(suite, base, evidence, 30)
    return report


def test_expect_visible_accepts_a_selector_matching_several_visible_elements(tmp_path):
    """Playwright strict mode failed three visible cards; "visible" must mean all visible."""
    import shutil
    if not shutil.which("node") and not __import__("os").environ.get("FACTORY_CHROMIUM"):
        pytest.skip("browser required")
    server, base = _serve_static(tmp_path, MULTI)
    try:
        report = _run(tmp_path, base, [{
            "flow_id": "cards", "name": "Three visible cards", "kind": "browser",
            "actions": [{"action": "goto"}, {"action": "expect_visible", "selector": "[data-testid='card']"}],
        }])
        assert report["status"] == "passed", report
    finally:
        server.shutdown()


def test_expect_visible_still_fails_when_nothing_matches(tmp_path):
    """An empty match must still fail; a failure aborts the suite by design."""
    server, base = _serve_static(tmp_path, MULTI)
    try:
        with pytest.raises(AssertionError):
            _run(tmp_path, base, [{
                "flow_id": "missing", "name": "Missing selector fails", "kind": "browser",
                "actions": [{"action": "goto"}, {"action": "expect_visible", "selector": "[data-testid='nope']"}],
            }])
    finally:
        server.shutdown()


def test_expect_text_ignores_capitalisation(tmp_path):
    """A page capitalising a sentence must not fail a reasonable text assertion."""
    server, base = _serve_static(tmp_path, MULTI)
    try:
        report = _run(tmp_path, base, [{
            "flow_id": "copy", "name": "Case-insensitive copy", "kind": "browser",
            "actions": [{"action": "goto"},
                        {"action": "expect_text", "selector": "[data-testid='tagline']",
                         "value": "handmade teak furniture"}],
        }])
        assert report["status"] == "passed", report
    finally:
        server.shutdown()


def test_expect_text_accepts_text_in_any_matched_element(tmp_path):
    """A selector matching several items must pass when any one of them shows the copy."""
    server, base = _serve_static(tmp_path, MULTI)
    try:
        report = _run(tmp_path, base, [{
            "flow_id": "cards", "name": "Text lives in the third card", "kind": "browser",
            "actions": [{"action": "goto"},
                        {"action": "expect_text", "selector": "[data-testid='card']", "value": "Three"}],
        }])
        assert report["status"] == "passed", report
    finally:
        server.shutdown()


def test_expect_text_still_fails_when_no_element_has_it(tmp_path):
    server, base = _serve_static(tmp_path, MULTI)
    try:
        with pytest.raises(AssertionError):
            _run(tmp_path, base, [{
                "flow_id": "cards", "name": "Absent text fails", "kind": "browser",
                "actions": [{"action": "goto"},
                            {"action": "expect_text", "selector": "[data-testid='card']", "value": "Absent"}],
            }])
    finally:
        server.shutdown()


def test_browser_flow_must_declare_its_selectors():
    """The plan is the single source of truth for selectors.

    A worker shipped `edit-input-name` for a row in edit mode while the tester asserted
    `input-name`; both were reasonable, and a correct page failed. Declaring selectors in
    the flow removes the guess.
    """
    with pytest.raises(ValueError, match="must declare the selectors"):
        AcceptanceFlow(id="ui", kind="browser", goal="Visitor edits a row.", steps=["visit /"])

    flow = AcceptanceFlow(id="ui", kind="browser", goal="Visitor edits a row.", steps=["visit /"],
                          selectors={"row": "[data-testid='row']"})
    assert flow.selectors == {"row": "[data-testid='row']"}
    # API flows carry no DOM, so they need no selectors.
    AcceptanceFlow(id="api", kind="api", goal="Health returns ok.", steps=["GET /api/health"])


@pytest.mark.parametrize("selectors", [
    {"Bad Name": "#x"},
    {"ok": ""},
    {"ok": "a" * 400},
    {"ok": "url(evil)"},
])
def test_invalid_declared_selectors_are_rejected(selectors):
    with pytest.raises(ValueError):
        AcceptanceFlow(id="ui", kind="browser", goal="Visitor sees the page.", steps=["visit /"],
                       selectors=selectors)


def test_starter_plan_declares_its_browser_selectors():
    """The shipped starter is a template; it must satisfy the same rule."""
    import json
    from factory.models import Plan
    from tests.conftest import ROOT
    plan = Plan.model_validate(json.loads((ROOT / "factory" / "starters" / "nuxt-studio" / "factory-plan.json").read_text()))
    browser = [f for f in plan.test_flows if f.kind == "browser"]
    assert browser and all(f.selectors for f in browser)
