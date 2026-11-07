# Visual editing and independent tester

## Supported journey

The factory remains local and build-focused. No deployment is triggered by these actions.

1. Configure the `nuxt-studio` profile, open the local dashboard, and create a project.
2. For an empty project, **Create & test Studio starter** initializes the included Nuxt/Content/Studio application and verifies it. Alternatively, have the planner and workers produce an application compatible with that profile.
3. On a verified project, **Open editable draft** copies its source into a separate workspace, installs locked dependencies, and starts a loopback-only Nuxt dev server. Only one Studio preview runs at a time; it expires after 30 minutes.
4. The dashboard embeds the application on a separate local origin. Use its Studio toggle, open `site.json`, and edit fields such as brand, hero title, description, button text, accent color and features. The official Studio module provides the form UI and live preview. You may also open it in a new tab.
5. **Review changes** stops the preview to freeze the draft and shows before/after content. **Apply & test** imports content as a new source revision and queues verification. It does not ask an implementer to regenerate the edited UI.
6. Independent tests and the build must pass before the new artifact becomes current. The previous verified source/artifact remains available. A stale draft is rejected instead of overwriting a newer revision. Stopped drafts are retained and can be reopened; discarding is explicit.

This implements content-driven UI editing, not arbitrary Vue source editing or a general drag-and-drop app builder. Only Markdown, JSON and YAML under `content/` are imported. The first version does not import uploaded media or changes to application logic/configuration. The starter's theme and fields are deliberately exposed through a Nuxt Content schema. Additional components need explicit editable props/schema.

Nuxt Studio is enabled in development and disabled for production builds in the starter. No Git publishing, production OAuth, or external Studio account is needed for local editing. Preview and Studio mutations require the factory to be accessed through localhost. Remote browser access to the factory is not enough: `127.0.0.1` in the browser refers to that browser's machine. Do not expose unauthenticated Nuxt development servers to a network.

## Local Linux setup

Requires Node 24 (tested with 24.22.1), npm, Python 3.12+, Git, uv, the installed Pi 1.5.0 CLI (`npm i -g @earendil-works/pi-coding-agent`), and a configured provider credential for live agent work.

```sh
uv sync --frozen
uv run playwright install chromium
uv run factory init --data ./data
set -a
. ./data/secrets.env
set +a
export FACTORY_CONFIG="$PWD/examples/config-studio-linux.json"
export FACTORY_ALLOW_NATIVE=1
export FACTORY_OMP=/absolute/path/to/pi
export FACTORY_MODEL=provider/model
# Optional: separate model choices for reviewer and tester.
# export FACTORY_REVIEW_MODEL=provider/model
# export FACTORY_TESTER_MODEL=provider/model
uv run factory api
# Another terminal, same environment:
uv run factory scheduler --lane build
```

`FACTORY_CHROMIUM` optionally selects an already installed Chromium executable. `FACTORY_NPM_CACHE` optionally selects a shared local npm cache to avoid repeatedly downloading dependencies. Provider and factory tokens are not inherited by generated applications. Native execution still has the local OS account's filesystem/network authority and is not a sandbox. Use it only for trusted local work.

The starter is pinned to Nuxt 4.6.4, Nuxt Content 3.16.2 and Nuxt Studio 1.8.0. Its lockfile excludes npm-marked extraneous packages that caused cross-platform `npm ci` failures. Verification tests the materialized `release/server/index.mjs` output that is subsequently packaged; internal Nitro links are resolved only within the build directory and exported as regular files.

## Parallel tester design

The planner includes `test_flows` alongside implementation tasks:

```json
{
  "id": "contact-journey",
  "kind": "browser",
  "goal": "A visitor can submit a valid demo contact enquiry.",
  "steps": [
    "Visit / and confirm [data-testid=hero-title] is visible",
    "Fill #email with person@example.test",
    "Click button[type=submit]",
    "Expect [data-testid=contact-result] to contain the documented confirmation"
  ]
}
```

For tester-enabled profiles, an independent Pi tester invocation starts concurrently with implementation. It receives the planner's flow contract rather than the worker's code and returns a bounded declarative suite. The controller validates flow coverage, case kinds, selectors, app-relative URLs and the presence of browser assertions. It saves the suite outside the generated application source. The tester has its own optional model setting and shares the job's atomically reserved model-call budget.

Supported cases currently include HTTP methods with JSON bodies, expected status and optional exact JSON response assertions; and browser navigation, fill, click, visible/text assertions. API requests use the locally launched application URL and do not follow redirects. Browser HTTP requests are restricted to that app origin; service workers and WebSockets are blocked during testing. Arbitrary generated test JavaScript is not executed by this acceptance runner.

Once worker changes pass code review and configured build steps succeed, the verifier starts the application using administrator-configured `serve_argv`. It waits for the configured readiness endpoint, executes API and Playwright browser cases, records a report and screenshots, and terminates the app process group. Failure blocks artifact publication. The dashboard shows tester preparation, readiness wait, execution, result and evidence.

The implementation is two concurrent model invocations at most: one implementer/reviewer lane plus one tester-preparation lane. Test execution follows application readiness. This is not a general distributed multi-agent scheduler. Interrupted jobs require inspection; no background model task silently resumes after restart.

## Honest limits

- Test quality still depends on the approved flows and assertions. Coverage validation is structural, not proof that every requirement is tested.
- This version does not independently provision databases, test real SAP/Microsoft services, authenticate to external providers, generate arbitrary browser code, or automatically repair failing acceptance tests.
- Tests must use local/mock data. The example contact endpoint validates input and does not send email.
- Third-party calls, cross-origin authentication and WebSockets are deliberately outside the current browser runner's supported contract.
- A native application runs under the local account and can access that account's files. Separate test files are an orchestration boundary, not an OS security boundary.
- Live Pi and Memorix still need provider credentials and their own acceptance tests. Scripted model responses used in tests must not be described as a live AI run.
- Binary image uploads are not part of draft import. Reusable layouts beyond the supplied starter require additional schemas/components.

## Validation commands

```sh
uv run pytest -q
node --test tests/workers.test.cjs
# Real Nuxt installation/build, embedded Studio editing, and API/browser retest:
FACTORY_ALLOW_NATIVE=1 uv run python scripts/validate_studio_journey.py
```

The last command is an opt-in local integration journey that installs npm dependencies and launches Chromium. It uses deterministic tester responses; all Nuxt/editor/build/HTTP/browser operations are real. It writes report/screenshots under ignored `data/validation/`, not to Git.

Primary capability references: [Nuxt Studio module](https://nuxt.com/modules/studio), [Studio editing documentation](https://nuxt.studio/content), [official source](https://github.com/nuxt-content/nuxt-studio). Checked 2026-11-07.

## Executed validation result (2026-11-07)

- 58 Python tests passed, including actual HTTP/Chromium tests and concurrent tester preparation; 2 Docker tests skipped.
- 8 JavaScript worker-state tests passed; lint and JavaScript syntax checks passed.
- The real dashboard journey passed: create project, initialize Nuxt starter, clean npm installation, build, 3 API/browser cases, embedded Studio field editing, review diff, apply revision 2, rebuild, and all 3 API/browser cases again. The original artifact was retained. Desktop/mobile screenshots and a machine-readable report were saved locally under ignored `data/validation/`.
- Tester model responses were scripted test doubles. Nuxt Studio, npm, application servers, HTTP requests and Chromium interactions were real. Live OMP and Memorix remain unverified.
