# Architecture and trust boundaries

Browser → authenticated FastAPI API → SQLite WAL → build scheduler → OMP planner/implementer/reviewer → disposable verification container → immutable artifact → deployment scheduler → SSH release helper.

The operator owns the brief, approved plan, verification image and checks. AI output is untrusted. The controller validates paths, limits file size, rejects symlinks, snapshots evidence, and confines execution to a disposable container with read-only source/check mounts, no network, no Docker socket, no application credentials, dropped capabilities, limited memory/CPU/PIDs, and bounded writable tmpfs. Only bounded regular files are extracted from its output.

OMP receives explicitly selected provider variables. Extensions, tools, rules, context discovery and session persistence are disabled. Full source is provided as data, limited to 300 KB. Review can use `FACTORY_REVIEW_MODEL`; otherwise it uses a new invocation of the same model. Approval does not prove correctness; configured verification checks and human acceptance remain necessary.

SQLite records projects, versioned plans, jobs, reserved call budgets, events and deployments. Partial indexes prevent simultaneous jobs within a project; `BEGIN IMMEDIATE` serializes claims. Separate flock locks enforce one scheduler per lane. Heartbeats expose availability. Cancellation kills local process groups and removes verification containers. Restart reconciliation does not replay side effects.

Memorix stores optional project knowledge in a separate trusted memory directory. Generated projects cannot provide its configuration. Memory is bounded and advisory; failures are visible as events. Source, tests and workflow records remain authoritative.

Production release is a separate lane and token. Each project's target must be configured explicitly. The remote helper validates compressed and expanded sizes, rejects traversal and links, checks SHA-256, uses a per-target lock, changes a symlink atomically, and checks application health. An interrupted remote journal requires explicit recovery. Code rollback cannot undo data mutations.

Current limitations: one operator, no tenant authorization, no task-parallel merge queue, no general coding-shell tools, no automatic repair after verification failure, no binary source generation, no automatic provider-quota scheduling, and no automatic application database provisioning/migration. The dashboard is a lightweight static application; generated Nuxt apps are a separate concern.
