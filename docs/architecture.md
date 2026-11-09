# Architecture and trust boundaries

Current path: browser → authenticated FastAPI API → SQLite WAL → build scheduler → Pi planner → a **DAG of tasks** → **parallel Pi harness workers, one git worktree each** → review of each diff → **merge barrier** into the product branch → configured native or Docker verifier (build, operator checks, API/browser tester) → a tested artifact that is the product's codebase plus a generated README and its own git history. Studio edits create a separate draft and return through a new verification job.

Deployment is deferred in the current product scope. Its separate scheduler and SSH helper remain as legacy capability and are described below for maintenance context.

## The execution model

`factory/models.py` holds the plan as a DAG. `ready_set(completed)` returns the tasks whose dependencies have all merged, and `execution_order()` derives a topological order while rejecting cycles, self-dependencies, unknown ids and duplicate ids. Scheduling comes from the graph, never from list position.

`factory/parallel.py` drives it:

1. Compute the ready wave. Spawn one worker per task, bounded by `FACTORY_WORKER_CONCURRENCY` (default 3, max 16).
2. Each worker runs in its own worktree on its own branch, created by `factory/products.py::worktree_add`.
3. The controller commits the worker's worktree, then the **diff against the wave base** goes to review.
4. When the whole wave has finished, the barrier opens and accepted branches merge into the product branch one at a time.
5. A conflicting merge is aborted — the target is never left half-merged — a fixer worker resolves the integration on a detached worktree of the target, and the wave retries. Persistent failure fails the job; partial work is never published.

Why a barrier: two workers may touch the same file. Merging as each worker finished would mean a worker completing against a base that no longer exists. Waiting for the wave keeps every worker's premise stable and makes merge order explicit instead of racy.

Workers are recorded in SQLite (`workers`): task, branch, worktree, base commit, result commit, attempts and outcome. That is what makes "the planner assigned work to workers" real data rather than a dashboard decoration.

## Products own their code

Each product has an independent git repository at `data/products/<project-id>`. SQLite holds workflow state; the product repository holds source, history and diffs. A new project's repository starts empty, which keeps "build the application from scratch" legitimate rather than silently forcing a template. The Studio starter writes its files into that repository through a draft branch.

Product repositories run with `GIT_CONFIG_NOSYSTEM`, hooks disabled, gpg signing off and prompts disabled, and commits are attributed to a factory identity. A product repository therefore never reads the operator's git configuration or credentials. A stale worktree still attached to a branch must be removed before that branch is deleted or reused, because git refuses to recreate a worktree for an existing branch.

## Two Pi interfaces

`PiWorker` (`factory/agents.py`) is the coding harness used for plan tasks. Tools stay enabled; it reads, runs and edits inside its worktree. It runs with `--no-session` and with extensions, skills, MCP, prompt templates, themes and context-file discovery disabled, so it cannot pull configuration from the operator's environment or the product repository.

`PiAgent` remains a **tool-free structured JSON client** for planning, test preparation and review. It returns a validated Pydantic object that the controller consumes as data — it does not write files. The plan becomes scheduling input (`ready_set`), the test suite becomes a runner input, and the review verdict becomes an accept/reject decision. Using it for implementation was removed deliberately: the runtime injects a coding-agent preamble, so a tool-free model emits markup that looks like a tool call and the stage failed with no usable output. Note the naming: `PiAgent` is not an agent in the tool-using sense, only a generation client; `PiWorker` is the harness.

Review can use `FACTORY_REVIEW_MODEL`; otherwise it uses the configured model. Approval does not prove correctness — configured verification checks and human acceptance remain necessary.

## Worker isolation, and what it does not cover

Each worker gets a temporary `HOME`, `PI_CODING_AGENT_DIR` and `XDG_*` directories pointing inside a per-worker temporary directory. As a result `~/.ssh`, `~/.aws` and the operator's git identity are unreachable, and `FACTORY_OPERATOR_TOKEN`, `FACTORY_RELEASE_TOKEN` and the operator's git identity variables are removed from the environment even when exported.

**Provider credentials are deliberately retained**, because the model call requires them. A worker able to run `env` can therefore observe the provider key. Native execution is also not a filesystem sandbox: nothing stops a determined worker from reading other files reachable by the OS account. This is accepted for trusted local single-operator use and must not be described as container-grade isolation.

## Verification and gates

