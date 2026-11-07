# Architecture and trust boundaries

Current path: browser → authenticated FastAPI API → SQLite WAL → build scheduler → Pi planner → a DAG of tasks → parallel Pi harness workers, one git worktree each → review of each diff → merge barrier into the product branch → configured native or Docker verifier → tested artifact. Studio edits create a separate draft and return through a new verification job.

## Products, workers and the merge barrier

Each product owns an independent git repository at `data/products/<product>`. A worker is one Pi harness invocation for one plan task, and it runs in its own worktree on its own branch, so parallel workers cannot see each other's files.

The planner's plan is a DAG. Tasks whose dependencies are satisfied form a wave and run concurrently, bounded by `FACTORY_WORKER_CONCURRENCY` (default 3). Review sees the worker's **diff**, not the worker's own description of its work.

Merging is a barrier, not a race. Every worker in a wave finishes first, then accepted branches merge into the product branch one at a time. A worker therefore never finishes against a base that no longer exists. A conflicting merge is aborted (the target is never left half-merged), a fixer worker is spawned on a detached worktree of the target to integrate the intent by hand, and the wave is retried. If resolution still fails after bounded attempts the job fails rather than publishing partial work.

Workers are recorded in SQLite (`workers`), which is what makes the dashboard's planner-to-worker assignment real rather than decorative.

Deployment is deferred in the current product scope. Its separate scheduler and SSH helper remain as legacy capability and are described below for maintenance context.

The operator owns the brief, approved plan, verification image and checks. AI output is untrusted. The controller validates paths, limits file size, rejects symlinks and snapshots evidence. In Docker profiles it confines execution to a disposable container with read-only source/check mounts, no network, no Docker socket, no application credentials, dropped capabilities, limited memory/CPU/PIDs, and bounded writable tmpfs. Only bounded regular files are extracted from container output. Native profiles instead execute in a disposable source copy under the local OS account, with time/output/process cleanup controls but without filesystem, network or CPU/RAM isolation. See [Studio and tester](studio-and-tester.md) for the current local journey and independent API/browser verification.

Two Pi interfaces exist for two different jobs. **`PiWorker`** is the coding harness used for plan tasks: tools stay enabled and it edits its own worktree, with `--no-session`, no extensions, no MCP and no project resource discovery. **`PiAgent`** remains a tool-free structured JSON client for planning, test preparation and review. Using the JSON client for implementation was removed because the runtime injects a coding-agent preamble, so a tool-free model would emit markup resembling a tool call.

Worker isolation is real but bounded. Each worker gets a temporary `HOME`, `PI_CODING_AGENT_DIR` and XDG directories, so `~/.ssh`, `~/.aws` and the operator's git identity are unreachable. `FACTORY_OPERATOR_TOKEN` and `FACTORY_RELEASE_TOKEN` are removed from the environment even when exported. **Provider credentials are deliberately retained** because the model call requires them: a worker able to run `env` can observe the provider key. Native execution is not a filesystem sandbox, and that exposure is accepted here rather than hidden.

Review can use `FACTORY_REVIEW_MODEL`; otherwise it uses the configured model. Approval does not prove correctness; configured verification checks and human acceptance remain necessary.

SQLite records projects, versioned plans, jobs, reserved call budgets, events and deployments. Partial indexes prevent simultaneous jobs within a project; `BEGIN IMMEDIATE` serializes claims. Separate flock locks enforce one scheduler per lane. Heartbeats expose availability. Cancellation kills local process groups and removes verification containers. Restart reconciliation does not replay side effects.

Memorix stores optional project knowledge in a separate trusted memory directory. Generated projects cannot provide its configuration. Memory is bounded and advisory; failures are visible as events. Source, tests and workflow records remain authoritative.

Production release is a separate lane and token. Each project's target must be configured explicitly. The remote helper validates compressed and expanded sizes, rejects traversal and links, checks SHA-256, uses a per-target lock, changes a symlink atomically, and checks application health. An interrupted remote journal requires explicit recovery. Code rollback cannot undo data mutations.

Current limitations: one operator and no tenant authorization (products are separated by repository and directory, not by login). Workers are not sandboxed beyond the isolated environment above. There is no merge queue beyond the per-wave barrier, no binary source generation, no automatic provider-quota scheduling, and no automatic application database provisioning/migration. The dashboard does not yet render worktree or wave state. The dashboard is a lightweight static application; generated applications are a separate concern.
