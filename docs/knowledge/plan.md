# Delivery plan

Written 2026-11-08 after reviewing the goal, the code and the recorded evidence. This is the
working plan; [handoff](handoff.md) records what has actually been executed and
[origin and decisions](origin-and-decisions.md) records why the architecture is what it is.

## The goal being served

The owner is starting a software company from zero. The factory must turn a written brief into
an application that is **built, reviewed, tested and defensible**, for different clients, with
the planner assigning work to workers and the dashboard showing that work. Reliability,
reviewability and evidence outrank volume of code. Deployment is out of scope until reopened.

## Where the project actually stands

The components are individually proven with live model calls and real builds:

| Stage | Status | Evidence |
| --- | --- | --- |
| Brief → plan as a real DAG | works | Live planner produced DAGs and refused to invent copy |
| Plan → parallel Pi workers → merge barrier | works | Two workers started the same second and merged; a fixer resolved a rename/rename conflict |
| Merge → build → secret scan → API/browser acceptance | works | Kayu Jati 4/4 cases; bike-fleet CRUD 11/11 cases |
| Persistence across a restart | works | A record was read back after a full process restart |
| Dashboard shows waves and workers | works | 6 waves, 5 workers, 1 repair, tester and verifier passed |

**The finding that dominated everything, now closed.** Across every run before 2026-11-09:

```
build jobs : failed, failed, failed, interrupted, interrupted, interrupted
verify jobs: succeeded   ← every published artifact came from a separately enqueued verify
```

A `build` job is the one that runs the workers *and* publishes. It had never once succeeded
end-to-end. **Phase 0 closed on 2026-11-09** — see below. The parts work, and now the factory
works as one operation.

## Gap analysis

Ordered by what blocks the stated goal, not by how interesting it is.

1. ~~**One job has never closed the loop.**~~ **Closed 2026-11-09.** A single `build` job ran a
   complete brief to a published artifact in 15.3 minutes: planner (no questions asked, because
   the brief was complete), a four-task DAG, four Pi workers (two of them concurrent in wave 2),
   merge barrier, `npm ci` + `npm run build`, and three acceptance cases — two real browser
   cases with `console_errors: []` — all passed. `build` row `succeeded`, project `ready`,
   artifact `kind: source`, 12 files, 262 KB. The artifact was then extracted and built
   independently of the factory: `npm ci` and `npm run build` both exited 0, `/api/health`
   returned `{"status":"ok"}`, and the home page served the shop name and both menu items.
   **What made the difference** was not the retry wiring: every earlier attempt died at the
   tester gate because the approved plan contradicted itself — the operator's answers to the
   planner's questions were appended to the task instructions but never to `test_flows[].steps`,
   so the worker built the corrected copy and the tester asserted the planner's original
   placeholder. A complete brief avoids that path.
2. **No repair loop for failing gates.** `max_repairs` applies only to a worker *review*
   rejection. When verification or the tester fails, `verify()` raises and the job dies. The
   historical build failures were tester-side defects since fixed, but the architecture still
   cannot take "tests failed" and turn it into "a worker fixes it".
3. **Builds do not resume.** `ParallelBuilder.build_tasks()` starts with `completed = set()`, so
   an interrupted job re-runs every merged task. `Store.completed_tasks()` and
   `Store.worker_states()` exist and are unused — the resume path was written but never wired.
4. **The planner duplicates the verifier.** Live plans include an `integration-verification`
   task asking a worker to run `npm ci`, build, start the app and smoke every endpoint — work
   the verifier already does after the merge barrier. It wastes a worker and risks timeouts.
5. **Timeout semantics are wrong for build-sized tasks.** Default `FACTORY_AGENT_TIMEOUT` is
   600s, while a task is instructed to do a clean-room `npm ci` plus build plus smoke tests.
6. **The tester cannot authenticate.** `TestAction` has no `headers` field, so any application
   with a login cannot be tested. Cookies do persist within a case, so session flows partly work.
7. **No lint or typecheck.** The active profile runs `npm ci` and `npm run build` only. An app
   can ship that builds but does not typecheck.
8. **No client attribution or product hand-off.** Products are separate git repositories, but
   there is no `client_id`, no archive and no export path to give a client their code.
