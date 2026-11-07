# Origin and design decisions

Recorded 2026-11-07 from the owner's instructions and the implementation history, extended 2026-11-08 with the parallel-worker decision.

## The actual business goal

The owner wants to start a software company from zero and build an AI software factory. Initially there are no company templates, existing products or established production platform. The factory should eventually generate new applications ranging from static portfolios and CRUD/admin systems to POS and complex ERP integrations, and those products are for **different clients**. Reliability, reviewability and safe operation matter more than merely producing code.

The owner's mental model is a planner assigning work to workers. The dashboard must make that work visible: what was assigned, dependencies, progress, independent review and evidence. Useful templates should emerge from working applications. Do not assume a mature template catalog already exists or force every business application into one universal template.

## How the runtime was selected

The investigation started with DeepSeek Harness and its UI customization possibilities. The owner then asked whether a harness could power each agent in a larger factory. OpenHands software-agent-sdk and Oh My Pi were inspected alongside it. The owner explicitly chose **Oh My Pi**. The product being built is the software factory around that runtime, not an Oh My Pi clone or a modification of DeepSeek Harness.

Historical research checkouts were kept separately under `/workspace/dsh-anton/` in the original workspace. They are not dependencies of this repository and may not exist in another checkout. Do not copy them into the factory or rely on absolute paths to run this project.

Provider subscriptions with recurring usage windows were discussed as a cost concern. No paid provider was finally configured here. The current adapter needs a real selected provider/model and credentials. It does not automatically supply a subscription or route around provider limits.

## Lightweight persistence

An early PostgreSQL suggestion was challenged by the owner. The resulting design uses **SQLite for operational state** and optional **Memorix for shared project knowledge**. They solve different problems. Jobs, dependencies, approvals, revisions and artifact state must remain deterministic and durable in SQLite; memory is bounded, advisory context for model sessions. Memorix being unavailable must not silently corrupt or replace task state.

## Operating system and scope changes

1. The original ambition included end-to-end deployment to Linux servers.
2. Docker verification and an SSH release helper were implemented as an early foundation.
3. The owner clarified that local review would happen on a Windows laptop **without Docker**. The constraint was the environment, not necessarily RAM.
4. The owner narrowed the immediate milestone to tests and `npm run build`; deployment was postponed.
5. Cross-platform native execution was discussed. Native Windows was requested, but not implemented.
6. The next concrete request was to align execution with Linux and prove edit-after-completion and integration testing. An opt-in native Linux runner was added and validated.
7. On 2026-11-08 the owner confirmed the development target for this version is **native only**.

Current validated target is native Linux; Docker is optional and remains for the legacy profile. Windows compatibility remains an outstanding goal. Do not infer Windows support from Python/JavaScript alone: `fcntl`, POSIX process groups and other lifecycle details still need adaptation and real Windows tests.

## Editing after completion

The owner wants generated UI to remain editable from the factory dashboard. Nuxt Studio was selected for the capabilities it actually offers: local content editing, schema-based forms and preview inside supported Nuxt Content applications.

The chosen journey is: verified build → separate editable draft → embedded Studio form/preview → review a frozen diff → apply as a new revision → run build and acceptance tests again. Existing verified source/artifacts are preserved. Studio is not treated as an arbitrary Vue-code editor or a universal editor for non-Nuxt apps.

The first supplied starter exposes brand, hero text, button label, accent color and features via `content/site.json` and a Nuxt Content schema. It includes a contact demo that validates input without sending email. The starter is a new foundation created in this project, not a pre-existing company template.

## Why there is an independent tester

The owner asked for a tester outside the implementation worker: it receives flows from the planner, prepares real API/web-UI testing while the app is being built, and executes those tests when the app is ready.

The implementation follows that split. The planner supplies `test_flows`. A separate Pi invocation prepares a declarative suite concurrently with implementation, without seeing the worker's source in that preparation request. The controller validates coverage and executes HTTP/Playwright cases after readiness. This limits self-certification and makes evidence visible; it does not prove complete business coverage or eliminate human acceptance.

## Pi as a real coding harness (2026-11-08)

This decision changed the execution model and reversed an earlier one.

