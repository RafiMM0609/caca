"""Answering a planner question must reach every place the answer has to reach.

An operator editing plan JSON by hand had to remember four things: remove the question, add an
assumption, append the answer to every task instruction, and mirror it into every test flow.
Skipping the last one once failed a build whose application was correct — the workers built the
Edit button, the tester never asserted it. These tests exist so that cannot recur.
"""

import pytest
from fastapi.testclient import TestClient

from factory.answers import OPERATOR_HEADING, apply_answers
from factory.api import create_app
from factory.config import Settings
from factory.db import Store
from factory.models import Plan

IMAGE = "python@sha256:" + "a" * 64


def a_plan(**overrides):
    payload = {
        "summary": "A plan with one interface task and one browser flow.",
        "questions": ["Haruskah ada tombol Edit?"],
        "tasks": [{"id": "ui", "title": "Build the interface",
                   "instructions": "Build the interface as described.",
                   "acceptance": ["works"]}],
        "test_flows": [{"id": "add_row", "kind": "browser",
                        "goal": "User can add a row",
                        "steps": ["Open the page"],
                        "selectors": {"name": "#name"}}],
    }
    payload.update(overrides)
    return Plan.model_validate(payload)


def test_an_answer_reaches_all_four_places():
    plan = a_plan()
    answered = apply_answers(plan, {"Haruskah ada tombol Edit?": "Ya, di setiap baris."})
    assert answered.questions == [], "the question must be resolved or approval stays blocked"
    assert any("tombol Edit" in a for a in answered.assumptions), "the decision must be recorded"
    assert "tombol Edit" in answered.tasks[0].instructions, "the worker builds what the answer says"
    assert "tombol Edit" in answered.test_flows[0].goal, "the flow goal must reflect it"
    assert any("tombol Edit" in step for step in answered.test_flows[0].steps), \
        "the steps are what the tester executes; missing this failed a correct build once"


def test_an_unanswered_question_stays_and_blocks_approval():
    plan = a_plan(questions=["Question one?", "Question two?"])
    answered = apply_answers(plan, {"Question one?": "yes"})
    assert answered.questions == ["Question two?"]


def test_a_blank_answer_does_not_count():
    plan = a_plan()
    assert apply_answers(plan, {"Haruskah ada tombol Edit?": "   "}).questions == \
        ["Haruskah ada tombol Edit?"]


def test_answers_accumulate_across_rounds():
    """A second round must not drop the first: an operator decision must never be lost."""
    plan = a_plan(questions=["First?", "Second?"])
    first = apply_answers(plan, {"First?": "alpha"})
    second = apply_answers(first, {"Second?": "beta"})
    instructions = second.tasks[0].instructions
    assert instructions.count(OPERATOR_HEADING) == 1, "the heading must not stack"
    assert "alpha" in instructions and "beta" in instructions


def test_an_unknown_question_is_rejected():
    """An answer to a question the plan does not contain means a stale page."""
    with pytest.raises(ValueError, match="stale"):
        apply_answers(a_plan(), {"Pertanyaan yang tidak ada?": "yes"})


def test_answering_nothing_returns_the_plan_unchanged():
    """Silently 'succeeding' with no change would let an unapprovable plan look handled."""
    plan = a_plan()
    assert apply_answers(plan, {}) == plan


def test_the_plan_is_not_mutated_in_place():
    plan = a_plan()
    apply_answers(plan, {"Haruskah ada tombol Edit?": "ya"})
    assert plan.questions == ["Haruskah ada tombol Edit?"], "the original must be untouched"


@pytest.fixture
def client(tmp_path):
    settings = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                        pi="/usr/bin/true", model="test/model",
                        profiles={"dash": {"image": IMAGE, "verify_argv": ["true"],
                                           "tester": True, "checks_dir": str(tmp_path)}})
    settings.validate()
    store = Store(tmp_path / "factory.sqlite3")
    app = create_app(settings)
    with TestClient(app) as test_client:
        test_client.headers["Authorization"] = "Bearer " + settings.operator_token
        yield test_client, store


def open_plan(client, store):
    created = client.post("/api/projects", json={
        "name": "Form Answers", "brief": "A product whose plan will raise a question.",
        "profile": "dash"}).json()
    plan = a_plan()
    with store.tx() as db:
        db.execute("UPDATE projects SET plan=?,revision=1,state='awaiting_plan' WHERE id=?",
                   (plan.model_dump_json(), created["id"]))
    return created["id"], plan


def test_the_endpoint_resolves_a_question_and_bumps_the_revision(client):
    test_client, store = client
    project, _ = open_plan(test_client, store)
    response = test_client.post(f"/api/projects/{project}/answers", json={
        "revision": 1, "answers": {"Haruskah ada tombol Edit?": "Ya, di setiap baris."}})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision"] == 2
    assert Plan.model_validate_json(body["plan"]).questions == []


def test_the_endpoint_records_the_answer_as_an_event(client):
    test_client, store = client
    project, _ = open_plan(test_client, store)
    test_client.post(f"/api/projects/{project}/answers", json={
        "revision": 1, "answers": {"Haruskah ada tombol Edit?": "Ya."}})
    events = [e for e in store.all("SELECT kind FROM events WHERE project_id=?", (project,))
              if e["kind"] == "plan.answered"]
    assert events, "answering must be auditable"


