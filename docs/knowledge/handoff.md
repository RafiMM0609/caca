# Implementation handoff

Checkpoint: 2026-11-08, after commit `5eb3318`. Read [origin and decisions](origin-and-decisions.md) before reprioritizing work. This is pre-production code.

## What exists

- Authenticated local FastAPI dashboard: projects, versioned plans, task/worker tree, review evidence, tester status, source/artifact inspection and local Studio editing.
- **Planner-driven parallel Pi workers.** The plan is a DAG; every task whose dependencies have merged runs at once as a real `PiWorker` harness in its own git worktree with an isolated `HOME`. Workers are bounded by `FACTORY_WORKER_CONCURRENCY`. Review sees the diff, not the worker's own description. A merge barrier lands accepted branches into the product branch one at a time, and a conflicting merge spawns a fixer worker before the wave retries.
- **One git repository per product** at `data/products/<project-id>`. SQLite holds workflow state including a `workers` table; the product repository holds source, history and diffs.
- SQLite WAL workflow store with atomic claims, one active workflow job per project, transactional call-budget reservation, events, heartbeats, cancellation and interrupted-job reconciliation.
- Two Pi interfaces: `PiWorker` (coding harness with tools, for tasks) and `PiAgent` (tool-free structured JSON client, for planner, test preparation and review).
- Native Linux verification with configured command arrays, operator-owned post-build checks, deadlines/output limits and process-group cleanup. Docker verification remains available separately but the Nuxt profile is native because `npm ci` needs network.
- Plan editing after completion and rebuilding from prior verified source; original source/artifacts are retained.
- Independent tester preparation concurrent with the worker lane. Declarative API assertions and browser navigation/fill/click/selection/visible/text/count/url assertions run against a readiness-checked local app. Reports and screenshots are protected evidence. Failures block publication.
- Browser cases fail on uncaught page exceptions and console errors unless the case sets `allow_console_errors`. `HEAD` requests are supported.
- `checks/secret_scan.py` blocks credential-shaped values and exact configured secret values from appearing in a built artifact.
- Local Nuxt Studio draft workspace, loopback preview, content diff, stale-draft rejection. An applied draft imports onto its own product branch and merges only after verification passes.
- A pinned Nuxt 4.6.4 / Content 3.16.2 / Studio 1.8.0 starter and regular-file build export. Production Studio is disabled in the starter.
- Optional Memorix MCP adapter. Legacy deploy lane/helper remain in source; local profiles have no targets and dashboard deploy controls are hidden.

## Code map

| Area | Main files |
| --- | --- |
| Settings, profiles and contracts | `factory/config.py`, `factory/models.py`, `examples/` |
| Plan DAG, readiness and topological order | `factory/models.py` (`Plan.ready_set`, `Plan.execution_order`) |
| Product repositories, worktrees, merges, diffs | `factory/products.py` |
| Worker waves, review, merge barrier, fixer | `factory/parallel.py` |
| Durable state, jobs, workers table | `factory/db.py` |
| Workflow, Studio import and publication | `factory/engine.py` |
| Pi harness worker and tool-free JSON client | `factory/agents.py` |
| Memory adapter | `factory/memory.py` |
| Native/Docker verification, operator checks | `factory/verifier.py`, `factory/process.py`, `factory/sandbox_runner.py`, `checks/` |
| Local app readiness and cleanup | `factory/local_app.py`, `factory/watchdog.py` |
| API/browser test schema and executor | `factory/testing.py` |
| Studio draft lifecycle / source validation | `factory/studio.py`, `factory/workspace.py` |
| HTTP endpoints and auth | `factory/api.py` |
| Dashboard and derived worker states | `factory/static/` |
| Studio-ready application starter | `factory/starters/nuxt-studio/` |
| CLI and legacy deployment code | `factory/cli.py`, `factory/deploy.py`, `deploy/` |
| Automated tests and real editor journey | `tests/`, `scripts/validate_studio_journey.py` |

## Important continuation details

