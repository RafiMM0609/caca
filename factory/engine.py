import fcntl
import json
import logging
import shutil
import signal
import threading
import time

from .agents import OmpAgent
from .db import Store, uid
from .deploy import Deployer
from .memory import Memory
from .models import Change, Plan, Review
from .process import Cancelled
from .verifier import Verifier
from .workspace import apply, atomic_json, clone_tree, package, snapshot

logger = logging.getLogger(__name__)


class Engine:
    def __init__(self, settings, store=None, agent=None, verifier=None, memory=None, deployer=None):
        self.settings = settings
        self.store = store or Store(settings.data / "factory.sqlite3")
        self.agent = agent or OmpAgent(settings)
        self.verifier = verifier or Verifier()
        self.memory = memory or Memory(settings)
        self.deployer = deployer or Deployer()
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
                context = {"brief": project["brief"], "profile": project["profile"],
                           "memory": self.memory_operation(project_id, job_id, "search", project["brief"])}
                plan = self.generate(job_id, "planner", "Design a concrete ordered task plan with acceptance criteria. "
                                           "List unresolved business questions in questions. Do not invent requirements. "
                                           "The static profile serves plain HTML/CSS/JS, with index.html at the root. "
                                           "Other profiles must follow the operator's verification contract.",
                                           context, Plan, cancelled)
                atomic_json(work / "plan.json", plan.model_dump())
                with store.tx() as db:
                    db.execute("UPDATE projects SET plan=?,revision=revision+1,state='awaiting_plan' WHERE id=?",
                               (plan.model_dump_json(), project_id))
                result = {"plan": plan.model_dump()}
            elif job["kind"] == "build":
                result = self.build(project, job, work, cancelled)
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

    def build(self, project, job, work, cancelled):
        store = self.store
        plan = Plan.model_validate_json(project["plan"])
        profile = self.settings.profiles[project["profile"]]
        payload = json.loads(job["payload"])
        if payload["revision"] != project["revision"] or plan.questions:
            raise ValueError("Plan approval is stale or unresolved")
        source = work / "source"
        previous = json.loads(project["artifact"] or "null")
        if previous:
            clone_tree(self.settings.data / "jobs" / previous["job_id"] / "source", source)
        else:
            source.mkdir()
        memory = self.memory_operation(project["id"], job["id"], "search", plan.summary)
        for task in plan.tasks:
            if cancelled():
                raise Cancelled("Cancelled between tasks")
            self.store.event(project["id"], job["id"], "task.running", {"task": task.id, "title": task.title})
            context = {"brief": project["brief"], "plan": plan.model_dump(), "task": task.model_dump(),
                       "files": snapshot(source), "memory": memory,
                       "verification": profile.model_dump(exclude={"checks_dir"})}
            feedback = []
            candidate = work / (task.id + "-candidate")
            accepted = False
            for attempt in range(self.settings.max_repairs + 1):
                if cancelled():
                    raise Cancelled("Cancelled during task")
                if candidate.exists():
                    shutil.rmtree(candidate)
                clone_tree(source, candidate)
                store.event(project["id"], job["id"], "task.implementing", {"task": task.id, "attempt": attempt + 1})
                change = self.generate(job["id"], "implementer", "Implement this task. Return complete UTF-8 file contents "
                                             "for changed files and explicit deletions. No placeholders. "
                                             "Verification commands and production credentials are outside your control.",
                                             {**context, "review_feedback": feedback}, Change, cancelled)
                apply(candidate, change)
                atomic_json(work / f"{task.id}-{attempt}-change.json", change.model_dump())
                store.event(project["id"], job["id"], "task.reviewing", {"task": task.id, "attempt": attempt + 1})
                review = self.generate(job["id"], "review", "Independently review correctness, acceptance criteria and "
                                             "security. Inspect the actual files. Reject incomplete or unsafe changes. "
                                             "You cannot certify test execution; the verifier does that separately.",
                                             {**context, "candidate": snapshot(candidate)}, Review, cancelled)
                atomic_json(work / f"{task.id}-{attempt}-review.json", review.model_dump())
                store.event(project["id"], job["id"], "task.reviewed",
                            {"task": task.id, "attempt": attempt + 1, **review.model_dump()})
                if review.approved:
                    shutil.rmtree(source)
                    candidate.rename(source)
                    accepted = True
                    break
                feedback = review.findings
            if not accepted:
                raise ValueError("Review failed after bounded repair attempts")
            store.event(project["id"], job["id"], "task.accepted", {"task": task.id})
        store.event(project["id"], job["id"], "verification.running", {})
        output = work / "output"
        log = self.verifier.verify(profile, source, output, job["id"], cancelled)
        (work / "verification.txt").write_text(log)
        if cancelled():
            raise Cancelled("Cancelled before publishing artifact")
        artifact = package(output, work / "artifact.tar.gz")
        artifact.update({"job_id": job["id"], "revision": project["revision"]})
        atomic_json(work / "artifact.json", artifact)
        with store.tx() as db:
            db.execute("UPDATE projects SET state='ready',artifact=? WHERE id=?", (json.dumps(artifact), project["id"]))
            store.event_tx(db, project["id"], job["id"], "verification.passed", artifact)
        self.memory_operation(project["id"], job["id"], "save", "Verified factory build", plan.summary,
                              "artifact:" + artifact["digest"])
        return artifact

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
