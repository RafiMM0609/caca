# Architecture and trust boundaries

Current path: browser → authenticated FastAPI API → SQLite WAL → build scheduler → OMP planner/implementer/reviewer with parallel tester preparation → configured native or Docker verifier → tested artifact. Studio edits create a separate draft and return through a new verification job.

Deployment is deferred in the current product scope. Its separate scheduler and SSH helper remain as legacy capability and are described below for maintenance context.

The operator owns the brief, approved plan, verification image and checks. AI output is untrusted. The controller validates paths, limits file size, rejects symlinks and snapshots evidence. In Docker profiles it confines execution to a disposable container with read-only source/check mounts, no network, no Docker socket, no application credentials, dropped capabilities, limited memory/CPU/PIDs, and bounded writable tmpfs. Only bounded regular files are extracted from container output. Native profiles instead execute in a disposable source copy under the local OS account, with time/output/process cleanup controls but without filesystem, network or CPU/RAM isolation. See [Studio and tester](studio-and-tester.md) for the current local journey and independent API/browser verification.

OMP receives explicitly selected provider variables. Extensions, tools, rules, context discovery and session persistence are disabled. Full source is provided as data, limited to 300 KB. Review can use `FACTORY_REVIEW_MODEL`; otherwise it uses a new invocation of the same model. Approval does not prove correctness; configured verification checks and human acceptance remain necessary.

SQLite records projects, versioned plans, jobs, reserved call budgets, events and deployments. Partial indexes prevent simultaneous jobs within a project; `BEGIN IMMEDIATE` serializes claims. Separate flock locks enforce one scheduler per lane. Heartbeats expose availability. Cancellation kills local process groups and removes verification containers. Restart reconciliation does not replay side effects.

Memorix stores optional project knowledge in a separate trusted memory directory. Generated projects cannot provide its configuration. Memory is bounded and advisory; failures are visible as events. Source, tests and workflow records remain authoritative.

Production release is a separate lane and token. Each project's target must be configured explicitly. The remote helper validates compressed and expanded sizes, rejects traversal and links, checks SHA-256, uses a per-target lock, changes a symlink atomically, and checks application health. An interrupted remote journal requires explicit recovery. Code rollback cannot undo data mutations.

Current limitations: one operator, no tenant authorization, no task-parallel merge queue, no general coding-shell tools, no automatic repair after verification failure, no binary source generation, no automatic provider-quota scheduling, and no automatic application database provisioning/migration. The dashboard is a lightweight static application; generated Nuxt apps are a separate concern.
