# Implementation handoff

Checkpoint: 2026-11-07, after implementation commit `943f044`. Read [origin and decisions](origin-and-decisions.md) before reprioritizing work. This is pre-production code.

## What exists

- Authenticated local FastAPI dashboard: projects, versioned plans, task/worker tree, review evidence, tester status, source/artifact inspection and local Studio editing.
- SQLite WAL workflow store with atomic claims, one active workflow job per project, transactional call-budget reservation, events, heartbeats, cancellation and interrupted-job reconciliation.
- OMP adapter for planner, implementer, reviewer and independent tester. Sessions are tool-free structured generation clients with bounded calls; source edits are applied by the controller. The worker binary is now the public Pi 1.5.0 CLI (checked 2026-11-07: one live planner-lane call on `opencode-go/deepseek-v4.1-flash` returned parsed JSON `{"ok":1}` through the updated adapter; 58 unit tests pass with scripted doubles).
- Native Linux verification with configured command arrays, deadlines/output limits and process-group cleanup. Docker verification remains available separately.
- Plan editing after completion and rebuilding from prior verified source; original source/artifacts are retained.
- Independent tester preparation concurrent with implementation. Declarative API assertions and browser navigation/fill/click/visible/text assertions run against a readiness-checked local app. Reports/screenshots are available as protected evidence. Failures block publication.
- Local Nuxt Studio draft workspace, loopback preview, content diff, stale-draft rejection and content-only verification jobs. Apply does not invoke implementer regeneration.
- A pinned Nuxt 4.6.4 / Content 3.16.2 / Studio 1.8.0 starter and regular-file build export. Production Studio is disabled in the starter.
- Optional Memorix MCP adapter. Legacy deploy lane/helper remain in source; local profiles have no targets and dashboard deploy controls are hidden.
- Operator-owned post-build checks wired into native verification (`checks_argv` + `checks_dir`). The active Nuxt profile runs `checks/secret_scan.py` so a build that embeds a provider credential cannot be published.
- Strengthened browser test contract: `HEAD` requests, plus `reload`, `select`, `press`, `expect_hidden`, `expect_count` and `expect_url`. Browser cases now fail on uncaught page exceptions and console errors unless the case explicitly sets `allow_console_errors`.

## Code map

| Area | Main files |
| --- | --- |
| Settings, profiles and contracts | `factory/config.py`, `factory/models.py`, `examples/` |
| Durable state and plan/job invariants | `factory/db.py` |
| Workflow, parallel tester preparation and publication | `factory/engine.py` |
| OMP model client / memory adapter | `factory/agents.py`, `factory/memory.py` |
| Native/Docker verification and command lifecycle | `factory/verifier.py`, `factory/process.py`, `factory/sandbox_runner.py` |
| Local app readiness and cleanup | `factory/local_app.py`, `factory/watchdog.py` |
| API/browser test schema and executor | `factory/testing.py` |
| Studio draft lifecycle / source/archive validation | `factory/studio.py`, `factory/workspace.py` |
| HTTP endpoints and auth | `factory/api.py` |
| Dashboard and derived worker states | `factory/static/` |
| Studio-ready application starter | `factory/starters/nuxt-studio/` |
| CLI and legacy deployment code | `factory/cli.py`, `factory/deploy.py`, `deploy/` |
| Automated tests and real editor journey | `tests/`, `scripts/validate_studio_journey.py` |

## Important continuation details

- `Plan.test_flows` is optional for general profiles but required by a tester-enabled build. Tester suites must cover each declared flow with matching API/browser kind. Structural coverage checks cannot certify semantic quality.
- Profile commands are administrator-owned. `tester` currently requires native execution and `serve_argv`. OMP calls use `FACTORY_MODEL`, with optional reviewer/tester model overrides.
- `FACTORY_ALLOW_NATIVE=1` is explicit consent to local code execution, not a sandbox. Native code has account-level filesystem/network access; Docker CPU/RAM isolation does not apply.
- Studio imports only `.md`, `.json`, `.yaml`, `.yml` under `content/`. Other code edits and media are not imported. Preview requires localhost and expires after 30 minutes.
- Draft import sets a `pending_edit` source and creates a `verify` job at a new revision. Only successful verification replaces the current artifact. A failed edit keeps the previous artifact and pending source; inspect this state before designing retries.
- Model/UI text snapshots omit dependency lockfiles, but source copies retain them. Total source/context/file limits are enforced in `workspace.py` and models; do not confuse the 20 MB native/test export contract with every archive helper's absolute cap.
- The starter lockfile was validated by clean `npm ci`. npm-generated extraneous platform entries caused an earlier failure and were removed; do not blindly reintroduce them. Update dependency versions/integrities through a fresh install and rerun the actual starter journey.
- The starter materializes Nitro links within its output tree before packaging. Acceptance testing launches `release/server/index.mjs`, the same output subsequently packaged.
- Studio's launcher is in a custom element/shadow DOM. Browser automation must wait for it and scroll the iframe into view. Avoid using the page's last generic button: it can hit the demo contact form before Studio hydrates.
- Nuxt Studio/API processes are local-only. A remote dashboard viewer's localhost is a different machine; remote preview is not implemented.

