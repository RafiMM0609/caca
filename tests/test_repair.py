"""A failing gate produces a bounded repair, not a dead job.

Phase 0 closed the loop; this covers what happens when the loop hits a red gate. The gate
must never be weakened to pass, the repair must be bounded, and exhaustion must fail loudly
rather than publish something that does not work.
"""
import json
from pathlib import Path


from factory import repair as repair_module
from factory.engine import Engine
from factory.models import Review


class ScriptedAgent:
    """Planner, reviewer and tester double. Never a live model.

    The tester role matters: a tester-enabled profile makes `Engine.prepare_tests()` call
    `generate("tester", ..., TestSuite)`. Leaving that role unhandled returned a `Review`,
    the suite failed `validate_coverage`, and the test burned three real retries with a
    sleep between them before failing for the wrong reason.
    """

    def __init__(self, plan):
        self.plan = plan

    def generate(self, role, instruction, context, schema, cancelled):
        if role == "planner":
            return self.plan
        if role == "tester":
            from factory.testing import TestSuite
            return TestSuite(summary="One browser case covering the home page hero.", cases=[
                {"flow_id": "homepage", "name": "Home shows the hero", "kind": "browser",
                 "actions": [
                     {"action": "goto", "path": "/"},
                     {"action": "expect_text", "selector": "[data-testid=hero]",
                      "value": "Ada"},
                 ]}])
        return Review(approved=True, findings=[])


class ScriptedWorker:
    """Writes a real file into its worktree; records every instruction it is given."""

    def __init__(self):
        self.instructions = []
        self.timeouts = []

    def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False,
                 skills=()):
        self.instructions.append(instruction)
        self.timeouts.append(timeout)
        target = Path(worktree) / "index.html"
        # A repair run must actually change the tree: a repair that commits nothing is
        # rejected on purpose, so an idle double would test the wrong branch.
        if "verification gate failed" in instruction:
            target.write_text(
                "<!doctype html><html lang='en'><head><title>Portfolio</title></head>"
                "<body><h1 data-testid='hero'>Ada</h1><p>ada@example.test</p></body></html>")
        else:
            target.write_text(
                "<!doctype html><html lang='en'><head><title>Portfolio</title></head>"
                "<body><h1>Ada</h1><p>ada@example.test</p></body></html>")
        return "scripted harness worker"


class FailingThenPassingVerifier:
    """Fails the gate a fixed number of times, writing a tester report each time."""

    __test__ = False

    def __init__(self, failures=1):
        self.failures = failures
        self.calls = 0

    def verify(self, profile, source, output, job, cancelled, **testing):
        self.calls += 1
        evidence = testing.get("evidence")
        if self.calls <= self.failures:
            if evidence:
                evidence.mkdir(parents=True, exist_ok=True)
                (evidence / "report.json").write_text(json.dumps({
                    "status": "failed",
                    "cases": [
                        {"flow_id": "homepage", "name": "Home shows the hero", "kind": "browser",
                         "status": "failed", "error": "AssertionError",
                         "message": "None of 1 elements matched [data-testid=hero] contained "
                                    "the expected text 'Kopi Senja'",
                         "screenshot": "case-1.png"},
                        {"flow_id": "health", "name": "Health is ok", "kind": "api",
                         "status": "passed"},
                    ]}))
            raise AssertionError("tester case failed")
        output.mkdir(exist_ok=True)
        (output / "index.html").write_text(
            (Path(source) / "index.html").read_text() if (Path(source) / "index.html").is_file()
            else "<title>ok</title>")
        return "TEST DOUBLE: gate passed"


def with_flows(store, project, plan):
    """Give the plan a test flow, which a tester-enabled profile requires before approval."""
    from factory.models import AcceptanceFlow

    plan.test_flows = [AcceptanceFlow(
        id="homepage", kind="browser", goal="The home page shows the hero and identity.",
        steps=["Visit / and assert the hero text"],
        selectors={"hero": "[data-testid=hero]"})]
    with store.tx() as db:
        db.execute("UPDATE projects SET plan=? WHERE id=?", (plan.model_dump_json(), project["id"]))
    return store.project(project["id"])


