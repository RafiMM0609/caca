"""A JSON assertion must be able to say "these fields have these values".

The bug this file exists for: `json_equals` demanded the response be *identical* to the
expectation, and the planner naturally wrote a partial object. A correct login route returning
`{id, name, email, role}` failed a case that only wanted `role`, and the repair worker then
"fixed" the application by adding a `?view=full` query parameter so the default response shrank
to match the test. The test was the defect. `json_contains` states a subset, and `ContractDefect`
stops the repair loop when an assertion cannot be satisfied by any correct application.
"""
import json
import socket
import subprocess
import pytest

from factory.testing import (TestSuite, _expected_is_partial,
                             json_subset_mismatch, run_suite)

SERVER = '''const http=require('node:http');
http.createServer((req,res)=>{
  res.setHeader('content-type','application/json');
  if(req.url==='/api/user'){res.end(JSON.stringify({data:{user:{id:1,name:"Admin Toko",email:"admin@toko.local",role:"admin"}}}));return;}
  if(req.url==='/api/nested'){res.end(JSON.stringify({data:{items:[{id:1,label:"a"},{id:2,label:"b"}]}}));return;}
  if(req.url==='/api/wrong'){res.end(JSON.stringify({data:{user:{role:"kasir"}}}));return;}
  res.statusCode=404;res.end(JSON.stringify({error:"not found"}));
}).listen(process.argv[2],'127.0.0.1');
'''


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    script = tmp_path / "server.cjs"
    script.write_text(SERVER)
    port = free_port()
    process = subprocess.Popen(["node", str(script), str(port)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    import time
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        process.kill()
        raise RuntimeError("test server did not start")
    yield f"http://127.0.0.1:{port}"
    process.kill()
    process.wait(timeout=10)


def suite_with(action):
    return TestSuite(summary="One API case for the JSON assertion contract.",
                     cases=[{"flow_id": "probe", "name": "JSON assertion probe", "kind": "api",
                             "actions": [action]}])


def run_case(server, tmp_path, action):
    """Execute one case and return its recorded result.

    `run_suite` re-raises the assertion after recording it (the engine relies on that), so the
    exception is swallowed here to inspect what the report and the message say.
    """
    evidence = tmp_path / "evidence"
    try:
        return run_suite(suite_with(action), server, evidence, timeout=30)["cases"][0]
    except BaseException:
        # The failure is recorded and flushed to report.json before run_suite re-raises, which
        # is exactly what the engine reads. Read the same file rather than assuming a shape.
        return json.loads((evidence / "report.json").read_text())["cases"][0]


def test_json_contains_passes_when_the_named_fields_match(server, tmp_path):
    case = run_case(server, tmp_path, {"action": "request", "path": "/api/user",
                                      "json_contains": {"data": {"user": {"role": "admin"}}}})
    assert case["status"] == "passed", case.get("message")


def test_json_contains_ignores_extra_fields(server, tmp_path):
    """The whole point: a response carrying more than the contract named still passes."""
    case = run_case(server, tmp_path, {"action": "request", "path": "/api/user",
                                      "json_contains": {"data": {"user": {"id": 1, "role": "admin"}}}})
    assert case["status"] == "passed", case.get("message")


def test_json_contains_fails_on_a_wrong_value_and_names_the_path(server, tmp_path):
    case = run_case(server, tmp_path, {"action": "request", "path": "/api/user",
                                      "json_contains": {"data": {"user": {"role": "kasir"}}}})
    assert case["status"] == "failed"
    assert "data.user.role" in case["message"], case["message"]
    assert "admin" in case["message"]


def test_json_contains_names_a_missing_field(server, tmp_path):
    case = run_case(server, tmp_path, {"action": "request", "path": "/api/user",
                                      "json_contains": {"data": {"user": {"nickname": "x"}}}})
    assert case["status"] == "failed"
    assert "data.user.nickname is missing" in case["message"], case["message"]


def test_json_contains_checks_list_length_and_items(server, tmp_path):
    ok = run_case(server, tmp_path, {"action": "request", "path": "/api/nested",
                                    "json_contains": {"data": {"items": [{"id": 1}, {"id": 2}]}}})
    assert ok["status"] == "passed", ok.get("message")
    bad = run_case(server, tmp_path, {"action": "request", "path": "/api/nested",
                                     "json_contains": {"data": {"items": [{"id": 1}]}}})
    assert bad["status"] == "failed"
    assert "expected 1" in bad["message"] or "item(s)" in bad["message"]


def test_a_partial_json_equals_is_a_contract_defect_not_an_application_failure(server, tmp_path):
    """The recorded production failure. It must not be blamed on the application."""
    case = run_case(server, tmp_path, {"action": "request", "path": "/api/user",
                                      "json_equals": {"data": {"user": {"role": "admin"}}}})
    assert case["status"] == "failed"
    assert case["error"] == "ContractDefect", case
    assert "json_contains" in case["message"], "the message must name the fix"


def test_a_real_value_mismatch_stays_an_ordinary_failure(server, tmp_path):
    """Only *shrinkage* is a contract defect. Wrong data is a genuine application bug."""
    case = run_case(server, tmp_path, {"action": "request", "path": "/api/wrong",
                                      "json_equals": {"data": {"user": {"role": "admin"}}}})
    assert case["status"] == "failed"
    assert case["error"] == "AssertionError", case


def test_json_equals_still_requires_an_exact_match(server, tmp_path):
    exact = {"data": {"user": {"role": "kasir"}}}
    case = run_case(server, tmp_path, {"action": "request", "path": "/api/wrong",
                                      "json_equals": exact})
    assert case["status"] == "passed", case.get("message")


def test_one_request_cannot_carry_two_json_assertions():
    with pytest.raises(ValueError, match="not both"):
        suite_with({"action": "request", "path": "/api/user",
                    "json_equals": {"a": 1}, "json_contains": {"a": 1}})


def test_an_empty_subset_asserts_nothing_and_is_rejected():
    with pytest.raises(ValueError, match="at least one field"):
        suite_with({"action": "request", "path": "/api/user", "json_contains": {}})


def test_a_json_assertion_belongs_to_a_request():
    with pytest.raises(ValueError, match="belongs to a request"):
        suite_with({"action": "goto", "path": "/", "json_contains": {"a": 1}})


def test_head_cannot_carry_a_subset_assertion():
    with pytest.raises(ValueError, match="no body"):
        suite_with({"action": "request", "path": "/api/user", "method": "HEAD",
                    "json_contains": {"a": 1}})


@pytest.mark.parametrize("expected,actual,partial", [
    ({"a": 1}, {"a": 1, "b": 2}, True),
    ({"a": 1}, {"a": 1}, False),          # identical, not a shrinkage
    ({"a": 2}, {"a": 1, "b": 2}, False),  # wrong value is a real failure
    ({"a": {"b": 1}}, {"a": {"b": 1, "c": 2}}, True),
    ({"a": {"b": 2}}, {"a": {"b": 1, "c": 2}}, False),
    ({"a": 1}, {"b": 1}, False),          # a missing key is not shrinkage either
])
def test_partial_detection_only_accepts_shrinkage(expected, actual, partial):
    assert _expected_is_partial(expected, actual) is partial


def test_subset_helper_reports_the_first_disagreement():
    assert json_subset_mismatch({"a": 1}, {"a": 1}) is None
    assert json_subset_mismatch({"a": 1}, {"a": 2}) == "$.a is 2, expected 1"
    assert json_subset_mismatch({"a": {"b": 1}}, {"a": {}}) == "$.a.b is missing"
    assert json_subset_mismatch({"a": [1]}, {"a": []}) == "$.a has 0 item(s), expected 1"
    assert "expected an object" in json_subset_mismatch({"a": 1}, [1])
