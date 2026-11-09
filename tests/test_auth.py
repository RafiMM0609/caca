"""An application with a login must be testable.

`TestAction` had no `headers` field, so the tester could not present a bearer token. Cookies
persisted within a case, which covered a flow that starts by logging in, but not a token the
caller has to supply — and that is how most CRUD/admin applications are protected.
"""
import json

import pytest

from factory.local_app import LocalApp

# Echoes the auth header so the test can prove the header actually reached the server,
# and gates a page on it so browser navigation is covered too.
AUTH_SERVER = r'''
const http = require("node:http");
http.createServer((req, res) => {
  const token = req.headers["authorization"] || "";
  const ok = token === "Bearer test-token-123";
  if (req.url === "/api/health") {
    res.setHeader("content-type", "application/json");
    res.end(JSON.stringify({ status: "ok", authenticated: ok }));
    return;
  }
  if (req.url === "/api/private") {
    res.statusCode = ok ? 200 : 401;
    res.setHeader("content-type", "application/json");
    res.end(JSON.stringify({ secret: ok ? "unlocked" : "denied" }));
    return;
  }
  res.setHeader("content-type", "text/html");
  res.end(ok
    ? '<!doctype html><title>private</title><h1 data-testid="area">Unlocked area</h1>'
    : '<!doctype html><title>login</title><h1 data-testid="area">Please log in</h1>');
}).listen(Number(process.env.PORT || 0), "127.0.0.1");
'''


def load_testing():
    """Imported lazily: pytest tries to collect any name starting with `Test`."""
    from factory import testing
    return testing


def serve(tmp_path):
    (tmp_path / "server.cjs").write_text(AUTH_SERVER)
    app = LocalApp(["node", "server.cjs"], tmp_path)
    app.wait_ready("/api/health", timeout=30)
    return app


def test_a_bearer_token_can_be_sent_on_an_api_request(tmp_path):
    app = serve(tmp_path)
    try:
        suite = load_testing().TestSuite(summary="An API case that presents a bearer token.", cases=[
            {"flow_id": "private", "name": "Private endpoint with a token", "kind": "api",
             "actions": [{"action": "request", "path": "/api/private", "status": 200,
                          "json_equals": {"secret": "unlocked"},
                          "headers": {"Authorization": "Bearer test-token-123"}}]}])
        report = load_testing().run_suite(suite, app.url, tmp_path / "ev", 60)
        assert report["status"] == "passed", report
    finally:
        app.close()


def test_without_the_token_the_same_case_fails(tmp_path):
    """Proof the header is what makes it pass, not an accident of the server."""
    app = serve(tmp_path)
    try:
        suite = load_testing().TestSuite(summary="The same case without a token must be rejected.", cases=[
            {"flow_id": "private", "name": "Private endpoint without a token", "kind": "api",
             "actions": [{"action": "request", "path": "/api/private", "status": 200,
                          "json_equals": {"secret": "unlocked"}}]}])
        with pytest.raises(AssertionError):
            load_testing().run_suite(suite, app.url, tmp_path / "ev", 60)
    finally:
        app.close()


def test_browser_navigation_can_carry_a_header(tmp_path):
    app = serve(tmp_path)
    try:
        suite = load_testing().TestSuite(summary="A browser case that needs a header to reach the page.", cases=[
            {"flow_id": "area", "name": "Private page with a header", "kind": "browser",
             "headers": {"Authorization": "Bearer test-token-123"},
             "actions": [{"action": "goto", "path": "/"},
                         {"action": "expect_text", "selector": "[data-testid=area]",
                          "value": "Unlocked"}]}])
        report = load_testing().run_suite(suite, app.url, tmp_path / "ev", 60)
        assert report["status"] == "passed", report
    finally:
        app.close()


@pytest.mark.parametrize("name", ["Authorization", "X-Api-Key", "x-request-id"])
def test_reasonable_header_names_are_accepted(name):
    action = load_testing().TestAction.model_validate({"action": "request", "path": "/",
                                        "headers": {name: "value"}})
    assert name in action.headers


@pytest.mark.parametrize("header,label", [
    ({"Bad Header": "x"}, "space in name"),
    ({"X-Test": "line1\nline2"}, "newline in value, which would allow header injection"),
    ({"": "x"}, "empty name"),
])
def test_a_header_that_could_inject_is_rejected(header, label):
    with pytest.raises(ValueError):
        load_testing().TestAction.model_validate({"action": "request", "path": "/", "headers": header})


def test_a_case_level_header_that_could_inject_is_rejected():
    with pytest.raises(ValueError):
        load_testing().TestCase.model_validate({"flow_id": "x", "name": "Case with a bad header",
                                 "kind": "browser",
                                 "headers": {"X-Test": "a\nb"},
                                 "actions": [{"action": "goto", "path": "/"},
                                             {"action": "expect_visible", "selector": "#a"}]})


def test_headers_serialise_into_the_saved_suite(tmp_path):
    """The suite is written to disk as evidence; headers must survive that round trip."""
    suite = load_testing().TestSuite(summary="A suite whose case carries a header.", cases=[
        {"flow_id": "x", "name": "Case with a header", "kind": "api",
         "actions": [{"action": "request", "path": "/", "headers": {"X-Api-Key": "abc"}}]}])
    restored = json.loads(suite.model_dump_json())
    assert restored["cases"][0]["actions"][0]["headers"] == {"X-Api-Key": "abc"}
