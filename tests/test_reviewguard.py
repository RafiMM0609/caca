"""A reviewer's build claim must be checked against the compiler before it rejects code.

The defect these tests pin: a live build was rejected three times for "Build-breaking" findings
while the tree compiled clean and served HTTP. The reviewer said `net.Stream.Reader.interface`
was a field (it is a method in Zig 0.15.2), called `std.io.Writer.Allocating` a namespace
mismatch (it compiles), and reported an out-of-bounds slice that does not exist in the file. Two
attempts and 44 minutes were spent sending a worker to fix code that was already correct.

The guard is deliberately narrow: it tests only the claim "this does not compile" and leaves
every other reason to reject untouched.
"""

import os
import shutil

import pytest

from factory import reviewguard
from factory.models import Profile


def profile_with(commands):
    """A valid native profile; `commands` is then set directly.

    The model refuses `commands=[]` for a native profile ("Native profiles require nonempty
    command argument arrays"), which is a real guard. To exercise the "profile declares nothing
    to run" branch the attribute is assigned after validation instead of being smuggled past it.
    """
    profile = Profile(runner="native", commands=[["true"]], artifact_dir="out")
    profile.commands = list(commands)
    return profile


def test_a_build_claim_is_detected():
    findings = ["Build-breaking: `foo.interface()` calls a field as a method",
                "Minor: variable name could be clearer"]
    matched = reviewguard.claims_build_failure(findings)
    assert len(matched) == 1
    assert "Build-breaking" in matched[0]


@pytest.mark.parametrize("phrase", [
    "the build is broken",
    "this does not compile",
    "it won't compile",
    "fails to compile",
    "compile error in main.zig",
    "namespace mismatch: std.io.Writer.Allocating",
    "no such field named interface",
])
def test_the_phrasings_a_reviewer_actually_uses(phrase):
    assert reviewguard.claims_build_failure([phrase]), phrase


def test_a_design_finding_is_not_a_build_claim():
    """A design or security finding must never be sent to the compiler and dropped."""
    for phrase in ("outline: none without :focus-visible", "colour alone carries state",
                   "fixed pixel width breaks at 360px"):
        assert reviewguard.claims_build_failure([phrase]) == [], phrase


def test_a_successful_build_disproves_the_claim(tmp_path):
    """The case that cost three attempts: the code compiled."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "ok.sh").write_text("#!/bin/sh\nexit 0\n")
    (worktree / "ok.sh").chmod(0o755)
    # A build command that succeeds, using a real executable on PATH rather than a shell string.
    profile = profile_with([["true"]])

    verdict = {"approved": False,
               "findings": ["Build-breaking: reader.interface() is not a function"]}
    resolved, evidence = reviewguard.reconcile(verdict, profile, worktree)

    assert evidence["checked"] and evidence["ran"] and evidence["ok"]
    assert resolved["findings"] == []
    assert resolved["approved"] is True
    assert resolved["build_claim_disproved"] == 1


def test_a_real_build_failure_keeps_the_finding(tmp_path):
    """When the compiler agrees, nothing changes: the guard must not launder a real defect."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    profile = profile_with([["false"]])

    verdict = {"approved": False, "findings": ["Build-breaking: it does not compile"]}
    resolved, evidence = reviewguard.reconcile(verdict, profile, worktree)

    assert evidence["ok"] is False
    assert resolved["findings"], "a genuine build failure must keep its finding"
    assert resolved["approved"] is False


def test_a_mixed_verdict_loses_only_the_build_claim(tmp_path):
    """A reviewer that also flagged a real security issue is still right about that."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    profile = profile_with([["true"]])

    verdict = {"approved": False,
               "findings": ["Build-breaking: reader.interface() is a method call",
                            "Security: the session cookie is not HttpOnly"]}
    resolved, _ = reviewguard.reconcile(verdict, profile, worktree)

    assert len(resolved["findings"]) == 1
    assert "HttpOnly" in resolved["findings"][0]
    assert resolved["approved"] is False, "one surviving finding still rejects the task"


def test_the_verdict_is_untouched_when_no_build_is_claimed(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    verdict = {"approved": False, "findings": ["Design: input has no label"]}
    resolved, evidence = reviewguard.reconcile(verdict, profile_with([["true"]]), worktree)
    assert evidence["checked"] is False
    assert resolved == verdict


def test_a_profile_without_build_commands_cannot_disprove_anything(tmp_path):
    """No commands means nothing to run, so the finding stands rather than being dropped."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    verdict = {"approved": False, "findings": ["Build-breaking: nope"]}
    resolved, evidence = reviewguard.reconcile(verdict, profile_with([]), worktree)
    assert evidence["ran"] is False
    assert resolved["findings"] == verdict["findings"]
    assert resolved["approved"] is False


