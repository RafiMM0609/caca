# Visual editing and independent tester

## Supported journey

The factory remains local and build-focused. No deployment is triggered by these actions.

1. Configure the `nuxt-studio` profile, open the local dashboard, and create a project.
2. For an empty project, **Create & test Studio starter** initializes the included Nuxt/Content/Studio application and verifies it. Alternatively, let the planner and Pi workers produce an application compatible with that profile.
3. On a verified project, **Open editable draft** copies its source into a separate workspace, installs locked dependencies, and starts a loopback-only Nuxt dev server. Only one Studio preview runs at a time; it expires after 30 minutes.
4. The dashboard embeds the application on a separate local origin. Use its Studio toggle, open `site.json`, and edit fields such as brand, hero title, description, button text, accent color and features. The official Studio module provides the form UI and live preview. You may also open it in a new tab.
5. **Review changes** stops the preview to freeze the draft and shows before/after content. **Apply & test** imports content as a new source revision and queues verification. It does not ask a worker to regenerate the edited UI.
6. The applied draft is imported onto its own branch of the product repository and merged **only after** verification passes. Independent tests and the build must succeed before the new artifact becomes current, so a failed edit cannot overwrite the previously verified artifact. A stale draft is rejected instead of overwriting a newer revision. Stopped drafts are retained and can be reopened; discarding is explicit.

This implements content-driven UI editing, not arbitrary Vue source editing or a general drag-and-drop app builder. Only Markdown, JSON and YAML under `content/` are imported. The first version does not import uploaded media or changes to application logic/configuration. The starter's theme and fields are deliberately exposed through a Nuxt Content schema. Additional components need explicit editable props/schema.

Nuxt Studio is enabled in development and disabled for production builds in the starter. No Git publishing, production OAuth, or external Studio account is needed for local editing. Preview and Studio mutations require the factory to be accessed through localhost. Remote browser access to the factory is not enough: `127.0.0.1` in the browser refers to that browser's machine. Do not expose unauthenticated Nuxt development servers to a network.

## Local Linux setup

Requires Node 24, npm, Python 3.12+, Git, uv, the installed Pi 1.5.0 CLI (`npm i -g @earendil-works/pi-coding-agent`), and a configured provider credential for live worker work.

```sh
uv sync --frozen
uv run playwright install chromium
uv run factory init --data ./data
set -a
. ./data/secrets.env
set +a
export FACTORY_CONFIG="$PWD/examples/config-studio-linux.json"
export FACTORY_ALLOW_NATIVE=1
export FACTORY_PI=/absolute/path/to/pi
export FACTORY_MODEL=provider/model
# Optional: separate model choices for reviewer and tester.
# export FACTORY_REVIEW_MODEL=provider/model
# export FACTORY_TESTER_MODEL=provider/model
# Optional: how many Pi workers may run at once (default 3, max 16).
# export FACTORY_WORKER_CONCURRENCY=3
uv run factory api
# Another terminal, same environment:
uv run factory scheduler --lane build
```

`FACTORY_CHROMIUM` optionally selects an already installed Chromium executable. `FACTORY_NPM_CACHE` optionally selects a shared local npm cache. Provider credentials reach Pi workers because the model call needs them; factory tokens do not. Each worker still gets an isolated temporary `HOME`, so `~/.ssh` and `~/.aws` are unreachable. Native execution has the local OS account's filesystem and network authority and is **not** a sandbox.

The starter is pinned to Nuxt 4.6.4, Nuxt Content 3.16.2 and Nuxt Studio 1.8.0. Its lockfile excludes npm-marked extraneous packages that caused cross-platform `npm ci` failures. Verification tests the materialized `release/server/index.mjs` output that is subsequently packaged; internal Nitro links are resolved only within the build directory and exported as regular files.

## How implementation and testing run together

The planner produces a task DAG plus `test_flows`. Two things then happen concurrently:

