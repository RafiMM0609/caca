# Origin and design decisions

Recorded 2026-11-07 from the owner's instructions and the implementation history.

## The actual business goal

The owner wants to start a software company from zero and build an AI software factory. Initially there are no company templates, existing products or established production platform. The factory should eventually generate new applications ranging from static portfolios and CRUD/admin systems to POS and complex ERP integrations. Reliability, reviewability and safe operation matter more than merely producing code.

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

Current validated target is native Linux; Docker is optional. Windows compatibility remains an outstanding goal. Do not infer Windows support from Python/JavaScript alone: `fcntl`, POSIX process groups and other lifecycle details still need adaptation and real Windows tests.

## Editing after completion

The owner wants generated UI to remain editable from the factory dashboard. Nuxt Studio was selected for the capabilities it actually offers: local content editing, schema-based forms and preview inside supported Nuxt Content applications.

The chosen journey is: verified build → separate editable draft → embedded Studio form/preview → review a frozen diff → apply as a new revision → run build and acceptance tests again. Existing verified source/artifacts are preserved. Studio is not treated as an arbitrary Vue-code editor or a universal editor for non-Nuxt apps.

The first supplied starter exposes brand, hero text, button label, accent color and features via `content/site.json` and a Nuxt Content schema. It includes a contact demo that validates input without sending email. The starter is a new foundation created in this project, not a pre-existing company template.

## Why there is an independent tester

The owner asked for a tester outside the implementation worker: it receives flows from the planner, prepares real API/web-UI testing while the app is being built, and executes those tests when the app is ready.

The implementation follows that split. The planner supplies `test_flows`. A separate OMP invocation prepares a declarative suite concurrently with implementation, without seeing the worker's source in that preparation request. The controller validates coverage and executes HTTP/Playwright cases after readiness. This limits self-certification and makes evidence visible; it does not prove complete business coverage or eliminate human acceptance.

## Repository identity and publication

Local project folder: `software-factory`. Package/project identity: Anton Software Factory. GitHub repository: `RafiMM0609/caca`. The owner supplied that repository and explicitly authorized public pushes. Earlier write attempts failed with GitHub 403 errors; access later worked and the code was pushed successfully. The name `caca` does not imply a different product goal or permission to replace existing documentation with a one-line README.
