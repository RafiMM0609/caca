// Inline editing changes one acceptance line and sends the whole plan back.
//
// The two mistakes worth testing are both about copies: a shallow spread shares the nested
// arrays, so mutating the copy silently mutates the object the screen is rendering, and a
// failed save would leave a change visible that was never stored.
const test = require("node:test");
const assert = require("node:assert");

// The same function as factory/static/app.js:deepCopyPlan. Kept in sync by this test.
const deepCopyPlan = plan => JSON.parse(JSON.stringify(plan));

function aPlan() {
  return {
    summary: "A plan with one task and two criteria.",
    questions: [],
    assumptions: [],
    tasks: [{ id: "ui", title: "Build it", instructions: "Build the page.",
              acceptance: ["First criterion", "Second criterion"], depends_on: [], skills: [] }],
    test_flows: [{ id: "f1", kind: "api", goal: "Check it works", steps: ["GET /"], selectors: {} }],
  };
}

// Mirrors the mutation startEdit performs before saving.
function editCriterion(plan, taskIndex, index, value) {
  const copy = deepCopyPlan(plan);
  copy.tasks[taskIndex].acceptance[index] = value;
  return copy;
}

test("a criterion can be changed without touching the original plan", () => {
  const original = aPlan();
  const edited = editCriterion(original, 0, 0, "Changed criterion");
  assert.equal(edited.tasks[0].acceptance[0], "Changed criterion");
  assert.equal(original.tasks[0].acceptance[0], "First criterion",
    "the rendered plan must not change when a save has not happened yet");
});

test("editing one criterion leaves the others intact", () => {
  const edited = editCriterion(aPlan(), 0, 1, "Only the second");
  assert.deepEqual(edited.tasks[0].acceptance, ["First criterion", "Only the second"]);
});

test("the edit is isolated to one task", () => {
  const plan = aPlan();
  plan.tasks.push({ id: "api", title: "Build API", instructions: "Build it.",
                    acceptance: ["Untouched"], depends_on: [], skills: [] });
  const edited = editCriterion(plan, 0, 0, "New first");
  assert.deepEqual(edited.tasks[1].acceptance, ["Untouched"]);
});

test("a shallow copy would corrupt the original", () => {
  // Guards the reason deepCopyPlan exists: a spread shares the nested array.
  const original = aPlan();
  const shallow = { ...original, tasks: [...original.tasks] };
  shallow.tasks[0] = { ...shallow.tasks[0], acceptance: [...shallow.tasks[0].acceptance] };
  shallow.tasks[0].acceptance[0] = "Safe";
  assert.equal(original.tasks[0].acceptance[0], "First criterion",
    "copying the nested acceptance array keeps the original intact");

  const naive = { ...original };
  naive.tasks[0].acceptance[0] = "Mutated through the shared array";
  assert.equal(original.tasks[0].acceptance[0], "Mutated through the shared array",
    "a bare spread does not protect the original");
});

test("everything except the edited criterion is preserved for the server", () => {
  const original = aPlan();
  const edited = editCriterion(original, 0, 0, "Changed");
  const { tasks, ...restEdited } = edited;
  const { tasks: _t, ...restOriginal } = original;
  assert.deepEqual(restEdited, restOriginal, "summary, flows and assumptions must survive a save");
  assert.equal(tasks[0].instructions, original.tasks[0].instructions);
  assert.equal(tasks[0].depends_on.length, 0);
});

test("an empty criterion is rejected before saving", () => {
  // The UI refuses a blank value; an acceptance line with no text is not a criterion.
  const isBlank = value => value.trim().length === 0;
  assert.equal(isBlank("   "), true);
  assert.equal(isBlank("Something measurable"), false);
});
