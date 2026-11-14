"""A provider failure must say what the operator can do about it.

The real incident: a model call returned nothing because the API key had expired. The provider
said so plainly — `{"code":"api_key_expired"}` — but the job reported "execution failed; inspect
task evidence and readiness", and the operator had to open evidence files to learn the cause.

The rule is narrow on purpose. A failure with a known cause is named in terms of the fix; an
unknown failure keeps the generic message, because a confident wrong diagnosis is worse than
none, and a pattern that quotes the provider could echo a credential into the UI.
"""
import pytest

from factory import diagnosis


class ProviderError(Exception):
    def __init__(self, message, output=""):
        super().__init__(message)
        self.output = output


def explain_of(text, output=""):
    return diagnosis.describe(ProviderError(text, output))


def test_an_expired_key_is_named_with_what_to_do():
    message = explain_of("model returned no text", '{"code":"api_key_expired",'
                          '"message":"This API key has expired."}')
    assert "kedaluwarsa" in message
    assert "OPENCODE_API_KEY" in message, "the operator must be told which setting to change"


def test_a_rejected_key_is_distinguished_from_an_expired_one():
    expired = explain_of("x", '{"code":"api_key_expired"}')
    rejected = explain_of('401 {"code":"invalid_api_key"}')
    assert expired != rejected, "an expired key and a wrong key need different actions"


@pytest.mark.parametrize("text,fragment", [
    ('{"code":"insufficient_quota"}', "Kuota"),
    ("429 too many requests", "membatasi laju"),
    ("404 model_not_found", "FACTORY_MODEL"),
    ("getaddrinfo failed: name resolution error", "jaringan"),
    ("maximum context length exceeded", "konteks"),
])
def test_each_known_failure_maps_to_its_own_advice(text, fragment):
    assert fragment in explain_of(text)


def test_an_unknown_failure_keeps_the_generic_message():
    """Guessing is worse than admitting there is nothing to say."""
    assert explain_of("something entirely unexpected happened") == ""


def test_an_empty_failure_explains_nothing():
    assert explain_of("") == ""


def test_the_output_of_the_call_is_searched_too():
    """The provider error arrives on stdout, not in the exception message."""
    assert explain_of("Model returned no text", '{"code":"api_key_expired"}') != ""


def test_a_credential_shaped_fragment_never_appears_in_the_reason():
    """The reason comes from a fixed vocabulary, not from quoting the provider."""
    secret = "oc_sk_deadbeefcafe_AAAABBBBCCCCDDDD"
    message = explain_of(f"401 authentication_error for key {secret}")
    assert secret not in message
    assert "oc_sk" not in message


def test_the_job_records_the_explanation_as_an_event(tmp_path):
    """The dashboard reads the job's error; the event keeps the reason auditable."""
    from factory.config import Settings
    from factory.db import Store
    from factory.engine import Engine
    from factory.models import Profile, ProjectInput

    settings = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                        model="test/model", pi="/usr/bin/true",
                        profiles={"dash": {"runner": "native", "commands": [["true"]], "artifact_dir": "index.html"}})
    # `Settings(...)` stores profiles as given; convert them the way `from_env` does, because
    # the plan path calls `profile.model_dump()`.
    settings.profiles = {name: Profile.model_validate(raw)
                         for name, raw in settings.profiles.items()}
    settings.validate()
    store = Store(tmp_path / "factory.sqlite3")
    project = store.create_project(ProjectInput(
        name="Failing Provider", brief="A product whose plan call will be rejected.",
        profile="dash"))
    job = store.enqueue(project["id"], "plan", {}, allowed={"draft"}, next_state="planning")

    class Agent:
        def generate(self, role, instruction, context, schema, cancelled):
            raise ProviderError("model returned no text", '{"code":"api_key_expired"}')

    class Memory:
        async def search(self, *args, **kwargs):
            return ""

    engine = Engine(settings, store, agent=Agent(), memory=Memory())
    # The 30s retry spacing exists to avoid hammering a provider; no provider is reached here.
    engine._spacing = lambda cancelled, seconds=0: None
    engine.process(store.claim())
    row = store.one("SELECT state, error FROM jobs WHERE id=?", (job["id"],))
    assert row["state"] == "failed"
    assert "kedaluwarsa" in row["error"], "the job error must say what happened"
    kinds = [e["kind"] for e in store.all("SELECT kind FROM events WHERE job_id=?",
                                          (job["id"],))]
    assert "job.failure_explained" in kinds