- **Scheduling comes from the DAG.** `Plan.dependencies` may point at any task. Cycles, self-dependencies, unknown ids and duplicate ids are rejected by validation. Do not reintroduce a rule that dependencies must appear earlier in the list.
- **Worktree/branch cleanup order matters.** Remove any stale worktree still attached to a branch before deleting or reusing that branch, or git refuses with "a branch named ... already exists". A retry must start from a clean branch.
- **`PiAgent` must not be used for implementation.** The runtime injects a coding-agent preamble, so a tool-free model emits markup that looks like a tool call. Measured live: implementer produced valid JSON 0 of 3 times that way.
- Review input is a bounded diff (200 KB from `products.diff_against`), not the whole tree. `workspace.snapshot()` still enforces the 300 KB text limit and is used for the dashboard source view.
- `Plan.test_flows` is optional for general profiles but required by a tester-enabled build. Tester suites must cover each declared flow with matching API/browser kind. Structural coverage checks cannot certify semantic quality.
- Profile commands are administrator-owned. `tester` requires native execution and `serve_argv`. Model selection uses `FACTORY_MODEL`, with optional `FACTORY_REVIEW_MODEL` / `FACTORY_TESTER_MODEL`.
- `FACTORY_ALLOW_NATIVE=1` is explicit consent to local code execution, not a sandbox. Workers get an isolated `HOME` and lose factory tokens, but **provider credentials still reach them** and native code has account-level filesystem/network access.
- Model-call budgets apply to planner, review and tester-preparation calls. Harness workers are separate processes and are not metered per call; bound them with `FACTORY_WORKER_CONCURRENCY` and `FACTORY_AGENT_TIMEOUT`.
- Studio imports only `.md`, `.json`, `.yaml`, `.yml` under `content/`. Other code edits and media are not imported. Preview requires localhost and expires after 30 minutes.
- Draft import sets `pending_edit`; `Engine.import_studio_edit` copies it onto branch `studio/<job>` and merges only after verification. A failed edit keeps the previous artifact and the pending source — inspect before designing retries.
- The starter lockfile was validated by clean `npm ci`. npm-generated extraneous platform entries caused an earlier failure and were removed; do not blindly reintroduce them.
- Studio's launcher is in a custom element/shadow DOM. Browser automation must wait for it and scroll the iframe into view. Avoid the page's last generic button: it can hit the demo contact form before Studio hydrates.
- Nuxt Studio/API processes are local-only. A remote dashboard viewer's localhost is a different machine; remote preview is not implemented.
- `workspace.apply()` and the `Change` model are now only used by tests and the security suite; the engine no longer applies whole-file JSON. They were deliberately kept, not removed.

## Last executed evidence