## Last executed evidence

- Python suite: **81 passed, 2 skipped**, with one upstream Starlette/httpx deprecation warning. The skipped tests were the optional Docker tests in this run.
- Worker-state JavaScript tests: **8 passed**. Lint, syntax and diff-whitespace checks passed.
- Validation gates were exercised against a real Playwright Chromium on 2026-11-07: `expect_count`/`expect_hidden`/`expect_url`/`reload` passed on a live local server; an uncaught page exception and a `console.error` were both blocked; `allow_console_errors` let the reviewed case through. `checks/secret_scan.py` passed a clean artifact and blocked credential-shaped values and a literal `OPENCODE_API_KEY` value. An end-to-end build test confirms a leaking build stays `failed` with no artifact.
- Actual dashboard journey passed: create starter → clean npm installation → Nuxt build → 3 API/browser cases → embedded Studio title edit → diff → apply revision 2 → rebuild → all 3 cases passed again. Prior artifact retained; desktop/mobile screenshots captured.
- Concurrent tester test uses synchronization to require overlap with implementation. Negative tests cover real browser assertion failure with evidence and integration failure blocking an artifact.
- The real editor journey is reproducible with `scripts/validate_studio_journey.py`; manual GitHub Actions execution also runs it and uploads selected report/screenshots.

**AI model responses were scripted test doubles.** npm, Nuxt Studio, local app servers, HTTP requests and Chromium interactions were real. These results do not validate live OMP generation or live Memorix. Local evidence lives in ignored `data/validation/` in the original workspace and is not guaranteed to exist in a fresh clone. The reproducible commands and recorded result are the portable evidence.

**Known gaps in this validation round.** The full Nuxt Studio journey was **not** re-run after the tester contract and checks changed; the last full-journey evidence predates these edits. Live Pi generation still fails intermittently (see remaining work 1) and was not re-tested here. There is still no lint or TypeScript typecheck step: the starter lockfile ships `typescript` but not `vue-tsc`, and adding a dependency would require regenerating the validated lockfile. The secret scan is a pattern and exact-value scan over text files up to 20 MB; it is not entropy analysis and will not catch an obfuscated or split credential.

## Remaining work — do not report as finished

1. Live Pi provider generation/review/tester acceptance for factory stages (planner, implementer, reviewer, tester) with the OpenCode Go provider; validate structured output, error handling, quota limits and real billing behavior. Adapter-level single-call validation passed (2026-11-07).

   Measured on 2026-11-07 with `opencode-go/deepseek-v4.1-flash`: through the Pi CLI, planner 3/3, tester 1/3 and implementer 0/3 produced valid JSON; overriding `--system-prompt` did not fix it. Pi injects a hardcoded coding-agent preamble and offers no constrained decoding, so a model that believes it has tools emits `<tool_call>` XML. Calling the provider directly with `response_format: json_schema` (strict) returned tester 5/5 and implementer 3/3 at roughly a third of the latency. The endpoint requires an `x-opencode-session` header, and using it bypasses Pi, so subscription terms must be checked before choosing that path.
2. Live Memorix search/store roundtrip, project isolation and failure behavior.
3. Native Windows lifecycle/locks/process cleanup/npm executable support plus real Windows integration/browser tests. Currently Linux-specific code remains.
4. Richer editable schemas/components, media import, arbitrary code editing or chat-based change requests only if prioritized by the owner. Studio currently edits scoped content.
5. Automatic QA repair/escalation after acceptance-test failures. Current behavior stops for review; it does not spawn an automatic fixer.
6. Third-party integration support: SAP/Microsoft adapters, mocks/sandboxes, credentials and representative integration tests. Cross-origin auth, WebSockets and external-service browser flows are not supported by the current tester contract.
7. Broader CRUD/POS/ERP templates, transactions/concurrency, databases and application migrations. None are production-certified by the starter tests.
8. Native sandboxing/resource policy, multi-user/tenant authorization, more advanced task concurrency, retention/monitoring and recovery drills before production use.
9. Revisit deployment only after the owner reopens that scope. The presence of SSH code is not evidence of a real server deployment.

No live model credentials, remote production targets or customer accounts were configured during the recorded validation. Do not ask users to paste secrets into chat.

## Starting the next session

1. Check `git status`, current branch/HEAD and the latest user request; preserve uncommitted work.
2. Read `AGENTS.md`, this handoff and the relevant feature document; inspect source/tests for the requested change.
3. Use the appropriate example profile. The current editing demo uses `examples/config-studio-linux.json`; do not default to mandatory Docker or activate deployment.
4. Identify what can be verified locally and what requires real credentials/platforms. State assumptions; continue independent work.
5. Run checks relevant to the change and update this handoff when capabilities or evidence change. Commit/push according to current authorization; never claim a push or a test succeeded without checking its result.