def test_an_unexplained_failure_still_says_where_to_look(tmp_path):
    from factory.config import Settings
    from factory.db import Store
    from factory.engine import Engine
    from factory.models import Profile, ProjectInput

    settings = Settings(data=tmp_path, operator_token="o" * 40, release_token="r" * 40,
                        model="test/model", pi="/usr/bin/true",
                        profiles={"dash": {"runner": "native", "commands": [["true"]], "artifact_dir": "index.html"}})
    # `Settings(...)` stores profiles as given; convert them the way `from_env` does, because
    # the plan path calls `profile.model_dump()`.
    settings.profiles = {name: Profile.model_validate(raw)
                         for name, raw in settings.profiles.items()}
    settings.validate()
    store = Store(tmp_path / "factory.sqlite3")
    project = store.create_project(ProjectInput(
        name="Odd Failure", brief="A product whose plan call fails for an unknown reason.",
        profile="dash"))
    job = store.enqueue(project["id"], "plan", {}, allowed={"draft"}, next_state="planning")

    class Agent:
        def generate(self, role, instruction, context, schema, cancelled):
            raise ProviderError("something nobody predicted")

    class Memory:
        async def search(self, *args, **kwargs):
            return ""

    engine = Engine(settings, store, agent=Agent(), memory=Memory())
    engine._spacing = lambda cancelled, seconds=0: None
    engine.process(store.claim())
    row = store.one("SELECT error FROM jobs WHERE id=?", (job["id"],))
    assert "inspect task evidence" in row["error"], \
        "with no known cause the operator is still pointed at the evidence"


def test_the_provider_error_inside_a_message_event_is_extracted():
    """Pi exits 0 and puts the failure *inside* `message`, not at the top level.

    `assistant_json` looked only at `event["errorMessage"]`, so an expired key was reduced to
    "Model returned no text" and the operator saw "inspect task evidence" for a cause the
    provider had stated plainly. This is the exact shape Pi emits.
    """
    from factory.agents import assistant_json

    line = ('{"type":"message_start","message":{"role":"assistant","content":[],'
            '"stopReason":"error","errorMessage":"401: {\\"code\\":\\"api_key_expired\\"}"}}')
    text, problems = assistant_json(line)
    assert text == ""
    assert problems, "the nested error must be reported, or the cause is lost"
    assert "api_key_expired" in problems[0]


def test_the_extracted_error_becomes_actionable_advice():
    from factory.agents import assistant_json

    line = ('{"type":"message_end","message":{"role":"assistant","content":[],'
            '"errorMessage":"401: authentication_error invalid credential"}}')
    _, problems = assistant_json(line)
    assert "OPENCODE_API_KEY" in diagnosis.explain(problems[0])


def test_a_normal_assistant_message_reports_no_problem():
    from factory.agents import assistant_json

    line = ('{"type":"message_end","message":{"role":"assistant",'
            '"content":[{"type":"text","text":"{\\"ok\\":true}"}]}}')
    text, problems = assistant_json(line)
    assert text == '{"ok":true}'
    assert problems == []


def test_a_worker_killed_by_its_deadline_says_which_knob_to_raise():
    """The measured incident: a repair worker died at 600.5s and reported nothing useful."""
    from factory.process import ProcessError

    error = ProcessError("Process exceeded its 600s deadline and was stopped", reason="timeout")
    reason = diagnosis.describe(error)
    assert "600" in reason, "the limit that was hit must be named"
    assert "FACTORY_WORKER_TIMEOUT" in reason, "the operator needs the knob, not just the fact"
    assert "FACTORY_JOB_TIMEOUT" in reason


def test_a_timeout_is_not_described_as_a_provider_problem():
    """A killed worker's own output can mention network timeouts; that must not become blame."""
    from factory.process import ProcessError

    error = ProcessError("Process exceeded its 1800s deadline and was stopped",
                         reason="timeout",
                         output="fetch failed: timeout of 5000ms exceeded\nconnection timed out")
    reason = diagnosis.describe(error)
    assert "Provider tidak bisa dihubungi" not in reason
    assert "batas waktu" in reason


def test_a_non_zero_exit_is_still_not_guessed_at():
    from factory.process import ProcessError

    assert diagnosis.describe(ProcessError("Process failed with exit code 3", reason="exit",
                                          output="Traceback: TypeError: boom")) == ""


def test_the_process_error_carries_its_reason_and_output():
    from factory.process import ProcessError

    error = ProcessError("boom", reason="timeout", output="tail")
    assert error.reason == "timeout"
    assert error.output == "tail"
    assert isinstance(error, RuntimeError)
