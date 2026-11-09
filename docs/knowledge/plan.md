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

**The one finding that dominates everything else.** Across every recorded run:

```
build jobs : failed, failed, failed, interrupted, interrupted, interrupted
verify jobs: succeeded   ← every published artifact came from a separately enqueued verify
```

A `build` job is the one that runs the workers *and* publishes. It has **never once succeeded
end-to-end**. Every green result was produced by enqueueing a `verify` job afterwards, from a
driver script. So the parts work; the factory does not yet work as one operation.

## Gap analysis

Ordered by what blocks the stated goal, not by how interesting it is.

1. **One job has never closed the loop.** `POST /approve` does enqueue a single `build` job that
   plans, runs workers, verifies and publishes, so the wiring exists — it has simply never
   completed. This is the milestone.
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
12. **Acceptance cases are only isolated when the profile declares `{data}`.** `verifier.py`
    enables per-case isolation through `if any("{data}" in value for value in profile.env.values())`.
    A tester-enabled profile without `env` shares one process and one database across every
    case, which is the contamination the isolation work was meant to remove. Unproven for
    profiles that keep state anywhere other than the data directory.

## Phases

### Phase 0 — Close the loop (P0, the milestone)

One `build` job from approval to published artifact, with no manual step.

1. Run the same CRUD brief as a `build` job, not a `verify`, on current code.
2. Capture the exact failing stage with evidence; classify it as planner, worker, review,
   verifier or tester.
3. Fix only real defects. Do not substitute retries for a fix.
4. **Done when:** the `build` row is `succeeded`, the project is `ready`, an artifact digest
   exists, every `workers` row is terminal, and the run is reproducible from a clean data dir.

### Phase 0 findings (recorded as they are found)

- **F1 — the planner has no retry, the tester has three.** `Engine.prepare_tests()` retries
  `max_repairs + 1` times on a structurally invalid response; the planner calls
  `generate(...)` once and a single malformed generation fails the whole job. Observed live: a
  planner job died with `ValueError` on its first attempt, and an identical standalone call with
  a slightly shorter prompt succeeded immediately. One model hiccup should not kill a run, so
  the planner needs the same bounded retry the tester already has.

### Phase 1 — Repair loop (P1)

5. On a verification or tester failure, persist structured evidence: the failing case, expected
   versus actual, and the screenshot path.
6. Spawn a repair worker on the product repository with that evidence, then re-verify. Bound it
   by `max_repairs` and fail loudly when exhausted — never silently ship a partial result.
7. **Done when:** deliberately breaking one acceptance assertion causes a repair and a
   republish, with an event trail showing attempt → evidence → fix.

### Phase 2 — Durability (P2)

8. Wire `completed_tasks()` / `worker_states()` into `build_tasks()` so a restart resumes at a
   wave boundary instead of redoing merged work.
9. Add a **job-level** deadline, distinct from the per-worker `agent_timeout`.
10. **Done when:** a build killed mid-wave resumes after a scheduler restart without re-running
    tasks that already merged.

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

**Phase 0 first.** It is the only item that converts "the components work" into "the product
works", it is cheap, and its result decides whether Phase 1 is even the right next move.
Everything later is speculative until a single build job goes green end to end.

## Honesty rules for this plan

- Report a phase complete only with the executed evidence that its "done when" names.
- Distinguish scripted test doubles from live model calls, and real builds from stand-ins.
- Do not describe native execution as sandboxed, or this pre-production release as
  production-ready.
- When a fix touches an error-handling path, prove the regression by reverting the fix and
  watching the test fail. Several long-standing bugs lived where failures were swallowed, which
  is why a green suite coexisted with a pipeline that could not finish.