def build(settings, store, project, verifier, worker, plan):
    """Run a build with a tester-enabled profile, so `evidence` reaches the verifier.

    The repair loop reads the tester's own report, which `Engine` only passes through when a
    suite exists. Enabling the tester on the profile is what a real failing run looks like.
    """
    from factory.models import Profile

    project = with_flows(store, project, plan)
    settings.profiles["static"] = Profile(
        runner="native", commands=[["true"]], tester=True,
        serve_argv=["node", "server.cjs"], readiness_path="/api/health")
    job = store.enqueue(project["id"], "build", {"revision": project["revision"]},
                        allowed={"awaiting_plan"}, next_state="building")
    Engine(settings, store, ScriptedAgent(plan), verifier, worker=worker).process(
        store.claim())
    return job


def test_a_failing_gate_triggers_a_repair_and_republishes(settings, store, project, plan):
    """Done-when for Phase 1: one broken assertion causes a repair and a republish."""
    verifier = FailingThenPassingVerifier(failures=1)
    worker = ScriptedWorker()
    job = build(settings, store, project, verifier, worker, plan)

    result = store.project(project["id"])
    assert result["state"] == "ready", "the repaired build must be published"
    assert result["artifact"], "a repaired build still produces an artifact"
    assert verifier.calls == 2, "the gate ran, failed, repaired, then ran again"

    kinds = [e["kind"] for e in store.all(
        "SELECT kind FROM events WHERE job_id=? ORDER BY seq", (job["id"],))]
    assert "repair.evidence" in kinds
    assert "repair.running" in kinds
    assert "repair.committed" in kinds
    assert "repair.retrying" in kinds


def test_the_repair_worker_is_told_what_actually_failed(settings, store, project, plan):
    """The instruction must quote the assertion, not just the case name."""
    verifier = FailingThenPassingVerifier(failures=1)
    worker = ScriptedWorker()
    build(settings, store, project, verifier, worker, plan)

    assert len(worker.instructions) == 2, "one implementation run, one repair run"
    repair_brief = worker.instructions[1]
    assert "Kopi Senja" in repair_brief, "the expected text must be quoted"
    assert "data-testid=hero" in repair_brief, "the selector must be quoted"
    assert "do not" in repair_brief.lower(), "the brief must forbid weakening the checks"


def test_repair_never_asks_the_worker_to_edit_the_tests(settings, store, project, plan):
    verifier = FailingThenPassingVerifier(failures=1)
    worker = ScriptedWorker()
    build(settings, store, project, verifier, worker, plan)
    brief = worker.instructions[1].lower()
    assert "do not edit or delete the test suite" in brief
    assert "do not delete an assertion" in brief


class ChangingWorker(ScriptedWorker):
    """Every run writes different content, so exhaustion is what stops the loop."""

    def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False,
                 skills=()):
        self.instructions.append(instruction)
        (Path(worktree) / "index.html").write_text(
            f"<title>attempt {len(self.instructions)}</title>")
        return "changed something"


def test_exhausted_repairs_fail_the_job_and_publish_nothing(settings, store, project, plan):
    """A partially working application must never be published as verified."""
    verifier = FailingThenPassingVerifier(failures=99)
    worker = ChangingWorker()
    build(settings, store, project, verifier, worker, plan)

    result = store.project(project["id"])
    assert result["state"] == "failed"
    assert result["artifact"] is None
    # max_repairs defaults to 2, so the gate runs three times and stops.
    assert verifier.calls == settings.max_repairs + 1


def test_a_repair_that_changes_nothing_stops_instead_of_burning_attempts(settings, store,
                                                                        project, plan):
    """Identical evidence will fail identically; say so rather than repeating it."""
    verifier = FailingThenPassingVerifier(failures=99)

    class IdleWorker(ScriptedWorker):
        """Writes once, then never again: the repair run changes nothing."""

        def run_task(self, worktree, instruction, model="", timeout=0, cancelled=lambda: False,
                 skills=()):
            self.instructions.append(instruction)
            if len(self.instructions) == 1:      # the initial implementation writes the file
                (Path(worktree) / "index.html").write_text("<title>portfolio</title>")
            return "no changes"

    worker = IdleWorker()
    job = build(settings, store, project, verifier, worker, plan)
    assert store.project(project["id"])["state"] == "failed"
    kinds = [e["kind"] for e in store.all("SELECT kind FROM events WHERE job_id=?",
                                          (job["id"],))]
    assert "repair.running" in kinds
    # The guard fires right after the idle repair commits nothing, so the gate is never
    # re-run: re-verifying unchanged code would reproduce the identical failure.
    assert verifier.calls == 1, "a no-op repair must not burn the remaining attempts"
    # The job error is deliberately generic (provider output can contain secrets), so the
    # observable proof is the event trail: evidence, one repair, and no retry after it.
    assert "repair.committed" in kinds
    assert kinds.count("verification.running") == 1
    assert "repair.retrying" not in kinds