- **Workers implement.** Every task whose dependencies have merged runs as its own Pi harness worker in its own git worktree, bounded by `FACTORY_WORKER_CONCURRENCY`. Each worker's diff is reviewed before it may merge. When the whole wave finishes, accepted branches merge into the product branch one at a time; a conflict spawns a fixer worker and the wave retries.
- **The tester prepares.** An independent Pi invocation receives the planner's flow contract — not the worker's code — and returns a declarative suite.

```json
{
  "id": "contact-journey",
  "kind": "browser",
  "goal": "A visitor can submit a valid demo contact enquiry.",
  "steps": [
    "Visit / and confirm the hero title is visible",
    "Fill the email field with person@example.test",
    "Click the submit button",
    "Expect the result element to contain the documented confirmation"
  ],
  "selectors": {
    "hero": "[data-testid=\"hero-title\"]",
    "email": "#email",
    "submit": "button[type=\"submit\"]",
    "result": "[data-testid=\"contact-result\"]"
  }
}
```

The `selectors` map is **required** for every `browser` flow and must be non-empty; a browser flow that declares no selectors is rejected, because it forces the tester to invent them. It is the single shared contract: the worker is told to render exactly these selectors, and the tester may only assert against them. Declare a selector for every state the flow observes, including fields shown while a row is being edited. Both API and browser flows may use semantic names; only the values are CSS.


The controller validates flow coverage, case kinds, selectors, app-relative URLs and the presence of browser assertions. The suite is saved outside the generated application source. The tester has its own optional model setting and shares the job's atomically reserved model-call budget.

Once worker changes pass review and the configured build steps and operator checks succeed, the verifier starts the application using administrator-configured `serve_argv`, waits for the readiness endpoint, executes the API and Playwright cases, records a report and screenshots, and terminates the app process group. Failure blocks artifact publication. The dashboard shows tester preparation, readiness wait, execution, result and evidence.

## Supported test contract

API cases: `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE` with a JSON body, expected status and optional exact JSON response assertion. `HEAD` cases may not assert a body. Requests use the locally launched application URL and do not follow redirects.

A browser case may begin with **optional `request` actions as setup**, then must navigate with `goto`, and after `goto` may use browser actions only. Setup requests exist because a UI case often needs a record to exist before the page is opened — otherwise a case could only ever test an empty page. A `request` after `goto` is rejected, so a UI assertion can never be satisfied by an API response.

Browser actions are `reload`, `fill`, `select`, `press`, `click`, `expect_text`, `expect_value`, `expect_visible`, `expect_hidden`, `expect_count` and `expect_url`.

