"""Operator-owned validation gates: strengthened tester contract and post-build checks."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from factory.models import Profile
from factory.testing import TestAction as Action
from factory.testing import ignorable_console

ROOT = Path(__file__).resolve().parent.parent
SCAN = ROOT / "checks" / "secret_scan.py"


# --- TestAction contract ---------------------------------------------------------------

def test_head_requests_are_supported():
    """The brief asked for HEAD /api/health; the schema used to reject it outright."""
    action = Action(action="request", method="HEAD", path="/api/health")
    assert action.method == "HEAD"
    assert action.json_equals is None


def test_head_rejects_body_comparison():
    with pytest.raises(ValueError, match="no body"):
        Action(action="request", method="HEAD", path="/api/health", json_equals={"status": "ok"})


def test_state_loss_and_double_submit_are_declarable():
    """Regression guards for the behaviour most common bugs in form applications."""
    steps = [
        Action(action="goto", path="/"),
        Action(action="fill", selector="#email", value="a@b.test"),
        Action(action="click", selector="button[type=submit]"),
        Action(action="expect_count", selector="#result", count=1),
        Action(action="reload"),
        Action(action="expect_hidden", selector="#error"),
        Action(action="press", value="Enter"),
        Action(action="select", selector="#plan", value="pro"),
        Action(action="expect_url", value="/thank-you"),
    ]
    assert [step.action for step in steps] == [
        "goto", "fill", "click", "expect_count", "reload", "expect_hidden", "press", "select", "expect_url"]


@pytest.mark.parametrize("action", [
    {"action": "expect_url", "value": "https://evil.example/x"},
    {"action": "expect_url", "value": "//evil.example/x"},
    {"action": "press", "value": "rm -rf /"},
    {"action": "expect_count", "selector": "li", "count": -1},
    {"action": "expect_count", "selector": "li"},
    {"action": "expect_hidden"},
    {"action": "select", "value": "pro"},
])
def test_unsafe_or_incomplete_actions_are_rejected(action):
    with pytest.raises(ValueError):
        Action(**action)


def test_expect_count_defaults_are_not_silently_accepted():
    """A browser case must not pass without an assertion."""
    from factory.testing import TestCase
    with pytest.raises(ValueError, match="assertion"):
        TestCase(flow_id="home", name="No assertion here", kind="browser",
                 actions=[{"action": "goto", "path": "/"}, {"action": "reload"}])


def test_console_noise_from_our_network_policy_is_ignored():
    assert ignorable_console("Failed to load resource: net::ERR_BLOCKED_BY_CLIENT")
    assert ignorable_console("GET http://127.0.0.1:9/favicon.ico 404")
    assert not ignorable_console("Hydration completed but contains mismatches.")
    assert not ignorable_console("TypeError: cannot read properties of undefined")


# --- Operator-owned secret scan -------------------------------------------------------

def scan(tmp_path, files, env=None):
    root = tmp_path / "artifact"
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    environ = {"PATH": "/usr/bin:/bin", **(env or {})}
    return subprocess.run([sys.executable, str(SCAN), str(root)],
                          capture_output=True, text=True, env=environ)


def test_secret_scan_passes_a_clean_artifact(tmp_path):
    result = scan(tmp_path, {"index.html": "<h1>Hello</h1>", "_nuxt/app.js": "console.log('ok')"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


@pytest.mark.parametrize("payload,label", [
    ('const k="sk-abcdefghijklmnopqrstuvwxyz123456"', "opencode_key"),
    ('token="ghp_abcdefghijklmnopqrstuvwxyz0123456789"', "github_token"),
    ('key="AKIAIOSFODNN7EXAMPLE"', "aws_access_key"),
    ("-----BEGIN PRIVATE KEY-----", "private_key"),
])
def test_secret_scan_blocks_credential_shapes(tmp_path, payload, label):
    result = scan(tmp_path, {"_nuxt/entry.js": payload})
    assert result.returncode == 1
    assert label in result.stdout


def test_secret_scan_blocks_configured_secret_values(tmp_path):
    leaked = "s3cret-value-from-the-environment"
    result = scan(tmp_path, {"app.js": f'var t="{leaked}"'}, {"OPENCODE_API_KEY": leaked})
    assert result.returncode == 1
    assert "OPENCODE_API_KEY" in result.stdout


def test_secret_scan_skips_binary_assets(tmp_path):
    result = scan(tmp_path, {"logo.png": "sk-abcdefghijklmnopqrstuvwxyz123456"})
    assert result.returncode == 0


# --- Profile wiring -------------------------------------------------------------------

def test_native_checks_argv_requires_checks_dir():
    with pytest.raises(ValueError, match="checks_dir"):
        Profile(runner="native", commands=[["true"]], checks_argv=[["python3", "{checks}/secret_scan.py", "release"]])


def test_native_profile_accepts_operator_checks():
    profile = Profile(runner="native", commands=[["true"]], checks_dir=str(ROOT / "checks"),
                      checks_argv=[["python3", "{checks}/secret_scan.py", "release"]])
    assert profile.checks_argv[0][1] == "{checks}/secret_scan.py"


def test_example_profiles_wire_real_checks():
    """The active profile must not look validated while running no operator checks."""
    for name in ("config-studio-linux.json", "config-native-linux.json"):
        data = json.loads((ROOT / "examples" / name).read_text())
        profile = Profile.model_validate(next(iter(data["profiles"].values())))
        if profile.runner == "native" and profile.tester:
            assert profile.checks_argv, f"{name} should run operator-owned checks"
            assert profile.checks_dir, f"{name} should name a checks directory"
            assert any("secret_scan" in " ".join(argv) for argv in profile.checks_argv)


def test_relative_checks_dir_resolves_against_the_checkout_not_the_cwd(tmp_path, monkeypatch):
    """A CWD-relative checks directory would silently run no operator checks."""
    from factory.verifier import resolve_checks_dir

    monkeypatch.chdir(tmp_path)
    profile = Profile(runner="native", commands=[["true"]], checks_dir="checks",
                      checks_argv=[["python3", "{checks}/secret_scan.py", "release"]])
    assert resolve_checks_dir(profile) == (ROOT / "checks").resolve()


def test_absolute_checks_dir_is_taken_as_written(tmp_path):
    from factory.verifier import resolve_checks_dir

    profile = Profile(runner="native", commands=[["true"]], checks_dir=str(tmp_path),
                      checks_argv=[["python3", "{checks}/secret_scan.py", "release"]])
    assert resolve_checks_dir(profile) == tmp_path.resolve()


def test_example_configs_do_not_hardcode_a_machine_specific_path():
    """A checked-in absolute path breaks other checkouts, and a path with spaces breaks Nuxt."""
    for name in ("config-studio-linux.json", "config-native-linux.json", "config.json"):
        assert "/home/" not in (ROOT / "examples" / name).read_text(), f"{name} hardcodes a home directory"


# --- End-to-end: operator checks gate publication -------------------------------------

def test_operator_check_blocks_publication_of_a_leaking_build(tmp_path, monkeypatch):
    """A successful build must not be publishable when it ships a credential."""
    from factory.db import Store
    from factory.engine import Engine
    from factory.models import AcceptanceFlow, ProjectInput, Review
    from factory.config import Settings

    server = ('const http=require("node:http");http.createServer((q,s)=>{if(q.url==="/api/health")'
              '{s.setHeader("Content-Type","application/json");s.end(\'{"ok":true}\');return;}'
              's.setHeader("Content-Type","text/html");s.end("<title>T</title><h1 id=h>Hi</h1>");'
              '}).listen(Number(process.env.PORT),"127.0.0.1");')

    leaked = "sk-abcdefghijklmnopqrstuvwxyz123456"

    class Agent:
        def generate(self, role, instruction, context, schema, cancelled):
            if role == "tester":
                return __import__("factory.testing", fromlist=["TestSuite"]).TestSuite(
                    summary="health and hero flow assertions",
                    cases=[{"flow_id": "health", "name": "Health contract", "kind": "api",
                            "actions": [{"action": "request", "path": "/api/health", "json_equals": {"ok": True}}]},
                           {"flow_id": "hero", "name": "Hero renders", "kind": "browser",
                            "actions": [{"action": "goto", "path": "/"},
                                        {"action": "expect_visible", "selector": "#h"}]}])
            assert role == "review", role
            return Review(approved=True, findings=[])

        def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False):
            """Harness double whose build output embeds a credential."""
            (worktree / "server.cjs").write_text(server)
            (worktree / "build.cjs").write_text(
                "const fs=require('node:fs');fs.mkdirSync('dist');"
                f"fs.writeFileSync('dist/index.html','<title>B</title><script>var k=\"{leaked}\"</script>');")
            return "scripted harness worker"

    monkeypatch.setenv("FACTORY_ALLOW_NATIVE", "1")
    executable = shutil_which_chromium()
    if executable:
        monkeypatch.setenv("FACTORY_CHROMIUM", executable)

    settings = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                        model="test/model", omp="/usr/bin/true",
                        profiles={"nativex": Profile(
                            runner="native", commands=[["node", "build.cjs"]], artifact_dir="dist",
                            tester=True, serve_argv=["node", "server.cjs"], readiness_path="/api/health",
                            checks_dir=str(ROOT / "checks"),
                            checks_argv=[["python3", "{checks}/secret_scan.py", "dist"]])})
    settings.validate()
    store = Store(tmp_path / "factory.sqlite3")
    project = store.create_project(ProjectInput(name="Leak demo", brief="Create a page that renders a hero heading.", profile="nativex"))
    flows = [AcceptanceFlow(id="health", kind="api", goal="Validate the JSON health endpoint.", steps=["GET /api/health"]),
             AcceptanceFlow(id="hero", kind="browser", goal="Visitor sees the hero heading.", steps=["Visit /"], selectors={"heading": "h1"})]
    from factory.models import Plan
    plan = Plan(summary="Build a page with a hero heading and health route.", tasks=[
        {"id": "web", "title": "Build page", "instructions": "Create the server and build script.",
         "acceptance": ["Page renders a heading"], "depends_on": []}], test_flows=flows)
    with store.tx() as db:
        db.execute("UPDATE projects SET plan=?,revision=1,state='awaiting_plan' WHERE id=?",
                   (plan.model_dump_json(), project["id"]))
    store.enqueue(project["id"], "build", {"revision": 1}, next_state="building")
    job = store.claim()
    # process() records the failure rather than re-raising, so assert on durable state.
    agent = Agent()
    Engine(settings, store, agent=agent, worker=agent).process(job)
    assert store.one("SELECT state FROM jobs WHERE id=?", (job["id"],))["state"] == "failed"
    row = store.project(project["id"])
    assert row["state"] == "failed"
    assert not row["artifact"], "A leaking build must never be published"
    log = (tmp_path / "jobs" / job["id"] / "verification.txt").read_text()
    assert "secret_scan" in log or "SECRET LEAK" in log


def shutil_which_chromium():
    import shutil
    return shutil.which("chromium") or shutil.which("google-chrome")
