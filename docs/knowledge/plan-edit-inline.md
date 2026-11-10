# Inline plan editing — plan

Written 2026-11-10. Proposal; nothing here is implemented.

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

- Clicking a criterion edits it, and the change survives a page reload.
- A save produces a new revision and the Approve button follows it.
- Editing is refused while a job is running, and the UI says why.
- `instructions` are readable without opening the JSON textarea.
- The JSON textarea still works unchanged.

## Risks

- **Stale revision.** Two tabs open, both save. The server rejects the second with 409; the UI
  must reload rather than silently retry.
- **Partial save.** Mutating a shallow copy can silently share nested arrays. Deep-copy the plan
  before mutation, and test it.
- **The sync gap remains.** Inline editing cannot guarantee the test flows track an instruction
  change. That is a known limit, shown as text, and it is the reason linkage was rejected.