The verifier runs administrator-owned `commands` argv arrays (typically install and build) in a disposable copy of the product source, then optional operator-owned `checks_argv` scripts resolved against the configured `checks_dir`. The active Nuxt profile runs two: `checks/secret_scan.py`, which blocks credential-shaped values and the
exact values of configured secrets from appearing in a built artifact, and `checks/js_syntax.py`, which
parses every shipped JavaScript file with `node --check` so an artifact that does not parse cannot be
published. A native profile that names a `checks_dir` without any `checks_argv` is rejected at startup,
because that combination runs nothing while appearing validated. After the build, when a tester suite exists, the verifier starts the application with `serve_argv`, waits for `readiness_path`, and executes the API and Playwright cases.

The tester contract (`factory/testing.py`) supports `GET`/`HEAD`/`POST`/`PUT`/`PATCH`/`DELETE` with JSON assertions, and browser `goto`, `reload`, `fill`, `select`, `press`, `click`, `expect_text`, `expect_value`, `expect_visible`, `expect_hidden`, `expect_count` and `expect_url`. A browser case may run `request` actions as setup before `goto` and nothing but browser actions after it. `expect_text` matches case-insensitively across any matched element, `expect_value` reads a field's value, and `click`/`fill`/`select` act on the first match rather than failing under strict mode. Browser cases fail on uncaught page exceptions and on console errors; noise from the runner's own aborted external requests is filtered, and a reviewed case may opt out via `allow_console_errors`. Failures block publication.

In Docker profiles execution is confined to a disposable container with read-only source/check mounts, no network, no Docker socket, no application credentials, dropped capabilities, limited memory/CPU/PIDs, and bounded writable tmpfs. Only bounded regular files are extracted. Native profiles instead run in a disposable source copy under the local OS account with time/output/process cleanup but without filesystem, network or CPU/RAM isolation. Note that `npm ci` needs network access, which is why the Nuxt profile is native rather than Docker.

## Durable state and scheduling

SQLite records projects, versioned plans, jobs, reserved call budgets, events, workers and deployments. Partial indexes prevent simultaneous jobs within a project; `BEGIN IMMEDIATE` serializes claims. Separate flock locks enforce one scheduler per lane. Heartbeats expose availability. Cancellation kills local process groups and removes verification containers. Restart reconciliation does not replay side effects.

Model-call reservations apply to planner, review and tester-preparation calls. Harness workers are separate processes and are not metered per call; their bound is `FACTORY_WORKER_CONCURRENCY` and `FACTORY_AGENT_TIMEOUT`.

Memorix stores optional project knowledge in a separate trusted memory directory. Generated projects cannot provide its configuration. Memory is bounded and advisory; failures are visible as events. Source, tests and workflow records remain authoritative.

## How a rejected task is repaired

A reviewer's `findings` are carried into the next attempt's prompt as `PREVIOUS ATTEMPT FEEDBACK`, quoted verbatim. Without that, a retry ran with a prompt identical to the one that had just failed: the worker repeated the same mistake and the reviewer re-read the same diff. This was found live — a task was rejected twice for an unrequested `better-sqlite3` dependency, the worker was never told, and the third review approved the unchanged problem.

If a task is approved on a later attempt after an earlier rejection, the verdict stands but a `review.flaky_approval` event records the disagreement together with the earlier findings, and `worker.detail.flaky_approval` is set. That is a warning rather than a block because the worker may genuinely have fixed the issue; the operator should confirm rather than discover it in two review files.

## What is published

The artifact is the product's source tree, assembled by `factory/artifact.py`: every source file, a `README.md` rendered from the approved plan and profile, and a fresh single-commit `git init` history. Build output is deliberately excluded; it is regenerated by the commands the README documents. `artifact_dir` survives only as a build sanity check, proving the configured commands produced something. A copied product `.git` is never shipped, because a hand-off must not carry the operator's branches, stale worktrees or remote configuration.

## Trust boundaries in one sentence

The operator owns the brief, the approved plan, the verification commands, the checks and the tokens. Worker output is untrusted code: it is reviewed as a diff, built and tested in a controlled runner, and only a passing artifact is published.

Production release, where configured, is a separate lane and token. Each project's target must be configured explicitly. The remote helper validates compressed and expanded sizes, rejects traversal and links, checks SHA-256, uses a per-target lock, changes a symlink atomically, and checks application health. An interrupted remote journal requires explicit recovery. Code rollback cannot undo data mutations.

Current limitations: one operator with no tenant authorization (products are separated by repository and directory, not by login); no filesystem sandbox for native workers; no merge queue beyond the per-wave barrier; no automatic repair after a verification failure; no binary source generation; no automatic provider-quota scheduling; no automatic application database provisioning or migration. The dashboard renders waves, per-worker outcome, branch name and review verdict from the `workers` table; it does not show worktree paths or worktree state. The dashboard is a lightweight static application; generated applications are a separate concern.
