# Knowledge for continuing agents

This folder preserves the reasoning and project history that cannot be inferred from code alone. Start at the root [AGENTS.md](../../AGENTS.md).

| Document | Purpose |
| --- | --- |
| [Origin and decisions](origin-and-decisions.md) | Why the factory exists, which choices the owner made, and how scope evolved |
| [Delivery plan](plan.md) | Gap analysis and the phased route from today to a dependable factory |
| [Serve lane plan](plan-serve-lane.md) | Running a built product on this machine behind the existing tunnel |
| [Inline plan editing plan](plan-edit-inline.md) | Editing a plan so a wrong feature can be fixed, without rebuilding the planner |
| [Design skills plan](design-skills-plan.md) | Making worker output look deliberate rather than generic: what Pi can load, and where each check belongs |
| [Handoff](handoff.md) | Current code map, last validation evidence, gaps and continuation checklist |
| [Architecture](../architecture.md) | Components, the parallel worker and merge model, and trust boundaries |
| [Studio and tester](../studio-and-tester.md) | The editor journey and the tester's contract |
| [Operations](../operations.md) | Linux operations, data layout, profiles and legacy deployment |
| [Linux validation](../validation-linux.md) | Native-runner validation and what the tests actually exercise |

Maintain these documents when behavior or priorities change. Prefer dated facts and links to source/evidence. Do not copy secrets, whole transcripts, model reasoning traces or temporary debugging output here. Mark assumptions and unverified integrations explicitly.

## Current checkpoint (2026-11-08)

- The execution model is **planner-driven parallel Pi workers**: a plan DAG, one Pi harness worker per ready task in its own git worktree, review of each diff, and a merge barrier into a per-product git repository.
- Each product owns a repository at `data/products/<project-id>`. SQLite holds workflow state; the repository holds client code and history.
- Check `git status` and `git log` rather than trusting a commit identifier written here.
- **The loop closes as of 2026-11-09.** A single `build` job ran a complete brief through the planner, four Pi workers (two concurrent), the merge barrier, the build and three independent acceptance cases to a published artifact in 15.3 minutes; the artifact was then extracted and built outside the factory. Phase 0 of the [delivery plan](plan.md) is closed. Phase 1 — a repair loop for failing gates — is the next milestone, motivated by the finding that the most likely way a run dies is a plan that contradicts itself after an operator answers a planner question.

Earlier checkpoints: the implementation before the parallel-worker change was pushed as `943f044` to the public `main` branch of [RafiMM0609/caca](https://github.com/RafiMM0609/caca). Commit identifiers here are historical checkpoints, not assertions about the current HEAD.

## Where things live

| Concern | Location |
| --- | --- |
| Plan DAG and readiness | `factory/models.py` |
| Product repositories and worktrees | `factory/products.py` |
| Worker waves, merge barrier, fixer | `factory/parallel.py` |
| Pi harness worker and JSON client | `factory/agents.py` |
| Workflow and Studio import | `factory/engine.py` |
| Operator-owned checks | `checks/` |
| Tester contract | `factory/testing.py` |
| Tests | `tests/` |