Originally Pi was used only as a **tool-free structured generation client**: every stage asked a model for JSON, and the controller applied whole-file changes. In practice the implementer stage produced valid JSON **0 of 3 times** in a measured live run with `opencode-go/deepseek-v4.1-flash`. The cause was structural rather than a weak model: Pi injects a hardcoded coding-agent preamble ("help users inspect files, run commands, and modify code"), so a model told it has no tools still emitted markup resembling a tool call, and the stage failed with no usable output. Overriding `--system-prompt` did not fix it, and Pi exposes no constrained-decoding flag. Calling the provider directly with `response_format: json_schema` returned valid output 5 of 5 — but the owner's position is that Pi, not a bespoke HTTP client, is the runtime.

The owner's stated model is therefore implemented directly: **the planner spawns Pi according to need, each spawned Pi has its own job, and the planner decides what must be sequential.** Concretely:

- Plan tasks form a DAG, so the planner expresses parallelism by leaving dependencies open. The planner prompt states this explicitly; without it, the planner produced a linear list and parallelism was never used.
- Each task runs as a `PiWorker` — a real coding harness with tools, in its own worktree.
- The other stages (planning, test preparation, review) keep the tool-free JSON client, where structured output actually works.
- Merging is a barrier: a wave finishes, then branches merge one at a time. The owner chose this rule explicitly so parallel code changes never merge into a moving base.
- A conflicting merge spawns a **fixer** worker rather than silently dropping a branch.

Because workers now have tools, each gets an isolated `HOME` so `~/.ssh` and `~/.aws` are unreachable. Provider credentials still reach the worker because the model call needs them; that exposure is recorded rather than hidden.

## Products are separate repositories (2026-11-08)

Product source was previously stored per job under `data/jobs/<job>/source`, keyed by job rather than product, with no version control. Since products are for different clients, and because per-worker worktrees require it, each product now owns a git repository at `data/products/<project-id>`.

Consequences the owner accepted: `data/` stays git-ignored, so products are not committed to this repository; handing a product to a client or archiving it still needs a deliberate export step, which is **not yet implemented**. Client attribution (`client_id`) is also **not implemented** — products are separated by repository and directory, not by login, and clients do not log in.

## Validation must mean more than a build (2026-11-08)

The owner rejected treating "unit tests plus a browser case" as sufficient validation. Operator-owned checks were wired into native verification (`checks_argv` / `checks_dir`) because `checks_dir` had only ever been read by the Docker verifier, so the active native profile ran no operator checks while appearing to have them. The first real check is a secret scan. The browser contract was extended with `HEAD`, `reload`, `select`, `press`, `expect_hidden`, `expect_count` and `expect_url`, and browser cases now fail on console errors and uncaught exceptions.

Lint and TypeScript typecheck are **still not part of validation**: the starter lockfile ships `typescript` but not `vue-tsc`, and adding a dependency means regenerating a lockfile that has already been validated.

## Runtime switch to public Pi and OpenCode Go

Recorded 2026-11-07, after the original implementation. The owner's environment had no `omp` 18.17.0 binary, and that fork is not publicly available. The owner selected the maintained upstream: the public **Pi 1.5.0 CLI** (`@earendil-works/pi-coding-agent`) as the worker binary behind the unchanged `FACTORY_OMP` interface. The adapter (`factory/agents.py`) was updated to Pi's current flags; budgets remain factory-enforced transactionally because Pi no longer takes per-call cost flags. Credentials: the owner uses an **OpenCode Go subscription**, exposed natively in Pi as provider `opencode-go` through `OPENCODE_API_KEY`; the working paid model is `opencode-go/deepseek-v4.1-flash` (the similarly named `opencode/...` Zen route requires prepaid balance and returned 402). The OpenCode CLI itself is not used as the worker runtime.

Free-tier models are not used for factory work and no burst API testing is performed. A scripted A/B measurement performed on 2026-11-08 during diagnosis did include one free-tier model and many sequential calls; that was a diagnostic exception, not factory work, and it should not be repeated as a habit.

## Repository identity and publication

Local project folder: `software-factory`. Package/project identity: Anton Software Factory. GitHub repository: `RafiMM0609/caca`. The owner supplied that repository and explicitly authorized public pushes. Earlier write attempts failed with GitHub 403 errors; access later worked and the code was pushed successfully. The name `caca` does not imply a different product goal or permission to replace existing documentation with a one-line README.
