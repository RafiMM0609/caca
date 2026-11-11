# Worker wall-clock: three fixes from the Toko Roti rebuild

Written 2026-11-11 after rebuilding Toko Roti Sederhana with `design-taste-frontend`. The build
succeeded but took 66 minutes of wall clock for one landing page. This records what the time
actually went to and the three changes made in response.

## What was measured

| | |
| --- | --- |
| Build #1 (`f43d70d0`) | 66.0 min, failed at verification (operator env, not code) |
| Build #2 (`b40ef24b`) | 19.9 min, succeeded, 2/2 acceptance cases passed |

Worker timeline for build #1:

| Task | Duration | Note |
| --- | --- | --- |
| `scaffold-nuxt-app` | 459 s | |
| **`build-homepage` attempt 1** | **1802 s** | hit the 1800 s deadline |
| `add-health-endpoint` | 121 s | but waited **1805 s** idle behind its sibling |
| `build-homepage` attempt 2 | 627 s | rejected by review (3 real design findings) |
| `build-homepage` attempt 3 | 585 s | merged |
| repair | 349 s | `FACTORY_ALLOW_NATIVE` missing; changed nothing |

## Why attempt 1 took 30 minutes

Not the model, and not `npm`. Measured directly on this host:

| Step | Time |
| --- | --- |
| `npm ci` cold cache | 29.4 s |
| `npm ci` warm cache | 16.8 s |
| `npm run build` (Nuxt) | 16.6 s |
| full cold cycle: ci + build + serve + curl | 71.4 s |

`better-sqlite3` ships prebuilt binaries, so there is no compiler step. A shared npm cache saves
**12.6 s per install**, which is under 1% of a 66-minute build. That is not the problem.

The forensics say what it was: `/tmp/bakery-shots/{1440,768,390}.png` timestamped 20:26–20:27,
inside attempt 1 (20:13:45–20:43:48), plus `/tmp/bakery-shots/tiles/` and three
`puppeteer_dev_chrome_profile-*` directories. The worker installed its own browser and was
iterating on the design visually — rebuild, screenshot three viewports, adjust CSS, repeat. Its
own thinking at the cut-off reads *"Let's rebuild and screenshot."*

That loop is what made the output good: the reviewer approved attempt 3 with eight positive
findings, including the palette, the type scale and the reduced-motion guard. The factory simply
had no time budget that acknowledged the loop exists, and the retry told the worker nothing
about why it had been killed.

## The three changes

### 1. A skill-carrying task gets a longer worker clock

`Task.skills` is already the signal that a task is doing design work. A task that carries a
skill may run to `skill_worker_timeout` (default 3600 s) instead of `worker_timeout`
(default 1800 s). Tasks without skills keep the old bound, because a task that only edits an API
route has no reason to explore.

This does not weaken the deadline — it sizes it to the work. Attempts 2 and 3 show what the
worker produces when it is not cut off mid-iteration.

### 2. A retry is told why the previous attempt failed

`ParallelBuilder.build_tasks` already carries reviewer findings into the next attempt, and the
comment there says why: a retry with an identical prompt repeats the same mistake. That reasoning
applies equally to a failure that is not a review — and timeouts were producing
`"feedback": {"build-homepage": []}`, an empty list. The worker restarted blind and re-explored.

Every failed attempt now contributes a feedback line: the reviewer's findings when there are any,
otherwise the recorded error and message, with guidance that fits a timeout (write the final
files before exploring, do not repeat a browser pass that already ran).

### 3. A worker can see the browsers the host already has

`PiWorker.worker_env` gives each worker an isolated `HOME`, which is deliberate: it hides
`~/.ssh`, `~/.aws` and the operator's git identity. The side effect is that
`~/.cache/ms-playwright` is invisible, so a worker that wants to look at its own page installs a
second browser into `/tmp` on every attempt.

The fix points at one directory, not at `HOME`: `PLAYWRIGHT_BROWSERS_PATH` is exported only when
that directory exists, so the worker reuses the browser the tester already uses. Isolation is
unchanged — the worker still cannot see the operator's home, and the variable names a path
containing browser binaries and nothing else.

## Done when

- A task with skills receives `skill_worker_timeout`; a task without skills receives
  `worker_timeout`; both are visible at the `run_task` call, not only in config.
- A timed-out attempt leaves a non-empty feedback line that reaches the next instruction.
- A reviewer rejection still wins over the generic error line, so a real finding is never
  replaced by "the process timed out".
- `PLAYWRIGHT_BROWSERS_PATH` is set when the directory exists and absent when it does not, and
  `HOME` stays isolated either way.
- `uv run ruff check factory tests` and the affected tests pass.

## What this deliberately does not do

- **No change to `worker_concurrency`.** On a 2 GB host, two concurrent Nuxt builds OOM. The
  serialization cost is real (1805 s of idle) but the fix is memory, not a config value.
- **No removal of self-verification.** Measured at 71 s for the full cycle; it is the cheap place
  to catch a defect, before the verifier and a repair worker.
- **No shared npm cache.** 12.6 s per install does not justify the change.
- **No unbounded timeout.** A wedged worker must still die.