def test_failing_cases_are_extracted_from_the_report(tmp_path):
    evidence_dir = tmp_path / "testing"
    evidence_dir.mkdir()
    (evidence_dir / "report.json").write_text(json.dumps({"status": "failed", "cases": [
        {"flow_id": "a", "kind": "api", "status": "failed", "error": "AssertionError",
         "message": "status 200 != 500"},
        {"flow_id": "b", "kind": "browser", "status": "passed"},
    ]}))
    found = repair_module.failing_cases(evidence_dir / "report.json")
    assert [c["flow_id"] for c in found] == ["a"]
    assert "status 200 != 500" in found[0]["message"]


def test_a_missing_report_is_not_an_error(tmp_path):
    assert repair_module.failing_cases(tmp_path / "nope.json") == []
    evidence = repair_module.describe_evidence(tmp_path)
    assert evidence["failures"] == []
    brief = repair_module.repair_instruction(evidence)
    assert "no evidence was captured" in brief


class ContractDefectVerifier:
    """The gate fails because the acceptance *case* is defective, not the application."""

    __test__ = False

    def __init__(self):
        self.calls = 0

    def verify(self, profile, source, output, job, cancelled, **testing):
        self.calls += 1
        from factory.testing import ContractDefect

        evidence = testing.get("evidence")
        if evidence:
            evidence.mkdir(parents=True, exist_ok=True)
            (evidence / "report.json").write_text(json.dumps({
                "status": "failed",
                "cases": [{"flow_id": "health", "name": "Health is ok", "kind": "api",
                           "status": "failed", "error": "ContractDefect",
                           "message": "API JSON assertion failed: the case asserts a partial "
                                      "object; use json_contains"}]}))
        raise ContractDefect("API JSON assertion failed: the case asserts a partial object; "
                            "use json_contains")


def test_a_contract_defect_does_not_spend_a_repair_worker(settings, store, project, plan):
    """The recorded production failure, as a regression.

    A case demanded an exact match on a partial object. The gate failed, the loop ran a repair
    worker, and that worker deleted fields from a correct API (adding `?view=full`) so the
    default response shrank to match the test — 22 files changed, job still failed, and the
    application was left worse than it started. When the defect is provably the test's, no
    worker may be spent on the application at all.
    """
    verifier = ContractDefectVerifier()
    worker = ScriptedWorker()
    job = build(settings, store, project, verifier, worker, plan)

    # Exactly one run: the ordinary implementation worker. A second would be a repair worker,
    # which is what must not happen here.
    assert len(worker.instructions) == 1, "no repair worker may run for a defective contract"
    assert verifier.calls == 1, "the gate must not be retried against an unsatisfiable case"
    result = store.project(project["id"])
    assert result["state"] != "ready", "an unsatisfiable gate must never publish"
    assert not result["artifact"]

    events = {e["kind"]: e for e in store.all(
        "SELECT kind, detail FROM events WHERE job_id=? ORDER BY seq", (job["id"],))}
    assert "contract.defect" in events, "the operator must be told the test is the defect"
    assert "json_contains" in events["contract.defect"]["detail"]
    assert "repair.running" not in events, "nothing is repaired when nothing can be fixed"


def test_the_repair_worker_gets_the_worker_budget_not_the_call_budget(settings, store, project, plan):
    """Measured production failure, as a regression.

    `_repair_then_retry` called `run_task` with no `timeout`, so the repair worker inherited
    `agent_timeout` (600s), the budget for a one-shot JSON call, while ordinary workers get
    `worker_timeout` (1800s). A repair on the rempah-rempah POS application was killed at 600.5s
    mid-edit, leaving five modified files and a job that reported only "execution failed".
    """
    verifier = FailingThenPassingVerifier(failures=1)
    worker = ScriptedWorker()
    build(settings, store, project, verifier, worker, plan)

    assert len(worker.timeouts) == 2, "one implementation run, one repair run"
    assert all(t == settings.worker_timeout for t in worker.timeouts), (
        "every harness worker, repair included, gets the worker budget; got "
        f"{worker.timeouts} against worker_timeout={settings.worker_timeout}")
    assert settings.worker_timeout > settings.agent_timeout, (
        "this regression is only meaningful while the two budgets differ")