9. **Windows is unfinished.** `fcntl`, `killpg` and POSIX process groups appear in four modules.
   Windows was requested; do not claim it works.
10. **Documentation has drifted.** The handoff still says the dashboard does not render waves
    (it now does), quotes several different test counts, and has duplicate list numbering. A
    stale handoff actively misleads the next agent.
    **Fixed 2026-11-09:** the outstanding drift on status, dashboard state, test counts,
    tester contract, product data layout and list numbering was corrected across the docs.
    Where documents disagree, the handoff's dated evidence is authoritative; re-run the
    checks rather than trusting a quoted number.
11. ~~**The product data directory sits inside the product repository.**~~ **Fixed 2026-11-09.**
    `local_app.app_data_dir()` returned `data/products/<id>/data`, *inside* the product's git
    working tree, while `docs/operations.md` and its own docstring claimed it sat beside it.
    A worker running `git add -A` could commit the live SQLite file; publication stayed safe
    only because `export_tree()` copies tracked files and the verifier builds from that
    export. The directory now lives at `data/product-data/<id>`, outside the repository, and
    `factory/cli.py` calls `app_data_dir()` instead of recomputing the path. The old test was
    a tautology (it asserted `".git"` was absent from the path parts, not that the path was
    outside the tree); it now asserts `not path.is_relative_to(repo)` and that writing the
    database leaves the repository clean. Confirmed to fail when the fix is reverted.
12. ~~**Acceptance cases are only isolated when the profile declares `{data}`.**~~ **Fixed
    2026-11-09.** Isolation is now unconditional, and each case also gets its own copy of the
    build tree, because a fresh process alone still let an application that persists beside
    itself leak state into the next case. The copy uses `clone_build_tree()`, which preserves
    symlinks: `node_modules/.bin` is entirely symlinks, and the strict source walk would have
    failed every real Node verification at that step.

## Phases

### Phase 0 — Close the loop (P0, the milestone) — **DONE 2026-11-09**

One `build` job from approval to published artifact, with no manual step.

All four exit criteria were met on a live run: `build` `succeeded`, project `ready`, artifact
digest `f4681a3a…`, all four `workers` rows `merged`, 15.3 minutes, and the extracted artifact
built and served correctly outside the factory.

### Phase 0 findings (recorded as they were found)

- **F1 — the planner has no retry, the tester has three.** *(Fixed.)* `Engine.prepare_tests()`
  retried `max_repairs + 1` times on a structurally invalid response; the planner called
  `generate(...)` once and a single malformed generation failed the whole job. The planner now
  has the same bounded retry and writes each failed attempt to `plan-attempt-<n>.txt`.
- **F2 — selector labels were rejected without ever being specified.** *(Fixed.)* The planner
  was told to fill a `selectors` map but never told the naming rule, and the validator required
  lowercase `[a-z][a-z0-9_-]*`. Two live planner jobs died on `shopName` / `heroName`.
  `camelCase` is now normalised to `snake_case`, and the prompt states the rule.
- **F3 — the gate was right and the plan was self-contradictory.** The last failing run before
  Phase 0 closed died with two browser cases passing except for one hero paragraph. The
  application was correct: the worker had implemented the operator's answer. The tester
  asserted the planner's original placeholder because `test_flows[].steps` still held it. The
  operator's answers were appended to `task.instructions` and to `assumptions`, but nothing
  rewrote the test contract. **Nothing in the code prevents this**, and it is the most likely
  way a real run dies: answering a planner question by editing one field and not the others.
  A complete brief avoids it, which is how Phase 0 was closed.

### Phase 1 — Repair loop (P1) — **DONE 2026-11-09**

5. On a verification or tester failure, persist structured evidence: the failing case, expected
   versus actual, and the screenshot path. **Done:** `factory/testing.py` records the assertion
   `message`, not just the exception class, and `factory/repair.py` extracts the failing cases
   into `repair-<n>-evidence.json`.
6. Spawn a repair worker on the product repository with that evidence, then re-verify. Bound it
   by `max_repairs` and fail loudly when exhausted — never silently ship a partial result.
   **Done:** `Engine._verify_with_repair` loops; the repair worker runs on the product branch
   because the failure is in merged code, and its brief quotes the failing selector, the
   expected text, the actual message and the screenshot, while forbidding any edit to the test
   suite or the checks.
