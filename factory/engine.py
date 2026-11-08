from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import logging
import signal
import threading
import time

from .agents import PiAgent
from .db import Store, uid
from .deploy import Deployer
from .memory import Memory
from .models import Plan, Review
from .parallel import WORKER_BRIEF, ParallelBuilder
from . import products
from .testing import TestSuite
from .process import Cancelled
from .verifier import Verifier
from .workspace import atomic_json, package

logger = logging.getLogger(__name__)


class Engine:
    def __init__(self, settings, store=None, agent=None, verifier=None, memory=None, deployer=None, worker=None):
        self.settings = settings
        self.store = store or Store(settings.data / "factory.sqlite3")
        self.agent = agent or PiAgent(settings)
        self.verifier = verifier or Verifier()
        self.memory = memory or Memory(settings)
        self.deployer = deployer or Deployer()
        self.parallel = ParallelBuilder(settings, self.store, worker=worker)
        self.stop = threading.Event()

    def memory_operation(self, project, job, method, *args):
        if not self.settings.memorix:
            return ""
        try:
            return getattr(self.memory, method)(project, *args)
        except Exception:
            self.store.event(project, job, "memory.unavailable", {"operation": method})
            return ""

    def generate(self, job, *args):
        with self.store.tx() as db:
            row = db.execute("SELECT reserved_usd FROM jobs WHERE id=?", (job,)).fetchone()
            if row[0] + self.settings.max_cost > self.settings.max_job_cost:
                raise ValueError("Job's reserved model-call budget exhausted")
            db.execute("UPDATE jobs SET reserved_usd=reserved_usd+? WHERE id=?", (self.settings.max_cost, job))
        return self.agent.generate(*args)

    def process(self, job, lane="build"):
        store, settings = self.store, self.settings
        job_id, project_id = job["id"], job["project_id"]
        project = store.project(project_id)
        done = threading.Event()

        def cancelled():
            return self.stop.is_set() or bool(store.one("SELECT cancel FROM jobs WHERE id=?", (job_id,))["cancel"])

        def heartbeat():
            while not done.wait(2):
                store.heartbeat(job_id, lane)

        pulse = threading.Thread(target=heartbeat, daemon=True)
        pulse.start()
        work = settings.data / "jobs" / job_id
        work.mkdir(parents=True, mode=0o700)
        try:
            if cancelled():
                raise Cancelled("Cancelled before execution")
            if job["kind"] == "plan":
                context = {"brief": project["brief"], "profile": project["profile"], "verification": settings.profiles[project["profile"]].model_dump(),
                           "memory": self.memory_operation(project_id, job_id, "search", project["brief"])}
                plan = self.generate(job_id, "planner", "Design a concrete task plan with acceptance criteria. "
                                           "Tasks form a DAG: use depends_on to declare real dependencies. Tasks "
                                           "whose dependencies are satisfied run in parallel as separate workers, "
                                           "so leave depends_on empty for work that can proceed independently, and "
                                           "declare an ordering only where the work truly requires it. Each task "
                                           "must be achievable by one worker on its own worktree. "
                                           "List unresolved business questions in questions. Do not invent requirements. "
                                           "The static profile serves plain HTML/CSS/JS, with index.html at the root. "
                                           "Other profiles must follow the operator's verification contract. "
                                           "For tester-enabled profiles include test_flows with explicit API routes/status/JSON "
                                           "and browser routes/selectors/actions/expected outcomes. These are the independent tester contract.",
                                           context, Plan, cancelled)
                atomic_json(work / "plan.json", plan.model_dump())
                with store.tx() as db:
                    db.execute("UPDATE projects SET plan=?,revision=revision+1,state='awaiting_plan' WHERE id=?",
                               (plan.model_dump_json(), project_id))
                result = {"plan": plan.model_dump()}
            elif job["kind"] in ("build", "verify"):
                result = self.build(project, job, work, cancelled, verification_only=job["kind"] == "verify")
            elif job["kind"] == "deploy":
                result = self.deploy(project, job, cancelled)
            else:
                raise ValueError("Unknown job kind")
            store.finish(job_id, "succeeded", result)
        except Cancelled:
            store.finish(job_id, "cancelled", error="Cancelled; inspect deployment status if applicable")
        except Exception as exc:
            # No raw exception text: providers, SSH, or invalid model output may contain secrets.
            logger.error("Job %s failed (%s)", job_id, type(exc).__name__)
            store.finish(job_id, "failed", error=f"{type(exc).__name__}: execution failed; inspect task evidence and readiness")
        finally:
            done.set()
            pulse.join(timeout=5)

    def _spacing(self, cancelled, seconds=30):
        for _ in range(max(1, seconds // 3)):
            if cancelled():
                raise Cancelled("Cancelled while spacing retry")
            time.sleep(3)

    def prepare_tests(self, project, job, work, plan, cancelled):
        self.store.event(project["id"], job["id"], "tester.preparing", {})
        instruction = (
            "Prepare independent executable API and browser acceptance cases for every planner flow. "
            "Create exactly one case per flow: copy each flow's id into case.flow_id and its kind "
            "into case.kind; do not invent, rename or omit flow ids. "
            "Use only the contract in the plan, not implementation code. Include real assertions. "
            "Use CSS selectors and relative paths only. Do not weaken expected behavior.")
        flows = [f.model_dump() for f in plan.test_flows]
        suite = None
        for attempt in range(self.settings.max_repairs + 1):
            if cancelled():
                raise Cancelled("Cancelled while preparing tests")
            context = {"brief": project["brief"], "flows": flows}
            if attempt:
                # Wait between attempts instead of re-hitting the provider rapidly.
                for _ in range(20):
                    if cancelled():
                        raise Cancelled("Cancelled while preparing tests")
                    time.sleep(3)
                context["previous_attempt_note"] = \
                    "A previous response was rejected by structural validation. Re-check flow ids and kinds."
            try:
                suite = self.generate(job["id"], "tester", instruction, context, TestSuite, cancelled)
                suite.validate_coverage(plan.test_flows)
            except Cancelled:
                raise
            except Exception as exc:
                detail = str(getattr(exc, "output", "") or exc)
                (work / f"tester-prep-attempt-{attempt}.txt").write_text(detail[-100000:])
                self.store.event(project["id"], job["id"],
                                 "tester.prep_attempt_failed", {"attempt": attempt + 1, "reason": type(exc).__name__})
                suite = None
            else:
                break
        if suite is None:
            raise ValueError("Tester preparation failed after bounded attempts; inspect prep evidence")
        atomic_json(work / "test-suite.json", suite.model_dump())
        self.store.event(project["id"], job["id"], "tester.prepared", {"cases": len(suite.cases)})
        return suite

    def build(self, project, job, work, cancelled, verification_only=False):
        profile = self.settings.profiles[project["profile"]]
        plan = Plan.model_validate_json(project["plan"])
        if not profile.tester:
            return self._build(project, job, work, cancelled, verification_only=verification_only)
        if not plan.test_flows:
            raise ValueError("Tester-enabled profiles require planner test flows")
        abort = threading.Event()
        def check():
            return cancelled() or abort.is_set()
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="factory-tester") as pool:
            future = pool.submit(self.prepare_tests, project, job, work, plan, check)
            try:
                return self._build(project, job, work, check, future, verification_only)
            finally:
                abort.set()

    def _build(self, project, job, work, cancelled, tester_future=None, verification_only=False):
        store = self.store
        plan = Plan.model_validate_json(project["plan"])
        profile = self.settings.profiles[project["profile"]]
        payload = json.loads(job["payload"])
        if payload["revision"] != project["revision"] or plan.questions:
            raise ValueError("Plan approval is stale or unresolved")
        root = products.ensure_repo(self.settings.data, project["id"])
        products.checkout_target(root)
        memory = self.memory_operation(project["id"], job["id"], "search", plan.summary)

        def instructions_for(task, base_context):
            # The worker reads the real files itself, so it gets the task contract and the
            # product brief rather than a full source dump.
            return (
                WORKER_BRIEF
                + "\n\nPRODUCT BRIEF:\n" + project["brief"]
                + "\n\nPLAN SUMMARY:\n" + plan.summary
                + "\n\nYOUR TASK (" + task.id + "): " + task.title
                + "\n" + task.instructions
                + "\n\nACCEPTANCE CRITERIA:\n" + "\n".join("- " + item for item in task.acceptance)
                + ("\n\nOTHER TASKS IN THE PLAN:\n" + "\n".join(
                    "- " + other.id + ": " + other.title for other in plan.tasks if other.id != task.id)
                   if len(plan.tasks) > 1 else "")
                + ("\n\nPROJECT MEMORY (advisory):\n" + memory if memory else "")
                + ("\n\nPREVIOUS ATTEMPT FEEDBACK:\n" + base_context.get("feedback", "")
                   if base_context.get("feedback") else "")
            )

        def review_for(task, worktree, diff, base):
            # Review sees the diff, not the worker's own description of its work.
            verdict = self.generate(job["id"], "review",
                                    "Independently review this diff against the task acceptance criteria and "
                                    "security. You see only the change. Reject incomplete, unsafe or "
                                    "unrelated edits. You cannot certify test execution; the verifier does that. "
                                    "`changed_files` is the authoritative list of every file the worker touched. "
                                    "Lockfile contents are omitted from `diff` because they are generated and "
                                    "oversized; a changed lockfile is not an error, and an omitted file is not a "
                                    "missing file. Judge only what the diff actually shows.",
                                    {"brief": project["brief"], "task": task.model_dump(),
                                     "changed_files": products.changed_files(worktree, base),
                                     "diff": diff or "(no file changes)"}, Review, cancelled)
            atomic_json(work / f"{task.id}-review.json", verdict.model_dump())
            return verdict.model_dump()

        workers_used = []
        studio_branch = None
        studio_worktree = None
        if project.get("pending_edit"):
            # A reviewed Studio draft replaces the content tree. It is published on its own
            # branch and only merged into the product branch after verification passes, so a
            # failed edit cannot overwrite a previously verified artifact.
            studio_branch, studio_worktree = self.import_studio_edit(project, job, root)
            source = products.export_tree_worktree(studio_worktree, work / "source")
        elif not verification_only:
            outcomes = self.parallel.build_tasks(project, job, plan, instructions_for, cancelled, review_for)
            workers_used = [outcome.as_dict() for outcome in outcomes]
            atomic_json(work / "workers.json", workers_used)
            source = products.export_tree(root, work / "source")
        else:
            source = products.export_tree(root, work / "source")

        store.event(project["id"], job["id"], "verification.running", {})
        output = work / "output"
        suite = tester_future.result() if tester_future else None
        if suite:
            def test_event(kind, detail):
                store.event(project["id"], job["id"], kind, detail)
            log = self.verifier.verify(profile, source, output, job["id"], cancelled,
                                       suite=suite, evidence=work / "testing", on_event=test_event)
        else:
            log = self.verifier.verify(profile, source, output, job["id"], cancelled)
        (work / "verification.txt").write_text(log)
        if cancelled():
            raise Cancelled("Cancelled before publishing artifact")
        artifact = package(output, work / "artifact.tar.gz")
        artifact.update({"job_id": job["id"], "revision": project["revision"]})
        atomic_json(work / "artifact.json", artifact)
        if studio_branch:
            products.checkout_target(root)
            products.merge_branch(root, studio_branch, f"Apply Studio edit for revision {project['revision']}")
            products.branch_delete(root, studio_branch)
        with store.tx() as db:
            db.execute("UPDATE projects SET state='ready',artifact=?,pending_edit=NULL WHERE id=?", (json.dumps(artifact), project["id"]))
            store.event_tx(db, project["id"], job["id"], "verification.passed", artifact)
        self.memory_operation(project["id"], job["id"], "save", "Verified factory build", plan.summary,
                              "artifact:" + artifact["digest"])
        return artifact

    def import_studio_edit(self, project, job, root):
        """Copy a reviewed Studio draft onto its own branch of the product repository.

        The draft is frozen content the operator already reviewed. It is not regenerated by
        a worker, and it must not touch the product branch until verification passes.
        """
        edit = project["pending_edit"]
        draft = self.settings.data / "edits" / edit / "source"
        if not draft.is_dir():
            raise ValueError("Studio draft source is missing")
        branch = f"studio/{job['id'][:12]}"
        worktree = self.settings.data / "workers" / job["id"] / "studio"
        for stale in products.list_worktrees(root):
            if stale.get("branch") == branch and stale.get("path"):
                products.worktree_remove(root, products.Path(stale["path"]))
        if worktree.exists():
            products.worktree_remove(root, worktree)
        products.branch_delete(root, branch)
        products.worktree_add(root, worktree, branch)
        # Replace the whole tracked content surface with the reviewed draft.
        for path in sorted(products.tracked_files(worktree)):
            target = worktree / path
            if target.is_file():
                target.unlink()
        for path in sorted(draft.rglob("*")):
            if path.is_file():
                target = worktree / path.relative_to(draft)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(path.read_text())
        products.commit_all(worktree, f"Apply Studio edit {edit[:12]}")
        self.store.event(project["id"], job["id"], "studio.imported",
                         {"draft": edit, "branch": branch})
        return branch, worktree

    def deploy(self, project, job, cancelled):
        payload = json.loads(job["payload"])
        target = self.settings.targets[project["id"] + ":" + payload["environment"]]
        artifact = json.loads(project["artifact"])
        if artifact["digest"] != payload["digest"]:
            raise ValueError("Stale deployment artifact")
        path = self.settings.data / "jobs" / artifact["job_id"] / "artifact.tar.gz"
        deployment = uid()
        with self.store.tx() as db:
            db.execute("INSERT INTO deployments VALUES(?,?,?,?,?,?,?,?)", (deployment, project["id"], job["id"],
                       payload["environment"], payload["digest"], "running", None, time.time()))
        try:
            receipt = self.deployer.apply(target, path, artifact["digest"], cancelled)
            with self.store.tx() as db:
                db.execute("UPDATE deployments SET state='succeeded',receipt=? WHERE id=?",
                           (json.dumps(receipt), deployment))
            return receipt
        except BaseException:
            with self.store.tx() as db:
                db.execute("UPDATE deployments SET state='unknown' WHERE id=?", (deployment,))
            raise

    def serve(self, lane="build"):
        lock_path = self.settings.data / (lane + ".lock")
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(f"Another {lane} scheduler holds the lock") from exc
            if lane == "build":
                for job in self.store.all("SELECT id FROM jobs WHERE state='running' AND kind='build'"):
                    self.verifier.cleanup(job["id"])
            self.store.recover(lane)
            signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
            signal.signal(signal.SIGINT, lambda *_: self.stop.set())
            while not self.stop.is_set():
                self.store.heartbeat(lane=lane)
                job = self.store.claim(lane)
                if job:
                    self.process(job, lane)
                else:
                    self.stop.wait(1)
