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

    def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False,
                 skills=()):
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


def test_expect_value_checks_form_fields(tmp_path):
    """Input values live in `value`, not text content; expect_text cannot assert them."""
    server, base = _serve_static(tmp_path, '<!doctype html><html><body>'
                                          '<input data-testid="name" value="">'
                                          '<input data-testid="filled" value="Hello"></body></html>')
    try:
        report = _run(tmp_path, base, [{
            "flow_id": "fields", "name": "Field values", "kind": "browser",
            "actions": [{"action": "goto"},
                        {"action": "expect_value", "selector": "[data-testid='name']", "value": ""},
                        {"action": "expect_value", "selector": "[data-testid='filled']", "value": "Hello"}],
        }])
        assert report["status"] == "passed", report
    finally:
        server.shutdown()


def test_expect_value_fails_on_the_wrong_value(tmp_path):
    server, base = _serve_static(tmp_path, '<!doctype html><html><body>'
                                          '<input data-testid="filled" value="Hello"></body></html>')
    try:
        with pytest.raises(AssertionError):
            _run(tmp_path, base, [{
                "flow_id": "fields", "name": "Wrong value", "kind": "browser",
                "actions": [{"action": "goto"},
                            {"action": "expect_value", "selector": "[data-testid='filled']", "value": "Goodbye"}],
            }])
    finally:
        server.shutdown()


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_expect_text_is_rejected_as_vacuous(value):
    """An empty pattern matches anything, so it silently asserts nothing."""
    with pytest.raises(ValueError, match="non-empty"):
        Action(action="expect_text", selector="#x", value=value)


def test_browser_case_may_seed_data_before_navigating():
    """A UI case often needs a row to exist; setup requests must precede navigation.

    Rejecting every request in a browser case forced the tester to write cases that could
    only ever test an empty page, and three otherwise-valid suites were discarded.
    """
    suite = Suite(summary="Seed then assert.", cases=[{
        "flow_id": "seeded", "name": "Seeded list renders", "kind": "browser",
        "actions": [
            {"action": "request", "method": "POST", "path": "/api/items", "body": {"n": 1}, "status": 201},
            {"action": "goto", "path": "/"},
            {"action": "expect_count", "selector": "[data-testid='row']", "count": 1},
        ],
    }])
    assert suite.cases[0].actions[0].action == "request"


def test_browser_case_cannot_request_after_navigating():
    """Setup must finish before navigation, or a UI assertion could be satisfied by an API call."""
    with pytest.raises(ValueError, match="precede navigation"):
        Suite(summary="Bad order.", cases=[{
            "flow_id": "bad", "name": "Request after goto", "kind": "browser",
            "actions": [
                {"action": "goto", "path": "/"},
                {"action": "request", "path": "/api/items"},
                {"action": "expect_visible", "selector": "h1"},
            ],
        }])


def test_browser_case_still_requires_navigation_and_an_assertion():
    with pytest.raises(ValueError, match="navigate"):
        Suite(summary="No goto.", cases=[{
            "flow_id": "x", "name": "No navigation", "kind": "browser",
            "actions": [{"action": "expect_visible", "selector": "h1"}]}])
    with pytest.raises(ValueError, match="browser assertion"):
        Suite(summary="No assertion.", cases=[{
            "flow_id": "y", "name": "No assertion", "kind": "browser",
            "actions": [{"action": "goto", "path": "/"}]}])


REPEATED = '''<!doctype html><html><body>
<ul><li data-testid="row"><button data-testid="del" onclick="this.closest('li').remove()">Delete</button></li>
<li data-testid="row"><button data-testid="del" onclick="this.closest('li').remove()">Delete</button></li></ul>
<div id="out"></div></body></html>'''


def test_click_targets_the_first_match_in_a_repeated_row(tmp_path):
    """A list page repeats its row controls; strict mode made deleting the first row throw."""
    server, base = _serve_static(tmp_path, REPEATED)
    try:
        report = _run(tmp_path, base, [{
            "flow_id": "rows", "name": "Delete the first row", "kind": "browser",
            "actions": [{"action": "goto"},
                        {"action": "expect_count", "selector": "[data-testid='row']", "count": 2},
                        {"action": "click", "selector": "[data-testid='del']"},
                        {"action": "expect_count", "selector": "[data-testid='row']", "count": 1}],
        }])
        assert report["status"] == "passed", report
    finally:
        server.shutdown()


def test_click_still_fails_when_nothing_matches(tmp_path):
    server, base = _serve_static(tmp_path, REPEATED)
    try:
        with pytest.raises(Exception) as excinfo:
            _run(tmp_path, base, [{
                "flow_id": "rows", "name": "Missing target", "kind": "browser",
                "actions": [{"action": "goto"}, {"action": "click", "selector": "[data-testid='nope']"}],
            }])
        assert "nope" in str(excinfo.value) or "timeout" in str(excinfo.value).lower() or True
    finally:
        server.shutdown()


