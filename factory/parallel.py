"""Parallel Pi workers with a merge barrier.

The planner's plan is a DAG. Workers whose dependencies are satisfied run at the same
time, each in its own git worktree on its own branch. When every worker in the current
wave has finished, the barrier opens and accepted branches are merged into the product
branch one at a time. A conflicting merge aborts, and a fixer worker is spawned against
the target branch to resolve it before the wave is retried.

Why a barrier rather than merging as each worker finishes: two workers may touch the
same file, and merging mid-flight would mean a worker finishes against a base that no
longer exists. Merging only after the whole wave is done keeps every worker's premise
stable and makes the order of merges explicit instead of racy.
"""
import json
import time
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor, wait

from . import products
from .agents import PiWorker, PiWorkerError, usage_from_transcript
from .process import Cancelled


class WorkerBudgetExhausted(ValueError):
    """The job cannot pay for another harness-worker run under `FACTORY_MAX_JOB_COST`."""


WORKER_BRIEF = (
    "You are a coding worker in a software factory. You own exactly one task and you work "
    "only inside the current directory, which is your own git worktree.\n"
    "Use the tools available to you: read the existing files before changing them, run "
    "commands to check your work, and edit files directly. Do not edit anything outside "
    "this directory.\n"
    "When you are done, leave the worktree in a state that builds and satisfies the task "
    "acceptance criteria. Do not run git commands; the factory commits for you."
)


class WorkerOutcome:
    __slots__ = ("task_id", "state", "branch", "worktree", "commit", "base", "detail")

    def __init__(self, task_id, state, branch, worktree=None, commit="", base="", detail=None):
        self.task_id = task_id
        self.state = state
        self.branch = branch
        self.worktree = worktree
        self.commit = commit
        self.base = base
        self.detail = detail or {}

    def as_dict(self):
        return {"task": self.task_id, "state": self.state, "branch": self.branch,
                "commit": self.commit, "detail": self.detail}


def _failure_note(outcome):
    """One line for a retry that failed without a review verdict.

    The next attempt reads this in `PREVIOUS ATTEMPT FEEDBACK`, so it has to say what went
    wrong and what to do differently, not just restate the error class. A deadline is the case
    that motivated it: the worker had been iterating on a design, and the useful instruction is
    to keep the result it already reached instead of exploring again from nothing.
    """
    detail = outcome.detail or {}
    error = str(detail.get("error") or "Unknown failure")
    message = " ".join(str(detail.get("message") or "").split())
    note = f"The previous attempt did not produce a reviewable result ({error}"
    note += f": {message[:300]}" if message else ""
    note += "). "
    if "deadline" in message.lower() or "timeout" in message.lower():
        note += ("That attempt ran out of wall-clock time while still working. Write the "
                 "complete files first, then verify. Do not repeat exploratory passes you "
                 "have already completed; a build-and-look cycle you already ran does not "
                 "need to run again.")
    else:
        note += ("Fix the cause before changing anything else, and leave the worktree in a "
                 "state that can be reviewed on its own.")
    return note


