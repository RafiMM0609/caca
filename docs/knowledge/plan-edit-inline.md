# Inline plan editing — **DONE 2026-11-10**

Written 2026-11-10, implemented the same day.

## The problem, stated precisely

The purpose of editing a plan is: **a feature does not match what the user wants, so change it
and rebuild.** Not "make the plan editable" in the abstract.

Measured against the real plans in this repository, the blocking step is not the editor. It is
that `instructions` — up to 2000 characters of specification per task — is **never displayed**.
`app.js:116` renders only `task.acceptance`:

```js
for (const text of task.acceptance) criteria.append(el("li", text));
```

So finding the wrong sentence means opening the JSON textarea and scanning 328 lines. The
criteria themselves are already readable and specific ("Delete memakai modal konfirmasi
kustom, bukan confirm() browser"), which means the missing capability is one interaction:

> read a line → click it → change it → save

## Who edits

Only the owner. Clients never touch the factory: products are separated by repository and
directory, not by login, and clients do not log in. This is why the plan is deliberately small.
An earlier draft designed forms for a non-technical employee who does not exist yet, and every
question opened a new layer for that imaginary user. That is how a 4-hour job became a two-day
one.

## Scope — what changes

1. **Criteria edit inline.** Clicking an acceptance line turns it into a textarea; Save writes it.
2. **Instructions are shown**, inside a collapsed `<details>` per task. Visible and readable,
   without becoming a wall of text.
3. **Flows stay visible beside tasks**, as they already are, so the testing contract is in view
   while judging a change.

## Scope — what deliberately does not change

| Not built | Why |
| --- | --- |
| Adding a task | Adding a task is planning work. Regenerate the plan instead. |
| Form for flows and selectors | The textarea already handles it; a selector map is not a form. |
| Task↔flow linkage | Restricts the planner, and does not belong to editing. |
| Gap report and coverage batches | Parked. Separate decision, separate data. |
| Sync warning as a mechanism | Shown as text only. The editor is the owner, who already understands it. |

## Implementation

No schema change, no endpoint change. `PUT /api/projects/<id>/plan` already accepts a whole
plan and enforces revision matching, so the UI sends the full object with one field changed.

- Mutate a copy of the plan and send it whole. Never patch server-side.
- Reuse the existing `action()` + `refresh()` path so `revision` advances after a save; the
  existing stale-approval guard then still protects a second operator.
- Disable inline editing while a job is active, matching the server rule that refuses an edit
  during a run.
- Keep the JSON textarea. It remains the only route for what inline editing deliberately omits.
- Show a line of text when a task's `instructions` changes but `test_flows` did not:
  "instruksi berubah, skenario uji tidak" — informational, not blocking.

## Done when

- Clicking a criterion edits it, and the change survives a page reload. **Done.**
- A save produces a new revision and the Approve button follows it. **Done:** revision 1 to 2,
  `plan.edited` recorded, and the Approve handler reads the same `revision` variable.
- Editing is refused while a job is running, and the UI says why. **Done:** criteria render as
  plain text while a job is queued or running, matching the server's refusal.
- `instructions` are readable without opening the JSON textarea. **Done:** a collapsed
  `<details>` per task, plus a `<details>` listing each flow's selectors.
- The JSON textarea still works unchanged. **Done**, deliberately: it remains the only route
  for adding a task and for editing selectors.

### What was verified, and how

In a real browser against a live factory, not only in unit tests: two criteria render as
clickable buttons, `instructions` and the selector table render inside their collapsed blocks,
clicking a criterion opens a textarea, Cancel restores a clickable row (this was a real bug —
attaching the handler by hand left the row dead after one cancel), Save stores the change and
the new text survives a refresh. The stored plan kept its instructions, its flow and all
selectors; the revision advanced to 2.

`tests/editing.test.cjs` covers the copy semantics: a shallow spread shares the nested
`acceptance` array and mutates the plan the screen is rendering, which would leave a change
visible after a save that never happened. 6 tests.

### Not built, as planned

Adding or removing a task, a form for flows and selectors, task-to-flow linkage, and any
enforcement of the sync warning. The warning is a line of text.

## Risks

- **Stale revision.** Two tabs open, both save. The server rejects the second with 409; the UI
  must reload rather than silently retry.
- **Partial save.** Mutating a shallow copy can silently share nested arrays. Deep-copy the plan
  before mutation, and test it.
- **The sync gap remains.** Inline editing cannot guarantee the test flows track an instruction
  change. That is a known limit, shown as text, and it is the reason linkage was rejected.