**Authentication.** A `request` action takes a `headers` map, and a whole case takes one too:
`request` headers cover that HTTP call, case-level headers are applied to every browser request
in the case (via Playwright's `extra_http_headers`). Without them any application behind a token
was untestable — cookies persisted within a case, which covered a flow that starts by logging in,
but not a bearer token or API key the caller must supply. Header names must match
`[A-Za-z0-9][A-Za-z0-9!#$%&'*+.^_`|~-]*` and values may not contain control characters or
newlines, because these reach an outbound request and a newline would permit header injection. At least one assertion must appear after `goto`. Expected URLs must be app-relative. Browser HTTP requests are restricted to the app origin; service workers and WebSockets are blocked during testing. Arbitrary generated test JavaScript is not executed by this runner.

Assertion semantics, which are deliberate and were each fixed against a real failing suite:

- `expect_text` matches case-insensitively against **any** element the selector matches, and requires non-empty text. `.first`-only matching failed a correct page whose copy sat in the second of three cards; an empty pattern asserts nothing and is rejected.
- `expect_value` compares a form field's **value** exactly. An `<input>` has no text content, so `expect_text` cannot observe it — use `expect_value` for "the field is empty after submit".
- `expect_visible` requires every matched element to be visible, and fails on an empty match.
- `click`, `fill` and `select` act on the **first** match. Playwright's strict mode raised when a selector matched several rows, so "delete the first row" threw instead of acting; a missing element still fails, and `expect_count` is how you assert how many exist.

A browser case fails on an uncaught page exception or a console error. Console noise caused by the runner's own aborted external requests is filtered. A case whose page has a known, reviewed console error may set `allow_console_errors: true`; prefer fixing the error.

Operator-owned checks run after the build and before testing. The active Nuxt profile runs `checks/secret_scan.py`, which blocks credential-shaped values and the exact values of configured secrets from appearing in a built artifact.

## Honest limits

- Test quality still depends on the approved flows and assertions. Coverage validation is structural, not proof that every requirement is tested.
- The concurrency model is per-plan waves, not a general distributed scheduler. There is no merge queue beyond the per-wave barrier and no cross-product scheduling.
- This version does not independently provision databases, test real SAP/Microsoft services, authenticate to external providers, generate arbitrary browser code, or automatically repair failing acceptance tests.
- Tests must use local/mock data. The example contact endpoint validates input and does not send email.
- Third-party calls, cross-origin authentication and WebSockets are deliberately outside the browser runner's contract.
- A native application and its workers run under the local account. The isolated `HOME` removes obvious secret paths, but this is an orchestration boundary, not an OS security boundary.
- Live Memorix still needs provider setup and its own acceptance tests. Scripted model responses used in tests must not be described as a live AI run.
- Binary image uploads are not part of draft import. Reusable layouts beyond the supplied starter require additional schemas/components.
- Lint and TypeScript typecheck are not part of validation yet.

## Validation commands

```sh
uv run pytest -q
node --test tests/workers.test.cjs
# Real model worker call (needs provider credentials):
FACTORY_LIVE_WORKER=1 FACTORY_MODEL=provider/model uv run pytest tests/test_worker.py -q
# Real Nuxt installation/build, embedded Studio editing, and API/browser retest:
FACTORY_ALLOW_NATIVE=1 uv run python scripts/validate_studio_journey.py
```

The last command is an opt-in local integration journey that installs npm dependencies and launches Chromium. It uses deterministic tester responses; all Nuxt/editor/build/HTTP/browser operations are real. It writes report/screenshots under ignored `data/validation/`, not to Git.

Primary capability references: [Nuxt Studio module](https://nuxt.com/modules/studio), [Studio editing documentation](https://nuxt.studio/content), [official source](https://github.com/nuxt-content/nuxt-studio). Checked 2026-11-07.

## Executed validation result

**2026-11-09 (current).** 230 Python tests passed, 3 skipped (164 collected); 15 JavaScript worker-state tests passed; ruff and `node --check` clean.

**2026-11-07 (before workers became parallel).** 58 Python tests passed, 2 Docker tests skipped; 8 JavaScript worker-state tests passed. The real dashboard journey passed: create project, initialize Nuxt starter, clean npm installation, build, 3 API/browser cases, embedded Studio field editing, review diff, apply revision 2, rebuild, and all 3 API/browser cases again, with the original artifact retained. Tester model responses were scripted test doubles.

**2026-11-08 (parallel worker lane).** The full journey was re-run under the parallel worker lane and passed (`scripts/validate_studio_journey.py`, `FACTORY_ALLOW_NATIVE=1`, real Chromium, exit 0): dashboard create → real `npm ci` + `nuxt build` (907-file artifact) → 3 API/browser cases → embedded Nuxt Studio edit → diff → apply → real rebuild at revision 2 → 3 cases re-run. Both jobs ended `succeeded`, `report.json` is `passed` with `console_errors: []` and `page_errors: []`, and the edit is present in revision 2's `content/site.json`. The operator check ran (`CHECK 1 PASS`), which also proves the relative `checks_dir` resolves.

Live runs with `opencode-go/deepseek-v4.1-flash` also produced: two concurrent workers merging a product in 6.8s; a full pipeline passing build, secret scan, API and browser cases; and a fixer resolving a real merge conflict in 3 of 3 runs. A Studio regression where applied drafts never reached the product source was found and fixed, now covered by tests.

**Scope of that result.** The journey covers one starter-shaped project whose planner and tester responses are scripted test doubles; npm, the Nitro server, HTTP and Chromium are real. It does not exercise live model workers producing a Nuxt application from a brief. The AI half of this path is still a double.