7. **Done when:** deliberately breaking one acceptance assertion causes a repair and a
   republish, with an event trail showing attempt → evidence → fix. **Done:** covered by
   `tests/test_repair.py` (7 tests). A repair that changes nothing is stopped immediately
   instead of burning the remaining attempts on identical evidence, and exhaustion fails the
   job with no artifact.

### Phase 2 — Durability (P2) — **DONE 2026-11-09**

8. Wire `completed_tasks()` / `worker_states()` into `build_tasks()` so a restart resumes at a
   wave boundary instead of redoing merged work. **Done, differently than planned.** The
   existing helpers were the wrong shape: they key on `job_id`, and a retry creates a new
   job, so they always answered "none". `Store.tasks_merged_for_project` instead requires two
   facts — a worker row recorded as merged **for this plan revision**, and that commit **still
   reachable on the branch** (`products.ancestors(root)`). A row alone is unsafe after a
   reset or rebase; a commit alone cannot say which task it belonged to. A `build.resumed`
   event records what was skipped.
9. Add a **job-level** deadline, distinct from the per-worker `agent_timeout`. **Done:**
   `FACTORY_JOB_TIMEOUT`, default 7200s, enforced through the same cancellation path as an
   operator cancel.
10. **Done when:** a build killed mid-wave resumes after a scheduler restart without re-running
    tasks that already merged. **Done:** `tests/test_resume.py` covers a second job resuming,
    a discarded commit being redone, a removed task not counting, and a new plan revision
    rebuilding instead of resuming. The resume path is confirmed to fail when the seeding is
    removed.

**Note on the plan's own assumption.** `completed_tasks()` and `worker_states()` remain in
`db.py`, unused by the engine: they answer a per-job question that no caller needs. They are
kept because tests use them to inspect outcomes, not because they are the resume path.

### Phase 3 — Contract correctness (P3)

11. Tell the planner that verification belongs to the job, not to a worker task, and stop
    planning a task that duplicates the verifier.
12. Make the worker timeout appropriate for tasks that build, and document the relationship
    between `FACTORY_AGENT_TIMEOUT` and real build times.
13. **Done when:** a live plan contains no redundant verification task and a normal build does
    not time out.

### Phase 4 — Capability (P4, only when the owner prioritises it)

14. `headers` support in the tester, so an app with authentication is testable.
15. Lint and TypeScript typecheck in the validation profile. This needs the lockfile
    regenerated and re-validated, which is why it was deferred.
16. `client_id`, per-client archiving and a product export path, so a finished product can
    actually be handed to a client.

### Phase 5 — Deferred

17. Windows support: abstract `fcntl`/`killpg`, then real Windows integration and browser tests.
18. Deployment. Only when the owner reopens that scope. The presence of SSH code in `deploy/`
    is not evidence of a working deployment.

### Phase 6 — Documentation, alongside not after

19. Fix the handoff drift and record **why** each fix exists, not only that it exists. A
    decision without its reason gets reverted by the next agent.

## Sequencing rule

**Phase 0 is closed, so Phase 1 is now the next move.** The loop closes, which means the
remaining work is about making a run survive contact with reality rather than about wiring.

Phase 1 first, and specifically for F3: the most likely way a live run dies is not a broken
component but a plan that contradicts itself after an operator answers a planner question.
A repair loop that feeds the failing case, expected versus actual, and the screenshot back to
a worker is what turns that from a dead job into a corrected one. Phase 2 (resume) matters as
soon as runs are long — the Phase 0 run took 15 minutes, so a scheduler restart in the middle
still discards all of it. Phase 3 is cheap and saves a worker per build.

## Honesty rules for this plan

- Report a phase complete only with the executed evidence that its "done when" names.
- Distinguish scripted test doubles from live model calls, and real builds from stand-ins.
- Do not describe native execution as sandboxed, or this pre-production release as
  production-ready.
- When a fix touches an error-handling path, prove the regression by reverting the fix and
  watching the test fail. Several long-standing bugs lived where failures were swallowed, which
  is why a green suite coexisted with a pipeline that could not finish.