- **First real brief-to-tested-build run** (2026-11-08, live `opencode-go/deepseek-v4.1-flash`, no doubles for planning/implementation/review). A brief for a carpentry workshop site ("Kayu Jati") went through the planner, which produced a genuine DAG: `scaffold-nuxt-app → {health-endpoint, single-page-ui-and-contact-form} → integration-and-verification`. The planner correctly raised four open business questions and refused to invent copy; the operator answered them and the plan advanced to revision 2. Three Pi harness workers then ran in real waves — `health-endpoint` and `single-page-ui-and-contact-form` **started the same second (08:59:39) and merged together**, proving concurrency, and `integration-and-verification` waited for both. The merged application (9 tracked files, 6 commits) then built with real `nuxt build`, passed `checks/secret_scan.py` (126 files), and passed **all four independent API/browser acceptance cases** with `console_errors: []` and `page_errors: []`. Screenshots show a real, styled site. **Caveats, stated plainly:** the build and verification were driven by scripts in `data/analysis/proof/` calling `Engine`/`Verifier` directly, not by the dashboard scheduler; the tester *suite* was authored by the scripted `ContractTester`, not a live tester call; and because builds do not resume (`parallel.py` starts `completed` empty), the integration task was re-attempted in a later run rather than finishing inside one job. The three merged tasks and their review verdicts are genuine live model work.
- **FULLSTACK CRUD NOW PASSES END-TO-END** (2026-11-08, live model, SQLite brief for a bike-rental fleet manager). `data/analysis/proof/crud3` reached `state: ready` at revision 2 with a published 152-file artifact after **11 of 11 acceptance cases passed** — 5 API (health, create+list, update, delete, validation) and 6 browser (empty state, render list, add, inline edit, cancel edit, delete) — with `report.json: passed`. The DAG was `scaffold-nuxt-release → sqlite-persistence → bikes-rest-api → fleet-ui → integration-verification`; `bikes-rest-api` needed one repair (attempt 2) and the others merged first try. The published artifact was then served independently, a bike created through the API, and **the same record was still returned after a full process restart**, confirming SQLite persistence in the shipped artifact. Screenshots: `data/screenshots/crud2/`. This supersedes the earlier "CRUD proof run attempted" result below, which failed at the tester gate for harness reasons, not application reasons.
- **Five harness defects had to be fixed to get there**, each found by a live suite and each with a revert-tested regression: (1) every case shared one process and one database, so a create-case's `id: 1` made the next case's `id: 1` assertion fail — cases are now isolated with a fresh data directory and process; (2) a suite-wide `qa_timeout` meant later cases timed out because earlier ones spent the budget — the budget is now per case; (3) `AcceptanceFlow` had no selector contract, so a worker shipped `edit-input-name` while the tester guessed `input-name` — flows now declare a `selectors` map that both sides read; (4) `expect_text` could not observe an `<input>` (no text content) and an empty pattern asserted nothing — `expect_value` was added and empty `expect_text` is rejected; (5) `click`/`fill`/`select` used Playwright strict mode, so clicking "the delete button" on a two-row list threw instead of acting on the first row. Browser cases may also make setup requests before `goto`, which three otherwise-valid suites needed.
- **Four defects blocked every previous real run** and are now fixed with revert-tested regressions: review diff truncated at 200 KB (a 290 KB lockfile hid all source from the reviewer, which then rejected correct work); `expect_url` demanded `value` while models naturally write `path`; `process.run()` *killed* a chatty tool-using worker instead of bounding its tail; and `safe_path()` rejected `.env.example`, making any app that documents env vars unbuildable. The last two are in error-handling paths, which is why the suite stayed green while the pipeline could not complete.
- **Persistence and profile `env` landed** (2026-11-08). A profile can now pass `env` to the build and the served app, with `{data}` (durable per-product directory) and `{app}` (build tree) placeholders; `FACTORY_*` names and provider credentials are rejected by validation. Verification uses a throwaway data directory so record-creating cases stay deterministic, while `factory run <project>` serves the published artifact against `data/products/<id>/data`. A real two-process restart test proves a record written before the restart is readable after it. The Kayu Jati project reached `state: ready` with a published artifact through the normal gates (1 API + 3 browser cases) and `factory run` served it with `DATABASE_URL`/`DATA_DIR` set. **Not yet proven:** a full CRUD app built from a brief by workers with a real testing loop. Persistence and env are the foundation; schema/migration and auth for generated apps are still missing, and the tester cannot yet send auth headers (though its shared httpx client does persist cookies).
- **Three more assertion defects found by a live tester suite** and fixed with revert-tested regressions: `expect_visible` used Playwright strict mode and failed a selector matching three visible cards; `expect_text` checked only the first match and failed when the copy was in the second card (and was case-sensitive); and the tester received only the brief and flows, never the approved task instructions, so it invented assertions such as requiring a service card to contain the product name. `factory run` also joined `serve_argv` against the job directory although the artifact root already is `artifact_dir`, so it looked for `release/release/server/index.mjs`.
- Python suite: **137 passed, 3 skipped**, one upstream Starlette/httpx deprecation warning. Two skips are the optional Docker tests; the third is the opt-in live worker test, which passed when run separately with `FACTORY_LIVE_WORKER=1`. (114, 117, 125 and 131 were recorded as regression tests were added.)
- **Real Nuxt/Studio journey passed** (2026-11-08, `scripts/validate_studio_journey.py`, `FACTORY_ALLOW_NATIVE=1`, real Chromium, exit 0). This closes the highest-value verification gap: dashboard create → real `npm ci` + `nuxt build` (907-file artifact) → 3 API/browser cases → embedded Nuxt Studio edit → diff → apply → **real rebuild** at revision 2 → 3 cases re-run. Both jobs ended `state: succeeded`; `report.json` is `passed` with `console_errors: []` and `page_errors: []`; the Studio edit is proven present in revision 2's `content/site.json`; the operator check ran (`CHECK 1 PASS`), which also proves the relative `checks_dir` resolves. Evidence: `data/validation/studio-journey.json` and `data/validation/run-524ae790/`. The AI side remains a **scripted test double**; npm, the Nitro server, HTTP and Chromium are real. Note the script needs `PYTHONPATH` set to the repo root when run as `./.venv/bin/python scripts/...`, otherwise it dies with `ModuleNotFoundError: factory` — and because that failure was piped through `tail`, it once reported exit 0. Use `set -o pipefail`.
- Worker-state JavaScript tests: **8 passed**. Lint and syntax checks passed. Dashboard smoke-tested in real Chromium: login, workspace, project detail, no page errors.
- **Planner emits a real DAG** (2026-11-08, live). The planner prompt previously asked for an "ordered task plan" and never mentioned that `depends_on` expresses parallelism, so it produced linear plans that could not use workers concurrently. The prompt now states that tasks form a DAG and that independent work should leave `depends_on` empty. A live plan call for a two-page site with a shared header then produced `scaffold → {header, home, about} → verification`: wave 1 has one task, wave 2 is three concurrent workers, wave 3 waits on all three.
- **Live parallel build** (2026-11-08, `opencode-go/deepseek-v4.1-flash`): two independent tasks ran as concurrent Pi harness workers and both merged into the product repository in **6.8s**, producing real `index.html` and `about.html`. A revision-2 rebuild started from files committed by revision 1.
- **Live full pipeline** (2026-11-08): a Pi worker produced an application, the build step passed, `checks/secret_scan.py` passed, and both the API and browser acceptance cases passed before the artifact was published (`state: ready`).
- **Live conflict resolution** (2026-11-08): two workers editing the same `config.json` triggered `merge.conflict`; a fixer resolved the integration on the target in **3 of 3 runs**, leaving the target clean with both commits. One earlier failure was the fixer exceeding a 240s agent timeout, not a logic error — give it the default 600s when conflicts are expected.
- Validation gates exercised against real Chromium: `expect_count`/`expect_hidden`/`expect_url`/`reload` passed on a live server; an uncaught page exception and a `console.error` were both blocked; `allow_console_errors` let a reviewed case through. `secret_scan.py` blocked credential shapes and a literal `OPENCODE_API_KEY` value; an end-to-end build test confirms a leaking build stays `failed` with no artifact.
- **Studio regression found and fixed**: replacing the sequential loop stopped the engine reading `pending_edit`, so an applied draft produced a revision that never reached the product source. Drafts now import onto their own branch. The test was confirmed to fail when the fix is reverted. The starter journey now has an end-to-end test as well.
- Concurrent tester test uses synchronization to require overlap with implementation. Negative tests cover real browser assertion failure with evidence and integration failure blocking an artifact.
- **Workspace paths must not contain spaces** (found and fixed 2026-11-08). With the checkout at `.../Default Project/caca`, Nuxt Content encoded the space as `%20` and tried `mkdir '.../Default%20Project/...'`, which does not exist; content queries returned 500, the page rendered as an empty shell, and the browser case failed while the build itself passed. Control experiment, same code, only the space differing: `/tmp/nosp/app` rendered `hero-title` and brand text (1/1); the spaced path rendered 0/0. Renaming the directory to `Default-Project` fixed it, and the journey above now passes. Treat a spaced `FACTORY_DATA` as unsupported rather than as a mysterious browser-test failure.

