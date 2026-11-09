from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import logging
import shutil
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
from . import artifact as artifact_module
from . import repair as repair_module
from .workspace import atomic_json, package

logger = logging.getLogger(__name__)

PLANNER_PROMPT = (
    "Design a concrete task plan with acceptance criteria. "
    "Tasks form a DAG: use depends_on to declare real dependencies. Tasks "
    "whose dependencies are satisfied run in parallel as separate workers, "
    "so leave depends_on empty for work that can proceed independently, and "
    "declare an ordering only where the work truly requires it. Each task "
    "must be achievable by one worker on its own worktree. "
    "Tasks that write interface code may set `skills` to a list of vendored design-skill "
    "names. Available: emil-design-eng, animate, apple-design, mobile-native, break-ui. Use "
    "them ONLY on tasks that build user interfaces; a skill is prompt cost on every run of "
    "the task that carries it and does nothing for an API-only task. At most four per task. "
    "IMPORTANT: the factory verifies the merged result after your workers finish. It runs "
    "the profile's install and build commands, the operator checks, and every test_flow you "
    "declare. Do not add a task that only re-runs installation, a build, a server start or an "
    "endpoint smoke check: that duplicates the verifier, costs a worker and a full model call, "
    "and its result is discarded. Only plan tasks that change source. If genuinely nothing "
    "else needs building, plan the source tasks alone. "
    "List unresolved business questions in questions. Do not invent requirements. "
    "The static profile serves plain HTML/CSS/JS, with index.html at the root. "
    "Other profiles must follow the operator's verification contract. "
    "Persistence: unless the brief explicitly names an external database "
    "(for example Supabase, Postgres, MySQL or any hosted database with a "
    "hostname, port and password), the application must store data in a local "
    "SQLite file. The environment provides DATABASE_URL pointing at that file "
    "and DATA_DIR pointing at the directory that holds it; both survive "
    "restarts and must be read rather than hardcoded. Only choose an external "
    "database when the brief asks for one by name. "
    "For tester-enabled profiles include test_flows with explicit API routes/status/JSON "
    "and browser routes/selectors/actions/expected outcomes. These are the independent tester contract. "
    "Every browser flow MUST list the selectors it depends on in its `selectors` map "
    "(label -> CSS selector). Selector labels are snake_case, matching [a-z][a-z0-9_-]*; "
    "`shopName` is invalid, `shop_name` is correct. The task instructions must use exactly "
    "those same selectors. Declare every state explicitly, including fields shown while a "
    "row is being edited: the worker builds what the instructions name and the tester "
    "asserts only the declared selectors, so an undeclared selector makes a correct page fail."
)


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

        # Scale the ceiling with the plan rather than using a flat number: a flat ceiling
        # either kills a legitimate larger plan or lets a wedged one run for hours. Waves,
        # not tasks, drive the cost because a wave runs concurrently.
        budget = self._job_budget(project, job_id, store)
        started = time.monotonic()
        deadline = started + budget
        store.event(project_id, job_id, "job.budget", {"seconds": budget,
                                                       "job_timeout": settings.job_timeout})

        def expired():
            return time.monotonic() > deadline

        def cancelled():
            # A job-level deadline as well as a cancel flag. `agent_timeout` bounds one model
            # call; a build is several waves plus a build, acceptance tests and a possible
            # repair loop, so a wedged job needed its own ceiling rather than an unbounded
            # wait behind a heartbeat that keeps looking healthy.
            return (self.stop.is_set()
                    or expired()
                    or bool(store.one("SELECT cancel FROM jobs WHERE id=?", (job_id,))["cancel"]))

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
                # The planner gets the same bounded retry the tester already has. A single
                # malformed generation used to fail the entire job on the first attempt, even
                # though an identical call seconds later could succeed. Model output is not
                # deterministic, so one bad sample must not end a run.
                plan = None
                for attempt in range(settings.max_repairs + 1):
                    if cancelled():
                        raise Cancelled("Cancelled while planning")
                    context["previous_attempt_note"] = (
                        "" if not attempt else
                        "Your previous response could not be parsed as the required plan schema. "
                        "Return only JSON matching the schema: a DAG of tasks with acceptance "
                        "criteria, and one test_flow per observable behaviour with its selectors.")
                    try:
                        plan = self.generate(job_id, "planner", PLANNER_PROMPT, context, Plan, cancelled)
                    except Cancelled:
                        raise
                    except Exception as exc:
                        (work / f"plan-attempt-{attempt}.txt").write_text(
                            str(getattr(exc, "output", "") or exc)[-100000:])
                        store.event(project_id, job_id, "plan.attempt_failed",
                                    {"attempt": attempt + 1, "reason": type(exc).__name__})
                        if attempt >= settings.max_repairs:
                            raise
                        self._spacing(cancelled)
                    else:
                        break
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
            self._record_timing(store, project_id, job_id, started, budget)
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

    def _record_timing(self, store, project, job, started, budget):
        """Summarise where this job's wall time went.

        A single total is not actionable: 15 minutes of workers needs a different response
        from 15 minutes of acceptance tests. The breakdown is read back from the events this
        job already emitted, so it cannot disagree with them.
        """
        events = store.all("SELECT kind, detail FROM events WHERE job_id=? ORDER BY seq",
                           (job,))
        waves = []
        workers = {}
        verification = 0.0
        for event in events:
            try:
                detail = json.loads(event["detail"])
            except (ValueError, TypeError):
                continue
            if event["kind"] == "wave.done" and "seconds" in detail:
                waves.append({"tasks": detail.get("tasks", []),
                              "seconds": detail.get("seconds")})
            elif event["kind"] in ("worker.merged", "worker.rejected"):
                task = detail.get("task")
                if task:
                    workers.setdefault(task, 0.0)
            elif event["kind"] == "verification.timed":
                verification += float(detail.get("seconds") or 0)
        for row in store.workers_for(job):
            try:
                seconds = json.loads(row["detail"] or "{}").get("seconds")
            except (ValueError, TypeError):
                seconds = None
            if seconds is not None:
                workers[row["task_id"]] = seconds
        total = round(time.monotonic() - started, 1)
        store.event(project, job, "job.timing", {
            "total_seconds": total,
            "budget_seconds": budget,
            "used_fraction": round(total / budget, 3) if budget else None,
            "waves": waves,
            "worker_seconds": workers,
            "verification_seconds": round(verification, 1),
        })

    def _job_budget(self, project, job_id, store) -> int:
        """Wall-clock budget for this job, scaled by the plan when one exists."""
        plan = None
        if project.get("plan"):
            try:
                plan = Plan.model_validate_json(project["plan"])
            except ValueError:
                plan = None
        profile = self.settings.profiles.get(project["profile"])
        return self.settings.job_deadline_seconds(plan, profile)

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
            "Use CSS selectors and relative paths only. Do not weaken expected behavior. "
            "Assert only facts the contract actually states: exact copy, counts and status codes that "
            "appear in `flows`, `tasks` or `brief`. Never invent expected text (for example treating "
            "the product's own name as the value of an unrelated field). "
            "For browser cases use ONLY the CSS selectors in each flow's `selectors` map; do not "
            "compose new selectors or guess names the plan does not declare. If a state you need has "
            "no declared selector, assert what is declared instead of inventing one. "
            "For repeated items use `expect_count` with the item selector, and avoid `:nth-of-type`, "
            "which depends on sibling tag names and silently matches nothing when items are wrapped. "
            "To check what a form field contains, use `expect_value` with the exact value; an "
            "`<input>` has no text content, so `expect_text` cannot observe or verify a field and "
            "`expect_text` with an empty value asserts nothing and is rejected.")
        flows = [f.model_dump() for f in plan.test_flows]
        # The tester must see the same approved contract the workers implemented. Sending only
        # the flow list let it assert copy the operator never approved and the workers never
        # wrote, which failed a correct build.
        tasks = [{"id": t.id, "title": t.title, "instructions": t.instructions,
                  "acceptance": t.acceptance} for t in plan.tasks]
        suite = None
        for attempt in range(self.settings.max_repairs + 1):
            if cancelled():
                raise Cancelled("Cancelled while preparing tests")
            context = {"brief": project["brief"], "flows": flows, "tasks": tasks,
                       "plan_summary": plan.summary, "assumptions": plan.assumptions}
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

    def _verify_with_repair(self, project, job, profile, plan, root, work, source, suite,
                            cancelled):
        """Run the gates, and repair the application when they fail.

        Phase 0 proved a build can run to a published artifact. It also proved the most likely
        way a run dies: the gate is right and the code is wrong, or the gate is right and the
        *plan* contradicts itself. Either way the job simply died, leaving the operator a
        report and a screenshot to read by hand. This loop turns that into a bounded repair.

        The repair worker is a normal Pi harness worker on the product repository's target
        branch, because the failure is in the merged code, not in one task's worktree. Its
        instruction is built from the tester's own report -- the failing case, the assertion
        message, the screenshot path -- so it can act instead of guessing.

        Bounded by ``max_repairs``, and exhaustion fails the job loudly. A partially working
        application is never published as if it were verified.
        """
        store = self.store
        output = work / "output"
        evidence_dir = work / "testing"
        for attempt in range(self.settings.max_repairs + 1):
            if cancelled():
                raise Cancelled("Cancelled before verification")
            verify_started = time.monotonic()
            store.event(project["id"], job["id"], "verification.running", {"attempt": attempt + 1})
            if evidence_dir.exists():
                # Stale evidence from the previous attempt would be read as the current
                # failure and send the repair worker after a problem it already fixed.
                shutil.rmtree(evidence_dir)
            if output.exists():
                shutil.rmtree(output)

            def test_event(kind, detail):
                store.event(project["id"], job["id"], kind, detail)

            try:
                if suite:
                    log = self.verifier.verify(profile, source, output, job["id"], cancelled,
                                               suite=suite, evidence=evidence_dir,
                                               on_event=test_event)
                else:
                    log = self.verifier.verify(profile, source, output, job["id"], cancelled)
            except Cancelled:
                raise
            except Exception as exc:
                evidence = repair_module.describe_evidence(work)
                evidence["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
                atomic_json(work / f"repair-{attempt}-evidence.json", evidence)
                store.event(project["id"], job["id"], "repair.evidence",
                            {"attempt": attempt + 1,
                             "failures": [f["flow_id"] for f in evidence["failures"]],
                             "error": evidence["error"]})
                if attempt >= self.settings.max_repairs:
                    (work / "verification.txt").write_text(
                        f"Verification failed after {attempt + 1} attempt(s). "
                        f"Last error: {evidence['error']}")
                    raise ValueError(
                        "Verification failed after bounded repair attempts; "
                        "inspect repair evidence before retrying") from exc
                self._repair_then_retry(project, job, root, work, evidence, attempt, cancelled)
                store.event(project["id"], job["id"], "repair.retrying", {"attempt": attempt + 1})
                continue

            (work / "verification.txt").write_text(log)
            store.event(project["id"], job["id"], "verification.timed",
                        {"attempt": attempt + 1,
                         "seconds": round(time.monotonic() - verify_started, 1)})
            return log
        raise ValueError("Verification loop ended without a result")

    def _repair_then_retry(self, project, job, root, work, evidence, attempt, cancelled):
        """Run one repair worker on the product branch and re-export the source to verify."""
        store = self.store
        products.checkout_target(root)
        instruction = repair_module.repair_instruction(evidence)
        (work / f"repair-{attempt}-instruction.txt").write_text(instruction)
        store.event(project["id"], job["id"], "repair.running", {"attempt": attempt + 1})
        before = products.head_commit(root)
        transcript = self.parallel.worker.run_task(root, instruction, cancelled=cancelled)
        (work / f"repair-{attempt}-transcript.txt").write_text(transcript[-100000:])
        commit = products.commit_all(root, f"Repair failing gate (attempt {attempt + 1})")
        # Compare against the commit from *before* this repair. `commit_all` returns HEAD
        # unchanged when the worktree was clean, and diffing HEAD~1 would then report the
        # previous attempt's changes as this one's.
        changed = products.changed_files(root, before) if commit != before else []
        store.event(project["id"], job["id"], "repair.committed",
                    {"attempt": attempt + 1, "commit": commit[:12], "changed": changed[:20]})
        if not changed:
            # A repair that changed nothing will fail identically; say so instead of
            # burning the remaining attempts on the same evidence.
            raise ValueError("Repair worker produced no change; the gate cannot pass unchanged")
        # Re-export from the repaired branch so the next attempt verifies the new code.
        products.export_tree(root, work / "source")

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

        # Measured once per build: the exact runtime the verifier will use.
        from .local_app import runtime_facts
        toolchain = runtime_facts()

        def instructions_for(task, base_context):
            # The worker reads the real files itself, so it gets the task contract and the
            # product brief rather than a full source dump.
            return (
                WORKER_BRIEF
                + "\n\nPRODUCT BRIEF:\n" + project["brief"]
                + "\n\nPLAN SUMMARY:\n" + plan.summary
                + (("\n\nREQUIRED SELECTORS (the independent tester asserts these exact CSS\n"
                    "selectors; every one that falls inside your task must exist in the rendered DOM):\n"
                    + "\n".join(f"- {name}: {selector}"
                                for flow in plan.test_flows
                                for name, selector in flow.selectors.items()))
                   if any(f.selectors for f in plan.test_flows) else "")
                + ("\n\nRUNTIME ON THIS MACHINE (measured, use these when declaring engine or\n"
                   "dependency constraints; do not invent version ranges):\n" + toolchain
                   if toolchain else "")
                + ("\n\nPERSISTENCE CONTRACT:\n"
                   "- Store data in a local SQLite file unless the brief names an external database.\n"
                   "- Read the database location from the DATABASE_URL environment variable (a\n"
                   "  file: URL) and the directory from DATA_DIR. Never hardcode a path.\n"
                   "- Do not commit the database file: it is created at runtime inside DATA_DIR.\n"
                   "  Add its filename to .gitignore.\n"
                   "- The data must survive a process restart: do not hold records only in memory."
                   if profile.env else "")
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
                                    "missing file. Judge only what the diff actually shows.\n\n"
                                    "If the diff contains interface code (CSS, a template, a component), "
                                    "also review it as design, using these as defects rather than "
                                    "preferences:\n"
                                    "- Motion with no stated purpose, or `transition: all`.\n"
                                    "- `ease-in` or a bare duration with no timing function on an "
                                    "entrance or hover; decelerating motion reads as responsive.\n"
                                    "- `outline: none` without a `:focus-visible` replacement; this "
                                    "removes the keyboard user's position indicator.\n"
                                    "- Colour alone carrying state, with no text or shape signal.\n"
                                    "- A solid border where a soft shadow would read as depth, and "
                                    "inputs left at browser default styling.\n"
                                    "- Fixed pixel widths that break at 360px, or body text smaller "
                                    "than 14px.\n"
                                    "- `user-select: none` on content a reader may want to copy.\n"
                                    "Reject a change that has these; do not reject one merely because a "
                                    "layout differs from how you would have drawn it. You cannot run a "
                                    "browser, so judge what the CSS and markup will do, not how it looks.",
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

        suite = tester_future.result() if tester_future else None
        self._verify_with_repair(project, job, profile, plan, root, work, source,
                                 suite, cancelled)
        if cancelled():
            raise Cancelled("Cancelled before publishing artifact")
        # The artifact is the product's codebase, not the build output: tracked source, a
        # README describing how to run it, and a fresh single-commit git history. Build
        # output is excluded because `npm ci && npm run build` regenerates it from the
        # locked manifest, and shipping it would be large and silently stale.
        published = work / "published"
        artifact_meta = artifact_module.assemble(
            project, profile, plan, source, published,
            commit_message=f"{project['name']}: verified build, revision {project['revision']}")
        artifact = package(published, work / "artifact.tar.gz")
        artifact.update({"job_id": job["id"], "revision": project["revision"],
                         "kind": "source", **artifact_meta})
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