def test_a_runtime_bug_is_not_a_build_claim():
    """A real Crash/DoS finding was dropped because the same review also claimed a build failure.

    `path[7..6]` is an out-of-bounds *slice*: it compiles and panics at runtime. The first
    version of `BUILD_CLAIM` matched "out-of-bounds slice" as a compile failure, so a correct
    build silently discarded a genuine crash bug. Only claims about *compiling* may be tested.
    """
    runtime_findings = [
        "Crash/DoS: POST /tasks/complete evaluates path[7..6], an out-of-bounds slice that panics",
        "A later successful save can permanently drop the task",
        "The session cookie is not HttpOnly",
        "Fixed pixel width breaks at 360px",
    ]
    for finding in runtime_findings:
        assert reviewguard.claims_build_failure([finding]) == [], finding


def test_the_real_review_keeps_its_runtime_bug_and_drops_only_build_claims(tmp_path):
    """Replays the live verdict: three build claims and two genuine findings."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    verdict = {"approved": False, "findings": [
        "Build-breaking: std.http.Server.init(stream_reader.interface(), ...) calls a field as a method",
        "Build-breaking namespace mismatch: std.io.Writer.Allocating",
        "Crash/DoS: path[7..6] is an out-of-bounds slice that panics and kills the server",
        "Data loss on failed delete: a failed save frees the removed task title",
    ]}
    resolved, evidence = reviewguard.reconcile(verdict, profile_with([["true"]]), worktree)

    assert evidence["disproved"] == 2, "only the two build claims are disproved"
    assert len(resolved["findings"]) == 2
    assert any("out-of-bounds" in f for f in resolved["findings"]), "the real crash bug survives"
    assert any("Data loss" in f for f in resolved["findings"])
    assert resolved["approved"] is False, "a surviving real defect still rejects the task"


def test_the_pinned_compiler_is_reachable_when_the_caller_path_lacks_it(tmp_path, monkeypatch):
    """A pinned FACTORY_ZIG must be usable even when PATH does not contain it.

    Measured: `zig build` answered "could not start: FileNotFoundError" from a process whose PATH
    lacked the install directory, while the same command ran in a shell that exported it. The
    guard would then report `ran: False` and keep a claim it could have disproved -- silently
    losing the whole benefit.
    """
    worktree = tmp_path / "wt"
    worktree.mkdir()
    binary = tmp_path / "fake-zig"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)

    monkeypatch.setenv("FACTORY_ZIG", str(binary))
    monkeypatch.setenv("PATH", "/nonexistent-directory")

    outcome = reviewguard.verify(profile_with([["fake-zig"]]), worktree)
    assert outcome["ran"] is True, outcome["output"]
    assert outcome["ok"] is True


# --- claims about tests need `zig test`, not `zig build` -----------------------------------

def test_a_test_claim_is_recognised_separately_from_a_build_claim():
    """`zig build` never type-checks or runs a `test` block.

    A live review said a `std.testing.expectEqual(null, optional)` test "does not type-check".
    It compiles and passes. The claim survived the guard because the guard only ran the build,
    which says nothing about a test body.
    """
    test_findings = [
        "the `noteIdFromPath` test uses expectEqual and does not type-check",
        "std.testing.expectError is used incorrectly here",
        "this does not type-check",
    ]
    for finding in test_findings:
        assert reviewguard.claims_test_failure([finding]), finding


def test_a_plain_build_claim_is_not_treated_as_a_test_claim():
    assert reviewguard.claims_test_failure(["Build-breaking: it will not compile"]) == []


@pytest.mark.skipif(shutil.which("zig") is None and not os.environ.get("FACTORY_ZIG"),
                    reason="Zig is not installed")
def test_a_wrong_test_claim_is_disproved_by_running_the_tests(tmp_path):
    """The exact wording from the live review, against a worktree whose test passes."""
    project = tmp_path / "zigproject"
    (project / "src").mkdir(parents=True)
    (project / "build.zig").write_text(
        'const std = @import("std");\n'
        'pub fn build(b: *std.Build) void {\n'
        '    const exe = b.addExecutable(.{.name="app", .root_module=b.createModule(.{\n'
        '        .root_source_file=b.path("src/main.zig"),\n'
        '        .target=b.standardTargetOptions(.{}),\n'
        '        .optimize=b.standardOptimizeOption(.{})})});\n'
        '    b.installArtifact(exe);\n'
        '}\n')
    (project / "src" / "main.zig").write_text(
        'const std = @import("std");\n'
        'pub fn noteIdFromPath(path: []const u8) ?usize { _ = path; return null; }\n'
        'test "returns null" { try std.testing.expectEqual(null, noteIdFromPath("/notes")); }\n'
        'pub fn main() void {}\n')

    verdict = {"approved": False, "findings": [
        "src/main.zig's `noteIdFromPath` test uses `std.testing.expectEqual(null, ?[]const u8)`, "
        "which does not type-check"]}
    resolved, evidence = reviewguard.reconcile(
        verdict, profile_with([["zig", "build", "-Doptimize=ReleaseSafe"]]), project)

    assert evidence["ran"] and evidence["ok"], evidence["output"]
    assert evidence["test_ran"], "a test claim must be settled by running the tests"
    assert resolved["findings"] == []
    assert resolved["approved"] is True


@pytest.mark.skipif(shutil.which("zig") is None and not os.environ.get("FACTORY_ZIG"),
                    reason="Zig is not installed")
def test_a_test_that_genuinely_fails_keeps_the_finding(tmp_path):
    """The guard must never launder a real failing test."""
    project = tmp_path / "zigproject"
    (project / "src").mkdir(parents=True)
    (project / "build.zig").write_text(
        'const std = @import("std");\n'
        'pub fn build(b: *std.Build) void {\n'
        '    const exe = b.addExecutable(.{.name="app", .root_module=b.createModule(.{\n'
        '        .root_source_file=b.path("src/main.zig"),\n'
        '        .target=b.standardTargetOptions(.{}),\n'
        '        .optimize=b.standardOptimizeOption(.{})})});\n'
        '    b.installArtifact(exe);\n'
        '}\n')
    # The function returns 5, the test expects null: the build succeeds, the test fails.
    (project / "src" / "main.zig").write_text(
        'const std = @import("std");\n'
        'pub fn noteIdFromPath(path: []const u8) ?usize { _ = path; return 5; }\n'
        'test "returns null" { try std.testing.expectEqual(null, noteIdFromPath("/notes")); }\n'
        'pub fn main() void {}\n')

    verdict = {"approved": False, "findings": ["the expectEqual test does not type-check"]}
    resolved, evidence = reviewguard.reconcile(
        verdict, profile_with([["zig", "build", "-Doptimize=ReleaseSafe"]]), project)

    assert evidence["test_ran"]
    assert resolved["findings"], "a genuinely failing test must keep its finding"
    assert resolved["approved"] is False


def test_an_api_shape_claim_is_recognised_without_the_word_compile():
    """The reviewer often states the defect, not the category.

    Measured: a live finding read "`const reader = stream_reader.interface();` calls `interface`
    as a function while ... treats it as a field ... are symmetric in Zig 0.15.x". It never used
    the word "compile", so the guard skipped it, the build was never run, and a correct file was
    rejected. These phrasings are decidable by compiling, so they must trigger the check.
    """
    shapes = [
        "calls `interface` as a function while the writer treats it as a field",
        "std.net.Stream.Reader/Writer are symmetric in Zig 0.15.x and expose interface",
        "the wrapper exposes interface as a field",
        "this is a method in Zig 0.15.2, not a field",
    ]
    for finding in shapes:
        assert reviewguard.claims_build_failure([finding]), finding


def test_runtime_and_design_findings_are_still_not_build_claims():
    """Broadening the pattern must not swallow real defects."""
    others = [
        "Crash/DoS: path[7..6] is an out-of-bounds slice that panics",
        "method_token is a slice into the reader's internal buffer and is invalidated",
        "Design: outline none without a focus-visible replacement",
        "Minor: the debug log prints the raw path, allowing terminal injection",
    ]
    for finding in others:
        assert reviewguard.claims_build_failure([finding]) == [], finding


def test_the_live_zig_api_claims_are_recognised():
    """Two of three findings from a real rejected attempt were false; none matched the pattern.

    The rejection read:
      "request.head.content_type is not a field on Zig 0.15.2 ..."
      "readerExpectNone(buffer) passes a buffer argument, but Zig 0.15.2's takes no buffer"
    Both were wrong -- `content_type` IS a field and `readerExpectNone` DOES take a buffer -- but
    neither matched `BUILD_CLAIM`, so `zig build` was never run and the task failed three times.
    """
    claims = [
        "Blocker: `request.head.content_type` is not a field on Zig 0.15.2 "
        "`std.http.Server.Request.Head`",
        "Build blocker: `request.readerExpectNone(buffer)` passes a buffer argument, but Zig "
        "0.15.2's readerExpectNone() takes no buffer",
        "the API takes no arguments in Zig 0.15.2",
        "remove the extra argument",
        "no such method on std.http.Server",
    ]
    for claim in claims:
        assert reviewguard.claims_build_failure([claim]), claim


def test_a_semantic_finding_is_still_not_a_build_claim():
    """Guard must not swallow the one genuine finding in that same rejection."""
    others = [
        "Robustness: readBody catches error.EndOfStream and returns an empty body, treating a "
        "truncated request as a normal POST",
        "statusFromCode maps every status outside its hardcoded list to 500",
        "path is a slice into the request head buffer and is used after the body is read",
    ]
    for finding in others:
        assert reviewguard.claims_build_failure([finding]) == [], finding


# --- the build runs BEFORE the review, so compilation is evidence not opinion ---------------

def test_a_successful_build_is_stated_in_the_prompt(tmp_path):
    """The reviewer is told the build passed, so a 'does not compile' finding contradicts it.

    This is the fix for the whole class of false findings: 11 of 51 findings across 15 Zig
    rejections (22%) asserted a compile error the compiler did not reproduce, and two whole
    rejections consisted of nothing else. Dropping them afterwards by matching phrasing is
    reactive -- new wordings kept slipping through. Showing the result makes it evidence.
    """
    build = {"ran": True, "ok": True, "output": "$ zig build\n", "commands": [["zig", "build"]]}
    block = reviewguard.prompt_block(build)
    assert "BUILD RESULT" in block
    assert "SUCCEEDED" in block
    assert "does not compile" in block, "the prompt must name the claim it forbids"
    assert "behaviour, security or correctness defect" in block, "and redirect to what is valid"


def test_a_failed_build_gives_the_reviewer_the_compiler_error(tmp_path):
    """A real failure must be quoted from the compiler, not paraphrased by the model."""
    build = {"ran": True, "ok": False,
             "output": "src/main.zig:12:9: error: no field named 'content_type'",
             "commands": [["zig", "build"]]}
    block = reviewguard.prompt_block(build)
    assert "FAILED" in block
    assert "no field named 'content_type'" in block
    assert "authoritative" in block


def test_no_build_means_no_block(tmp_path):
    """A profile with no build commands must not get an empty 'BUILD RESULT' heading."""
    assert reviewguard.prompt_block({"ran": False, "ok": False, "output": ""}) == ""
    assert reviewguard.prompt_block(None) == ""


def test_precheck_reports_what_the_build_did(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    ok = reviewguard.precheck(profile_with([["true"]]), worktree)
    assert ok["ran"] and ok["ok"]
    bad = reviewguard.precheck(profile_with([["false"]]), worktree)
    assert bad["ran"] and not bad["ok"]


def test_reconcile_reuses_the_precheck_build_instead_of_running_it_twice(tmp_path):
    """The guard must add one build, not two.

    `review_for` runs `precheck` before the review and passes the result here. Recompiling would
    double the only cost this guard adds.
    """
    worktree = tmp_path / "wt"
    worktree.mkdir()
    # A build command that would FAIL if it ran, so a passing verdict proves it was not re-run.
    stale = {"ran": True, "ok": True, "output": "reused", "commands": [["false"]]}
    verdict = {"approved": False, "findings": ["Build-breaking: this will not compile"]}

    resolved, evidence = reviewguard.reconcile(
        verdict, profile_with([["false"]]), worktree, build=stale)

    assert evidence["ok"] is True, "the passed-in build must be used"
    assert resolved["approved"] is True