def test_a_stale_revision_is_conflicted(client):
    test_client, store = client
    project, _ = open_plan(test_client, store)
    response = test_client.post(f"/api/projects/{project}/answers", json={
        "revision": 999, "answers": {"Haruskah ada tombol Edit?": "Ya."}})
    assert response.status_code == 409


def test_empty_answers_are_rejected_by_the_schema(client):
    test_client, store = client
    project, _ = open_plan(test_client, store)
    response = test_client.post(f"/api/projects/{project}/answers",
                                json={"revision": 1, "answers": {}})
    assert response.status_code == 422


def test_answering_requires_the_operator_token(tmp_path):
    settings = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                        pi="/usr/bin/true", model="test/model",
                        profiles={"dash": {"image": IMAGE, "verify_argv": ["true"],
                                           "tester": True, "checks_dir": str(tmp_path)}})
    settings.validate()
    anonymous = TestClient(create_app(settings))
    response = anonymous.post("/api/projects/anything/answers",
                              json={"revision": 1, "answers": {"Q?": "a"}})
    assert response.status_code == 401


def test_many_long_answers_do_not_produce_an_unvalidatable_plan():
    """A history: six detailed answers made every test flow's goal exceed its 2000-char limit.

    `apply_answers` appended the full text of every answer to every flow goal. Six answers
    measured 1854 characters on their own, so with a normal goal sentence the result was
    rejected by `Plan.model_validate` — and the API surfaced that as a 422 saying only "invalid
    content", with no indication that answering was the thing that broke it. A plan that cannot
    be validated cannot be approved, so answering a planner question bricked the project.
    """
    plan = Plan(
        summary="A project with a single flow and several open questions.",
        assumptions=[],
        questions=[f"Question number {index} about a design decision that needs answering?"
                   for index in range(6)],
        tasks=[{"id": "site", "title": "Build the site",
                "instructions": "Create the page.",
                "acceptance": ["The page renders"], "depends_on": []}],
        test_flows=[{"id": "flow", "kind": "api", "goal": "The health endpoint responds with ok.",
                     "steps": ["GET /api/health"], "selectors": {}}],
    )
    answers = {question: "This is a deliberately long operator answer. " * 12
               for question in plan.questions}

    updated = apply_answers(plan, answers)

    from factory.models import GOAL_MAX_LENGTH

    # The round trip is the assertion: this is exactly what the approval path does.
    Plan.model_validate_json(updated.model_dump_json())
    assert updated.questions == []
    for flow in updated.test_flows:
        assert len(flow.goal) <= GOAL_MAX_LENGTH, f"goal is {len(flow.goal)} characters"
        assert flow.goal.startswith("The health endpoint responds with ok.")
        # Six answers of this size fit the raised limit, so nothing should be shortened. If this
        # starts failing, either the limit moved or the answer text grew -- both worth knowing.
        assert "truncated" not in flow.goal, "a normal workload must not be silently abbreviated"
        for answer in answers.values():
            assert answer.strip().rstrip(".") in flow.goal, "an answer was dropped from the goal"


def test_the_full_answer_still_reaches_the_task_instructions():
    """The goal may be shortened, but the worker must still receive the whole decision.

    Dropping detail from the goal is only acceptable because the instructions carry it in full.
    """
    long_answer = "Store every record as JSON under DATA_DIR and never hardcode the path. " * 10
    plan = Plan(
        summary="One question, one task.",
        assumptions=[], questions=["Where does data live?"],
        tasks=[{"id": "site", "title": "Build", "instructions": "Create it.",
                "acceptance": ["Works"], "depends_on": []}],
        test_flows=[{"id": "flow", "kind": "api", "goal": "GET / returns a page.",
                     "steps": ["GET /"], "selectors": {}}],
    )
    updated = apply_answers(plan, {"Where does data live?": long_answer})
    instructions = updated.tasks[0].instructions
    assert long_answer.strip().rstrip(".") in instructions


def test_a_goal_that_exceeds_the_limit_is_shortened_visibly_not_silently():
    """The raised cap does not remove the guard; it only makes it rare.

    A plan can also be edited by hand, and an over-long goal that is silently truncated is worse
    than one that says so: the tester would be given text the operator believes is complete.
    """
    from factory.models import GOAL_MAX_LENGTH

    plan = Plan(
        summary="One question whose answer cannot possibly fit.",
        assumptions=[], questions=["Describe everything about the system."],
        tasks=[{"id": "site", "title": "Build", "instructions": "Create it.",
                "acceptance": ["Works"], "depends_on": []}],
        test_flows=[{"id": "flow", "kind": "api", "goal": "GET / returns a page.",
                     "steps": ["GET /"], "selectors": {}}],
    )
    huge = "X" * (GOAL_MAX_LENGTH * 2)
    updated = apply_answers(plan, {"Describe everything about the system.": huge})

    Plan.model_validate_json(updated.model_dump_json())
    flow = updated.test_flows[0]
    assert len(flow.goal) <= GOAL_MAX_LENGTH
    assert flow.goal.startswith("GET / returns a page."), "the original sentence must survive"
    assert "truncated" in flow.goal, "a shortened goal must say it was shortened"