**Model responses in the default suite are scripted test doubles.** The live runs above are separate and dated. npm, app servers, HTTP requests and Chromium interactions in the scripted tests are real. Local evidence lives in ignored `data/validation/` in the original workspace and is not guaranteed to exist in a fresh clone.

**Known gaps after this round.**

- The full Nuxt Studio journey **has now been re-run and passes** with the parallel worker lane (see Last executed evidence). The journey still covers one starter-shaped project with a scripted planner/tester double; it does not exercise live model workers producing a Nuxt app from a brief.
- No lint or TypeScript typecheck step exists. The starter lockfile ships `typescript` but not `vue-tsc`, and adding a dependency means regenerating the validated lockfile.
- The dashboard does not render worktrees, waves or the `workers` table. The data exists; the UI does not show it.
- The secret scan is a pattern and exact-value scan over text files up to 20 MB; it is not entropy analysis and will not catch an obfuscated or split credential.
- The fixer is exercised, but its cost and latency under real conflicts are not characterised. Each fixer run is a full model worker.
- Blockers can starve a wave: a plan whose task always fails exhausts `max_repairs` and fails the job rather than degrading gracefully.

## Remaining work — do not report as finished

1. ~~**Re-run the real Nuxt Studio journey** with the parallel worker lane~~ — **done 2026-11-08, passed** (see Last executed evidence).
2. ~~**A live Pi worker producing a Nuxt application from a brief**~~ — **done 2026-11-08** (see First real brief-to-tested-build run), with the caveats recorded there. What is still unproven is the *single-command* path: one `build` job from approval to published artifact through the dashboard scheduler. Today the run needed a script, a recovery between waves, and a separately-driven verification. Closing that is the next real milestone.
3. **Test cases share one database, so write-cases contaminate each other.** This is now the top blocker for CRUD acceptance. In one suite the first case inserted a bike and received `id: 1`; the next case inserted again and asserted `id: 1`, but the app correctly returned `id: 2`. `run_suite()` reuses a single httpx client and the same `DATABASE_URL` for every case, and cases often start with an empty-state assumption. It needs per-case isolation: either a fresh data directory per case, or an explicit reset/seed hook the planner and tester agree on. Until then a multi-case CRUD suite can only pass if the tester happens to avoid id or count assertions.
4. **Builds do not resume across a restart.** `ParallelBuilder.build_tasks()` starts with `completed = set()`, so a job interrupted after some tasks merged re-runs every task from scratch even though the product repository already contains the merged commits. `Store.completed_tasks()` and `worker_states()` exist and are unused — the resume path was written but never wired in.
4. Dashboard view for workers, waves, worktrees and per-task branches, so parallel work is visible to the operator.
5. Client attribution and product export/archive. Products are for different clients but there is no `client_id`, no per-client archiving and no export/hand-off path for a product repository.
6. Live Memorix search/store roundtrip, project isolation and failure behavior.
7. Native Windows lifecycle/locks/process cleanup/npm executable support plus real Windows integration/browser tests. Currently Linux-specific code remains.
8. Lint and TypeScript typecheck in the validation profile, once the lockfile can be regenerated and re-validated safely.
9. Richer editable schemas/components, media import, arbitrary code editing or chat-based change requests only if prioritized by the owner.
10. Automatic QA repair/escalation after acceptance-test failures. Current behaviour stops for review.
11. Third-party integration support: SAP/Microsoft adapters, mocks/sandboxes, credentials and representative integration tests. Cross-origin auth, WebSockets and external-service browser flows are outside the current tester contract.
12. Broader CRUD/POS/ERP templates, transactions/concurrency, databases and application migrations. None are production-certified.
13. Native sandboxing/resource policy, multi-user/tenant authorization, retention/monitoring and recovery drills before production use.
14. Revisit deployment only after the owner reopens that scope. The presence of SSH code is not evidence of a real server deployment.

No live model credentials, remote production targets or customer accounts were configured during the recorded validation. Do not ask users to paste secrets into chat.

## Starting the next session

1. Check `git status`, current branch/HEAD and the latest user request; preserve uncommitted work.
2. Read `AGENTS.md`, this handoff and the relevant feature document; inspect source/tests for the requested change.
3. Use `examples/config-studio-linux.json` for the editing workflow; do not default to mandatory Docker or activate deployment.
4. Identify what can be verified locally and what requires real credentials/platforms. State assumptions; continue independent work.
5. Run checks relevant to the change and update this handoff when capabilities or evidence change. Commit/push according to current authorization; never claim a push or a test succeeded without checking its result.