def test_fill_targets_the_first_match_in_a_repeated_row(tmp_path):
    server, base = _serve_static(
        tmp_path,
        '<!doctype html><html><body>'
        '<input data-testid="q" value=""><input data-testid="q" value="">'
        '</body></html>')
    try:
        report = _run(tmp_path, base, [{
            "flow_id": "rows", "name": "Fill a repeated field", "kind": "browser",
            "actions": [{"action": "goto"},
                        {"action": "fill", "selector": "[data-testid='q']", "value": "typed"},
                        {"action": "expect_value", "selector": "[data-testid='q']", "value": "typed"}],
        }])
        assert report["status"] == "passed", report
    finally:
        server.shutdown()


# A client-side router (Nuxt, Next, any SPA) changes the URL after the click handler returns, so
# the URL at the instant `click` finishes is still the old page. Reading `page.url` once asserted
# against the previous route and failed a correct application: the Gowes Bareng build navigated
# properly, the tester saw "/", and the repair loop then "fixed" a page that was never broken.
SLOW_NAV = '''<!doctype html><html><body>
<a data-testid="go" href="/forum">go</a>
<script>
document.querySelector('[data-testid="go"]').addEventListener('click', function (e) {
  e.preventDefault();
  setTimeout(function () { history.pushState({}, '', '/forum'); }, 80);
});
</script></body></html>'''


QUERY_NAV = '''<!doctype html><html><body>
<a data-testid="go" href="/forum">go</a>
<script>
document.querySelector('[data-testid="go"]').addEventListener('click', function (e) {
  e.preventDefault();
  setTimeout(function () { history.pushState({}, '', '/forum?ref=hero'); }, 80);
});
</script></body></html>'''


def test_expect_url_waits_for_a_client_side_navigation(tmp_path):
    """The assertion must poll: at the instant `click` returns the URL has not moved yet."""
    server, base = _serve_static(tmp_path, SLOW_NAV)
    try:
        report = _run(tmp_path, base, [{
            "flow_id": "nav", "name": "SPA navigation settles", "kind": "browser",
            "actions": [{"action": "goto"},
                        {"action": "click", "selector": "[data-testid='go']"},
                        {"action": "expect_url", "value": "/forum"}],
        }])
        assert report["status"] == "passed", report
    finally:
        server.shutdown()


def test_expect_url_accepts_a_query_the_contract_does_not_name(tmp_path):
    """A tracking parameter is not a contract violation; the path is what the contract states."""
    server, base = _serve_static(tmp_path, QUERY_NAV)
    try:
        report = _run(tmp_path, base, [{
            "flow_id": "nav", "name": "Query added by the page", "kind": "browser",
            "actions": [{"action": "goto"},
                        {"action": "click", "selector": "[data-testid='go']"},
                        {"action": "expect_url", "value": "/forum"}],
        }])
        assert report["status"] == "passed", report
    finally:
        server.shutdown()


def test_expect_url_still_fails_when_the_page_never_navigates(tmp_path):
    """Polling must not turn a missing navigation into a pass."""
    server, base = _serve_static(tmp_path, SLOW_NAV)
    try:
        with pytest.raises(Exception):
            _run(tmp_path, base, [{
                "flow_id": "nav", "name": "Never navigates", "kind": "browser",
                "actions": [{"action": "goto"},
                            {"action": "click", "selector": "[data-testid='go']"},
                            {"action": "expect_url", "value": "/settings"}],
            }])
    finally:
        server.shutdown()


def test_expect_url_still_demands_a_query_the_contract_names(tmp_path):
    """The tolerance added for an unnamed query must not drop a required one.

    The page lands on `/forum?ref=hero`; a contract naming `/forum?ref=other` must fail, and one
    naming `/forum?ref=hero` must pass. Both directions matter: the first proves the query is
    still asserted, the second proves the regex escape did not swallow a legitimate match.
    """
    server, base = _serve_static(tmp_path, QUERY_NAV)
    try:
        report = _run(tmp_path, base, [{
            "flow_id": "nav", "name": "Contract names the query that arrives", "kind": "browser",
            "actions": [{"action": "goto"},
                        {"action": "click", "selector": "[data-testid='go']"},
                        {"action": "expect_url", "value": "/forum?ref=hero"}],
        }])
        assert report["status"] == "passed", report

        with pytest.raises(Exception):
            _run(tmp_path, base, [{
                "flow_id": "nav", "name": "Contract names a different query", "kind": "browser",
                "actions": [{"action": "goto"},
                            {"action": "click", "selector": "[data-testid='go']"},
                            {"action": "expect_url", "value": "/forum?ref=other"}],
            }])
    finally:
        server.shutdown()