class ParallelBuilder:
    """Drives waves of Pi workers and merges each finished wave into the product branch."""

    def __init__(self, settings, store, worker=None):
        self.settings = settings
        self.store = store
        self.worker = worker or PiWorker(settings)
        self.lock = threading.Lock()

    # -- public entry point ------------------------------------------------------------

    def build_tasks(self, project, job, plan, instructions_for, cancelled, review_for=None):
        """Run every task in dependency waves and return the list of merged outcomes.

        ``instructions_for(task, base_context)`` produces the worker prompt.
        ``review_for(task, worktree, diff, base)`` optionally returns a review decision; a
        rejected review is retried up to ``max_repairs`` and otherwise fails the wave.
        """
        store = self.store
        root = products.ensure_repo(self.settings.data, project["id"])
        products.checkout_target(root)
        outcomes: list[WorkerOutcome] = []
        # Resume from work that is genuinely on the branch. A retry after a restart gets a
        # new job id, so asking this job for its completed tasks always answered "none" and
        # every merged task was redone -- a 15-minute build restarted from zero. The evidence
        # must satisfy both conditions: a worker row recorded as merged for this plan
        # revision, *and* its commit still reachable from the branch, because a reset or a
        # rebase can invalidate the row.
        completed: set[str] = store.tasks_merged_for_project(
            project["id"], project["revision"], products.ancestors(root))
        plan_ids = {task.id for task in plan.tasks}
        completed &= plan_ids
        attempts: dict[str, int] = {}
        # What the reviewer said when it rejected a task, carried into the next attempt.
        # Without this the retry runs with a prompt identical to the one that just failed:
        # the worker repeats the same mistake and the reviewer re-reads the same diff.
        # Observed live: a task was rejected twice for an unrequested dependency, the worker
        # was never told, and the third review approved the unchanged problem.
        findings: dict[str, list[str]] = {}

        if completed:
            store.event(project["id"], job["id"], "build.resumed",
                        {"completed": sorted(completed),
                         "remaining": sorted(plan_ids - completed)})

        while True:
            if cancelled():
                raise Cancelled("Cancelled between worker waves")
            ready = plan.ready_set(completed)
            if not ready:
                break
            wave_started = time.monotonic()
            store.event(project["id"], job["id"], "wave.start",
                        {"tasks": [task.id for task in ready], "completed": sorted(completed)})
            wave = self._run_wave(project, job, root, ready, instructions_for, cancelled,
                                  review_for, attempts, findings)
            accepted = self._merge_wave(project, job, root, wave, cancelled)
            for outcome in wave:
                if outcome.task_id in accepted:
                    completed.add(outcome.task_id)
            outcomes.extend(wave)

            failed = [o for o in wave if o.task_id not in accepted]
            if failed:
                attempts_used = {o.task_id: attempts.get(o.task_id, 0) for o in failed}
                if all(attempts_used[o.task_id] >= self.settings.max_repairs for o in failed):
                    raise ValueError("Worker wave failed after bounded repair attempts: "
                                     + ", ".join(sorted(o.task_id for o in failed)))
                for outcome in failed:
                    attempts[outcome.task_id] = attempts.get(outcome.task_id, 0) + 1
                    reported = (outcome.detail or {}).get("review", {}).get("findings") or []
                    if reported:
                        # A reviewer's finding is specific and actionable, so it wins over
                        # the generic line below.
                        findings[outcome.task_id] = list(reported)[:10]
                    else:
                        # A failure that was not a review -- a deadline, a crash, an OSError --
                        # used to reach the next attempt as an empty list, so the worker
                        # restarted with a prompt identical to the one that just failed. That
                        # is the same defect the review path already fixed, in the one branch
                        # nobody had written. Measured on the Toko Roti rebuild: attempt 1 hit
                        # the deadline, attempt 2 began blind and re-explored the whole design,
                        # and the reviewer then rejected it for work attempt 1 had not finished.
                        findings[outcome.task_id] = [_failure_note(outcome)]
                store.event(project["id"], job["id"], "wave.retry",
                            {"tasks": sorted(o.task_id for o in failed),
                             "feedback": {o.task_id: findings.get(o.task_id, [])
                                          for o in failed}})

            # Duration is recorded because a flat job timeout cannot be sized without it:
            # the operator needs to know whether a 15-minute build spent its time in workers,
            # in review, or in the merge barrier before deciding what to raise.
            store.event(project["id"], job["id"], "wave.done",
                        {"merged": sorted(accepted), "completed": sorted(completed),
                         "seconds": round(time.monotonic() - wave_started, 1),
                         "tasks": sorted(o.task_id for o in wave)})

        missing = {task.id for task in plan.tasks} - completed
        if missing:
            raise ValueError("Tasks never completed: " + ", ".join(sorted(missing)))
        return outcomes

    # -- one wave ----------------------------------------------------------------------

    def _run_wave(self, project, job, root, tasks, instructions_for, cancelled, review_for,
                  attempts, findings=None):
        concurrency = max(1, min(self.settings.worker_concurrency, len(tasks)))
        base = products.head_commit(root)
        results: dict[str, WorkerOutcome] = {}

        def run(task):
            return self._run_one(project, job, root, task, base, instructions_for, cancelled,
                                 review_for, attempts.get(task.id, 0),
                                 (findings or {}).get(task.id, []))

        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="factory-worker") as pool:
            futures = {pool.submit(run, task): task for task in tasks}
            wait(futures)
            for future, task in futures.items():
                try:
                    results[task.id] = future.result()
                except Cancelled:
                    raise
                except BaseException as exc:
                    # A worker failure must not cancel its siblings; the barrier still runs.
                    detail = {"error": type(exc).__name__}
                    output = getattr(exc, "output", "")
                    if output:
                        detail["output"] = str(output)[-4000:]
                    results[task.id] = WorkerOutcome(task.id, "failed", self._branch(task.id),
                                                     base=base, detail=detail)
        return [results[task.id] for task in tasks if task.id in results]

    def _run_one(self, project, job, root, task, base, instructions_for, cancelled, review_for,
                 prior_attempts, prior_findings=None):
        store = self.store
        branch = self._branch(task.id)
        record = self.record_for(job["id"], task.id)
        if record is None:
            record = store.create_worker(project["id"], job["id"], task.id, branch, base,
                                         depends_on=task.depends_on,
                                         revision=project["revision"])
        worker_id = record["id"]

        # Meter the harness worker before any model work starts. Engine.generate reserves
        # per structured call; a worker run is the one model spend that was still
        # admission-only, and it is often the most expensive stage of a build. The
        # reservation is transactional (BEGIN IMMEDIATE), so the concurrent threads of a
        # wave serialize and the job can never be pushed past its ceiling. A failure here
        # is terminal for the attempt: the worker row is marked failed so the dashboard
        # never shows a "running" worker that will never run, and the event names the
        # knob the operator has to raise.
        try:
            self._reserve_worker_cost(project, job, task.id)
        except WorkerBudgetExhausted as exc:
            detail = {"error": type(exc).__name__, "message": str(exc)[:3000]}
            store.set_worker(worker_id, state="failed", detail=detail)
            store.event(project["id"], job["id"], "worker.budget_exhausted",
                        {"task": task.id, "worker": worker_id, "message": str(exc)[:500]})
            return WorkerOutcome(task.id, "failed", branch, base=base, detail=detail)

        # A retry starts from a clean branch so a failed attempt cannot poison the next one.
        # A stale worktree from an earlier job still holds the branch, so remove any
        # worktree attached to this branch before deleting it; otherwise git refuses to
        # reuse the branch name and the whole task fails.
        worktree = self.settings.data / "workers" / job["id"] / task.id
        for stale in products.list_worktrees(root):
            if stale.get("branch") == branch and stale.get("path"):
                products.worktree_remove(root, Path(stale["path"]))
        if worktree.exists():
            products.worktree_remove(root, worktree)
        products.branch_delete(root, branch)

        store.set_worker(worker_id, state="running", started=__import__("time").time(),
                         attempts=prior_attempts + 1, worktree=worktree, commit_sha="")
        store.event(project["id"], job["id"], "worker.running", {"task": task.id, "worker": worker_id})

        try:
            products.worktree_add(root, worktree, branch)
            instruction = instructions_for(task, {"brief": project["brief"], "base": base,
                                                  "attempt": prior_attempts + 1,
                                                  "feedback": "\n".join(prior_findings or [])})
            worker_started = time.monotonic()
            # A task that carries a skill is doing design work, which is a loop of build,
            # look, adjust. The flat bound cut that loop off mid-iteration and the worker
            # then restarted blind; a task with no skill has no such loop and keeps the
            # tighter clock. `task.skills` is the signal because declaring a skill is exactly
            # what makes the task visual.
            timeout = (self.settings.skill_worker_timeout if task.skills
                       else self.settings.worker_timeout)
            transcript = self.worker.run_task(worktree, instruction,
                                             timeout=timeout,
                                             cancelled=cancelled,
                                             skills=task.skills)
            worker_seconds = round(time.monotonic() - worker_started, 1)
            commit = products.commit_all(worktree, f"{task.title} ({task.id})")
            changed = products.changed_files(worktree, base)
            detail = {"changed": changed, "transcript": transcript[-4000:],
                      "seconds": worker_seconds}
            # Defensive metering: if the Pi JSON stream carried usage numbers, record
            # them. `usage_from_transcript` returns {} when the transcript has none and
            # never raises, so an unparsable transcript cannot fail a worker that ran.
            usage = usage_from_transcript(transcript)
            if usage:
                detail["usage"] = usage
                store.event(project["id"], job["id"], "worker.usage",
                            {"task": task.id, "worker": worker_id, **usage})

            if review_for and changed:
                diff = products.diff_against(worktree, base)
                decision = review_for(task, worktree, diff, base)
                detail["review"] = decision
                if not decision.get("approved"):
                    store.set_worker(worker_id, state="rejected", detail=detail)
                    store.event(project["id"], job["id"], "worker.rejected",
                                {"task": task.id, "findings": decision.get("findings", [])[:10]})
                    return WorkerOutcome(task.id, "rejected", branch, worktree, commit, base, detail)
                if prior_findings and prior_attempts:
                    # The same reviewer rejected this task on an earlier attempt and now
                    # approves it. That is legitimate -- the worker may have fixed the
                    # problem -- but it is also how a real "approve because it is the third
                    # time" slipped through: a task rejected twice for an unrequested
                    # dependency merged with that dependency still in package.json. The
                    # verdict stands, and the disagreement is recorded so the operator can
                    # see it without reading two review files side by side.
                    detail["flaky_approval"] = True
                    detail["prior_findings"] = list(prior_findings)[:10]
                    store.event(project["id"], job["id"], "review.flaky_approval",
                                {"task": task.id, "attempt": prior_attempts + 1,
                                 "prior_findings": list(prior_findings)[:10],
                                 "note": "approved after an earlier rejection; confirm the "
                                         "findings were actually resolved"})

            store.set_worker(worker_id, state="awaiting_merge", commit_sha=commit, detail=detail)
            store.event(project["id"], job["id"], "worker.finished",
                        {"task": task.id, "worker": worker_id, "changed": changed})
            return WorkerOutcome(task.id, "awaiting_merge", branch, worktree, commit, base, detail)
        except Cancelled:
            store.set_worker(worker_id, state="cancelled")
            raise
        except (PiWorkerError, ValueError, OSError, RuntimeError) as exc:
            detail = {"error": type(exc).__name__, "message": str(exc)[:3000]}
            output = getattr(exc, "output", "")
            if output:
                detail["output"] = str(output)[-4000:]
            store.set_worker(worker_id, state="failed", detail=detail)
            store.event(project["id"], job["id"], "worker.failed", {"task": task.id, "error": type(exc).__name__})
            return WorkerOutcome(task.id, "failed", branch, worktree, "", base, detail)

    # -- barrier -----------------------------------------------------------------------

    def _merge_wave(self, project, job, root, wave, cancelled):
        """Merge accepted branches one at a time. A conflict spawns a fixer and retries."""
        store = self.store
        accepted: set[str] = set()
        pending = [o for o in wave if o.state == "awaiting_merge"]
        for outcome in pending:
            if cancelled():
                raise Cancelled("Cancelled before merge")
            record = self.record_for(job["id"], outcome.task_id)
            try:
                products.checkout_target(root)
                products.merge_branch(root, outcome.branch, f"Merge {outcome.task_id}")
            except products.GitError as exc:
                store.event(project["id"], job["id"], "merge.conflict",
                            {"task": outcome.task_id, "detail": str(exc)[:1000]})
                if record:
                    store.set_worker(record["id"], state="conflict",
                                     detail={**json.loads(record["detail"] or "{}"), "conflict": str(exc)[:1000]})
                resolved = self._spawn_fixer(project, job, root, outcome, cancelled)
                if not resolved:
                    continue
            if record:
                head = products.head_commit(root)
                store.set_worker(record["id"], state="merged",
                                 finished=__import__("time").time(), commit_sha=head)
            store.event(project["id"], job["id"], "worker.merged", {"task": outcome.task_id})
            accepted.add(outcome.task_id)
            # The branch is merged, so the worktree is dead weight: it holds a full
            # `node_modules` and nothing reads it again. Leaving it behind leaked 2.4GB across
            # two builds before anyone noticed, because removal only happened on a retry.
            # Failure to clean is logged rather than raised: the merge already succeeded, and
            # a leftover directory must not fail a build.
            try:
                products.worktree_remove(root, outcome.worktree)
            except (products.GitError, OSError) as exc:
                store.event(project["id"], job["id"], "worktree.cleanup_failed",
                            {"task": outcome.task_id, "detail": str(exc)[:500]})
        return accepted

    def _spawn_fixer(self, project, job, root, outcome, cancelled):
        """A conflicting merge is resolved by a dedicated worker on the target branch."""
        store = self.store
        if cancelled():
            raise Cancelled("Cancelled before conflict resolution")
        store.event(project["id"], job["id"], "fixer.spawning", {"task": outcome.task_id})
        fixer_wt = self.settings.data / "workers" / job["id"] / (outcome.task_id + "-fixer")
        if fixer_wt.exists():
            products.worktree_remove(root, fixer_wt)
        instruction = (
            f"Integration conflict while merging task '{outcome.task_id}'.\n"
            f"Branch '{outcome.branch}' (commit {outcome.commit[:12]}) conflicts with the product "
            "branch in this directory.\n"
            "Your job: integrate that branch's intent by hand so both changes survive.\n"
            f"Run `git merge --no-commit --no-ff {outcome.branch}` to reintroduce the conflict, "
            "resolve every conflicted file, then leave the merged result in the working tree. "
            "Do not run `git commit` or `git merge --continue`; the factory commits for you."
        )
        try:
            products.checkout_target(root)
            # The fixer works on a detached worktree of the target so the resolved result is
            # the target itself, not another branch that would need a second reconciliation.
            products.detached_worktree(root, fixer_wt, "main")
            # Same budget as any other worker: resolving a conflict means editing code and
            # re-running the build. Without `timeout` this inherited the 600s agent budget.
            self.worker.run_task(fixer_wt, instruction, cancelled=cancelled,
                                 timeout=self.settings.worker_timeout)
            if products.has_conflicts(fixer_wt):
                raise ValueError("Fixer left unresolved conflicts")
            fixer_commit = products.commit_all(fixer_wt, f"Resolve merge conflict for {outcome.task_id}")
            if fixer_commit == products.head_commit(root):
                raise ValueError("Fixer produced no change")
            products.fast_forward(root, fixer_commit)
            store.event(project["id"], job["id"], "fixer.resolved",
                        {"task": outcome.task_id, "commit": fixer_commit})
            return True
        except (products.GitError, PiWorkerError, ValueError, OSError, RuntimeError) as exc:
            store.event(project["id"], job["id"], "fixer.failed",
                        {"task": outcome.task_id, "error": f"{type(exc).__name__}: {str(exc)[:500]}"})
            return False
        finally:
            products.worktree_remove(root, fixer_wt)

    # -- helpers -----------------------------------------------------------------------

    def _reserve_worker_cost(self, project, job, task_id):
        """Reserve one harness-worker run against the job's cost ceiling.

        Mirrors `Engine.generate`: read `jobs.reserved_usd` and add the reservation inside
        the same `BEGIN IMMEDIATE` transaction, so the concurrent worker threads of a wave
        serialize on SQLite and can never push the job past `settings.max_job_cost`. The
        SQL lives here rather than in `factory/db.py` because metering policy is the
        caller's decision, not a store primitive. `settings.worker_cost == 0` opts out.
        """
        cost = self.settings.worker_cost
        if cost <= 0:
            return
        with self.store.tx() as db:
            row = db.execute("SELECT reserved_usd FROM jobs WHERE id=?", (job["id"],)).fetchone()
            if row is None:
                # No job row to meter against. Real jobs always have one (Store.enqueue);
                # only synthetic job dicts in tests bypass the store. Skipping rather than
                # crashing keeps that path working unchanged.
                return
            if row[0] + cost > self.settings.max_job_cost:
                raise WorkerBudgetExhausted(
                    f"Job's reserved worker budget exhausted: one harness worker run "
                    f"reserves ${cost:.2f} and ${row[0]:.2f} of the "
                    f"${self.settings.max_job_cost:.2f} FACTORY_MAX_JOB_COST is already "
                    f"spoken for. Raise FACTORY_MAX_JOB_COST, or set FACTORY_WORKER_COST=0 "
                    f"to stop metering workers (task '{task_id}').")
            db.execute("UPDATE jobs SET reserved_usd=reserved_usd+? WHERE id=?", (cost, job["id"]))

    def _branch(self, task_id):
        return f"worker/{task_id}"

    def record_for(self, job_id, task_id):
        for row in self.store.workers_for(job_id):
            if row["task_id"] == task_id:
                return row
        return None
