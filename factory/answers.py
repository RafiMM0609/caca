"""Turn operator answers to planner questions into an approvable plan.

A planner raises `questions` when the brief is underspecified, and the UI refuses to approve a
plan that still has any. Before this module the only way to answer was to hand-edit the plan
JSON: delete the question, append an assumption, and remember to mirror the answer into every
task instruction and every test flow. That last step was skipped once, and it cost a whole
build — the operator said the page must show an Edit button, the workers were told, the tester
was not, and a correct application failed the gate.

So the answer is applied to all four places in one operation rather than trusting an operator
to remember them:

  - the question is removed, because approval requires an empty list;
  - the answer becomes an assumption, so the record shows what was decided and why;
  - every task instruction gains it, so the workers build to it;
  - every test flow goal and step gains it, so the tester asserts it.

Pure functions over a `Plan`, with no database or HTTP, so the contract can be tested directly.
"""
from .models import GOAL_MAX_LENGTH, Plan

OPERATOR_HEADING = "OPERATOR ANSWERS"


def _existing_answers(instructions: str) -> list[str]:
    """Answer lines already carried by a task, so a second round adds rather than replaces."""
    if OPERATOR_HEADING not in instructions:
        return []
    block = instructions.split(OPERATOR_HEADING + ":", 1)[1]
    return [line.rstrip() for line in block.splitlines() if line.startswith("- ")]


def _clean(value: str) -> str:
    return " ".join(str(value).split()).strip()


def apply_answers(plan: Plan, answers: dict[str, str]) -> Plan:
    """Return a new plan with the answered questions resolved.

    `answers` maps a question string, exactly as the planner wrote it, to the operator's
    answer. Matching on the full question text is deliberate: a position or partial phrase
    would silently apply an answer to the wrong question if the planner reordered them, and a
    wrong answer is worse than a missing one.

    A question with no answer, or a blank one, is left in place: the plan stays unapprovable
    rather than being approved with a gap. An answer for a question that is not in the plan is
    rejected, because it usually means a stale page and the operator's intent would be lost.
    """
    cleaned = {_clean(question): _clean(answer) for question, answer in answers.items()}
    known = {_clean(question) for question in plan.questions}

    unknown = sorted(set(cleaned) - known)
    if unknown:
        raise ValueError(
            "These questions are not in the current plan, so the page is probably stale: "
            + "; ".join(question[:120] for question in unknown))

    resolved = [(question, cleaned[question]) for question in plan.questions
                if cleaned.get(_clean(question))]
    if not resolved:
        # Nothing to do. Returning the plan unchanged keeps the caller honest: approve will
        # still refuse, and the reason is the unanswered questions, not a silent edit.
        return plan.model_copy(deep=True)

    remaining = [question for question in plan.questions
                 if not cleaned.get(_clean(question))]

    lines = [f"- {question} -> {answer}" for question, answer in resolved]

    tasks = []
    for task in plan.tasks:
        instructions = task.instructions.rstrip()
        # A later round must accumulate, not replace. Stripping the previous block and writing a
        # fresh one silently dropped an earlier answer, which is the same class of mistake as
        # the one this module exists to prevent: an operator decision that never reaches the
        # worker.
        existing = _existing_answers(instructions)
        merged = [*existing, *(line for line in lines if line not in existing)]
        if OPERATOR_HEADING in instructions:
            instructions = instructions.split(OPERATOR_HEADING + ":")[0].rstrip()
        tasks.append(task.model_copy(update={
            "instructions": f"{instructions}\n\n{OPERATOR_HEADING}:\n" + "\n".join(merged)}))

    # Answers are appended to every flow's goal. Six detailed answers measured 1854 characters
    # on their own, which exceeded the old 2000-character cap and made an answered plan fail
    # validation — the operator saw only a 422 "invalid content". The cap is now
    # `GOAL_MAX_LENGTH` (8000), which fits the answers map's own limit of 30 answers without
    # truncating any of them.
    #
    # Truncation is kept as a guard rather than removed, because the plan can also be edited by
    # hand and a goal that silently exceeds the model's cap is far harder to diagnose than one
    # that says it was shortened. It trims only the appended addition, never the original
    # sentence: the original is what the flow is *for*, while the addition repeats text the
    # worker already has in `task.instructions` and `assumptions`.
    flows = []
    for flow in plan.test_flows:
        addition = "; ".join(f"{question} -> {answer}".rstrip(". ")
                             for question, answer in resolved)
        goal = flow.goal.rstrip()
        if not goal.endswith("."):
            goal += "."
        head = f"{goal} Operator decisions this flow must respect: "
        # Reserve one character for the terminating full stop.
        budget = max(0, GOAL_MAX_LENGTH - len(head) - 1)
        if len(addition) > budget:
            # The marker counts against the budget too; appending it after slicing overshot the
            # limit by its own length and produced a plan the model rejected.
            marker = " [detail truncated]"
            keep = max(0, budget - len(marker))
            addition = addition[:keep].rstrip(" ;,") + marker
            addition = addition[:budget]
        flows.append(flow.model_copy(update={
            "goal": f"{head}{addition}.",
            # The step carries the same text the goal has, so the tester executes what the goal
            # states. It is not separately capped, and `steps` is limited by count, not length.
            "steps": [*flow.steps, f"Honour the operator decision: {addition}."],
        }))

    assumptions = [*plan.assumptions, *(f"{question} -> {answer}" for question, answer in resolved)]

    return plan.model_copy(update={
        "questions": remaining,
        "assumptions": assumptions,
        "tasks": tasks,
        "test_flows": flows,
    })


def pending(plan: Plan) -> list[str]:
    """Questions still blocking approval, for the UI to render one input each."""
    return list(plan.questions)
