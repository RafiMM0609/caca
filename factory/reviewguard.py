"""Check a reviewer's build claims against the actual compiler.

**The defect this exists for.** A live build was rejected three times with findings that began
"Build-breaking: ..." and "the build is broken". The worker's tree compiled clean and served HTTP:

    $ zig build -Doptimize=ReleaseSafe   -> exit 0, zig-out/bin/app produced
    $ ./zig-out/bin/app
    {"status":"ok"}

Three of the reviewer's claims were wrong in the same way. It said `std.net.Stream.Reader`
exposes `interface` as a field; in Zig 0.15.2 that is a *method* (`pub fn interface(r: *Reader)`)
while the writer's is a field. It said `std.io.Writer.Allocating` was a namespace mismatch; both
compile. It reported an out-of-bounds slice that does not appear in the file at all.

A reviewer is the only gate before merge, so a wrong verdict is not a minor annoyance: the
correct code is rejected, the worker is told to fix something that is already right, and the
attempt is spent. Two of those attempts cost 44 minutes.

**What this module does, and does not, do.** It runs the profile's own build commands in the
worktree when — and only when — a review claims the build is broken. If they succeed, the claim
is disproved and the finding is dropped. If they fail, the compiler's own message replaces the
reviewer's guess. It never *approves* anything: a review that fails on design, safety or
completeness is untouched. Only the narrow, checkable claim "this does not compile" is put to
the compiler.

**Why the build is bounded.** The worktree already passed a worker and a review; running a full
build twice more would be wasteful, so it runs once with a tight ceiling and treats a timeout as
"cannot confirm" rather than as failure. An unconfirmed claim is kept, not silently dropped —
the safer error is to keep a finding the compiler could not settle.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

# Phrases a reviewer uses when it believes the code does not compile. Kept as a small, explicit
# list rather than a heuristic: this module acts on the meaning, so a false positive would
# override a legitimate finding.
#
# `out-of-bounds slice` and `panic` are deliberately ABSENT. They describe runtime behaviour, not
# a compile failure -- a first version matched "out-of-bounds slice" and wrongly dropped a real
# Crash/DoS finding about `path[7..6]` merely because the same review also made build claims.
# A claim about a *test* is a different assertion from a claim about the build. `zig build` does
# not type-check or run a `test` block, so a reviewer saying "this test does not type-check" was
# confirmed by a green build and survived the guard — measured on a live task, where the claim
# was also wrong: `std.testing.expectEqual(null, anOptional)` compiles and passes.
TEST_CLAIM = re.compile(
    r"\b(test (?:does not|doesn't) (?:type-check|compile)|"
    r"(?:does not|doesn't|will not|won't) type-check|"
    r"expectEqual|expectError|std\.testing|"
    r"fails? to type-check|type error)\b",
    re.IGNORECASE)

BUILD_CLAIM = re.compile(
    r"\b(build[- ]breaking|build (?:is )?broken|does not compile|doesn't compile|"
    r"will not compile|won't compile|"
    r"fails? to compile|compile (?:error|failure|blocker)|compilation error|"
    r"unresolvable|no such (?:field|member|declaration)|namespace mismatch|"
    # A reviewer often states the defect without naming compilation: "calls `interface` as a
    # function while the next line treats it as a field", "are symmetric in Zig 0.15.x and expose
    # ...". Measured: a live finding phrased exactly like that was skipped by the guard, so the
    # build was never run and the wrong claim rejected the task. These name the API-shape
    # mistakes that are decidable by compiling.
    r"calls? \S+ as a (?:function|field|method)|"
    r"treats? it as a (?:function|field|method)|"
    r"expose[sd]? \S+ as a (?:field|method|function)|"
    r"is (?:a|an) (?:field|method|function) in Zig|"
    r"are symmetric in Zig|"
    r"(?:not|isn't|is not) symmetric|"
    # Measured on a live Zig task: three attempts were rejected with claims phrased like these,
    # and two of the three were false. None matched, so the compiler was never consulted.
    #   "request.head.content_type is not a field on Zig 0.15.2 ..."
    #   "readerExpectNone(buffer) passes a buffer argument, but Zig 0.15.2's ... takes no buffer"
    r"is not a field on Zig|is not a method on Zig|"
    r"takes no buffer|takes no argument|takes no arguments|"
    r"passes a[n]? (?:buffer|argument) (?:argument )?(?:but|while|whereas)|"
    r"does not exist (?:on|in) Zig|not present (?:on|in) Zig|"
    r"no such (?:method|function|field) (?:on|in)|"
    r"remove the extra argument)\b",
    re.IGNORECASE)

# A short ceiling. A worktree that compiles does so quickly with a warm shared cache; anything
# slower is a cold cache or a genuinely broken tree, and neither is worth blocking a review on.
DEFAULT_TIMEOUT = 300


def claims_build_failure(findings):
    """True when any finding asserts the code does not compile.

    Returns the matching findings so the caller can report which claim was tested, not merely
    that one was.
    """
    return [finding for finding in findings or [] if BUILD_CLAIM.search(finding or "")]


def claims_test_failure(findings):
    """Findings that assert a test does not type-check, compile or pass.

    Checked separately because `zig build` never looks at a `test` block, so a green build is not
    evidence about a test claim.
    """
    return [finding for finding in findings or [] if TEST_CLAIM.search(finding or "")]


def build_commands(profile):
    """The profile's own build commands, or an empty list when it declares none.

    Zip/Nuxt profiles share this path, so nothing here is Zig-specific: whatever the operator
    configured is what gets run.
    """
    return [list(command) for command in (getattr(profile, "commands", None) or []) if command]


def verify_test(worktree, profile, timeout=DEFAULT_TIMEOUT):
    """Run the profile's build, then `zig test` over the sources it produced.

    Returns the same shape as `verify`. `zig test` takes one root file, so the profile's own
    build is run first and every `*_test.zig` or file containing a `test` block is tried; a file
    with no test block is skipped rather than failed.
    """
    built = verify(profile, worktree, timeout=timeout)
    if not built["ran"]:
        return built

    zig = zig_binary_from_env()
    if not zig:
        return built

    import subprocess

    candidates = []
    for path in sorted(Path(worktree).rglob("*.zig")):
        if any(part in (".zig-cache", "zig-out") for part in path.parts):
            continue
        try:
            if "test " in path.read_text(errors="replace"):
                candidates.append(path)
        except OSError:
            continue
    if not candidates:
        return {"ok": built["ok"], "ran": built["ran"], "commands": built["commands"],
                "output": built["output"] + "\n[no test block found; zig test not run]"}

    env = _build_env(worktree)
    transcript = [built["output"], "[zig test]"]
    for path in candidates[:5]:
        relative = path.relative_to(Path(worktree))
        transcript.append("$ zig test " + str(relative))
        try:
            completed = subprocess.run(
                [zig, "test", str(relative), "--cache-dir", str(Path(worktree) / ".zig-cache"),
                 "--global-cache-dir", str(shared_zig_cache())],
                cwd=worktree, env=env, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            transcript.append(f"[could not run zig test: {type(exc).__name__}]")
            continue
        tail = ((completed.stdout or "") + (completed.stderr or "")).strip()
        if tail:
            transcript.append(tail[-3000:])
        if completed.returncode != 0:
            return {"ok": False, "ran": True, "commands": built["commands"],
                    "output": "\n".join(transcript)[-8000:]}
    return {"ok": True, "ran": True, "commands": built["commands"],
            "output": "\n".join(transcript)[-8000:]}


def zig_binary_from_env():
    """The pinned compiler, or one from PATH. Imported lazily to avoid a cycle."""
    from .zigapi import zig_binary
    return zig_binary() if shutil.which(zig_binary()) or os.path.isabs(zig_binary()) else None


def shared_zig_cache():
    from .zigapi import shared_cache_dir
    return shared_cache_dir()


def _build_env(worktree):
    from .local_app import build_env
    env = build_env(worktree)
    env.setdefault("CI", "true")
    pinned = os.environ.get("FACTORY_ZIG", "")
    if pinned:
        env["PATH"] = str(Path(pinned).parent) + os.pathsep + env.get("PATH", "")
    return env


def verify(profile, worktree, timeout=DEFAULT_TIMEOUT):
    """Run the profile's build in `worktree`.

    Returns `{"ok": bool, "ran": bool, "output": str, "commands": [...]}`. `ran` is False when
    the profile declares no commands (nothing to test) or the build could not be started; the
    caller must treat that as "cannot confirm" and keep the finding.
    """
    commands = build_commands(profile)
    if not commands:
        return {"ok": False, "ran": False, "output": "the profile declares no build commands",
                "commands": []}

    from .local_app import build_env

    env = build_env(worktree)
    env.setdefault("CI", "true")
    # A pinned compiler must stay reachable from the build environment. `build_env` forwards the
    # caller's PATH, which is enough for the scheduler but not for a process started from a shell
    # without the install directory on PATH -- measured: `zig build` answered
    # "could not start: FileNotFoundError" while the same command ran fine in a shell that had
    # `~/.local/bin` exported. Prepending the pinned binary's directory makes the check
    # independent of how the caller was launched.
    pinned = os.environ.get("FACTORY_ZIG", "")
    if pinned:
        pinned_dir = str(Path(pinned).parent)
        env["PATH"] = pinned_dir + os.pathsep + env.get("PATH", "")
    transcript = []
    for command in commands:
        transcript.append("$ " + " ".join(command))
        try:
            completed = subprocess.run(command, cwd=worktree, env=env, timeout=timeout,
                                       capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            # Not a failure: the claim is unconfirmed, so the caller keeps the finding.
            return {"ok": False, "ran": False, "commands": commands,
                    "output": "\n".join(transcript) + f"\n[build timed out after {timeout}s]"}
        except (OSError, ValueError) as exc:
            return {"ok": False, "ran": False, "commands": commands,
                    "output": "\n".join(transcript) + f"\n[could not start: {type(exc).__name__}]"}
        tail = ((completed.stdout or "") + (completed.stderr or "")).strip()
        if tail:
            transcript.append(tail[-4000:])
        if completed.returncode != 0:
            return {"ok": False, "ran": True, "commands": commands,
                    "output": "\n".join(transcript)[-8000:]}
    return {"ok": True, "ran": True, "commands": commands,
            "output": "\n".join(transcript)[-8000:]}


def precheck(profile, worktree, timeout=DEFAULT_TIMEOUT):
    """Run the profile's build before the reviewer sees the diff.

    This is the fix for the whole class of false findings. A reviewer that recalls an API from
    training data can be wrong about whether code compiles -- measured across 15 Zig rejections,
    11 of 51 findings (22%) asserted a compile error that the compiler did not reproduce, and two
    whole rejections consisted of nothing else. `reconcile()` already dropped those *after* the
    fact by matching phrasing, but matching phrasing is reactive: a new wording slipped through
    every time (two such wordings were found in one session).

    Running the build first turns the question into a fact the reviewer is given rather than a
    claim it invents:

      - build fails  -> the prompt carries the compiler's own error, so the reviewer cites it
                        instead of guessing, and the worker gets the real error on retry;
      - build passes -> the prompt states that it passed, so a "this does not compile" finding
                        contradicts evidence the reviewer was shown.

    Cost: one build per task, which for this project is 0.11 s warm and 16 s cold -- negligible
    against a worker run measured in minutes. The result is cached per worktree because
    `reconcile()` would otherwise run the same build again.
    """
    outcome = verify(profile, worktree, timeout=timeout)
    return {
        "ran": outcome["ran"],
        "ok": outcome["ok"],
        "output": outcome["output"][-4000:],
        "commands": outcome["commands"],
    }


def prompt_block(build):
    """The measured build result, worded so a reviewer cannot mistake it for an opinion."""
    if not build or not build.get("ran"):
        return ""
    if build["ok"]:
        return (
            "\n\nBUILD RESULT (measured by running this profile's build commands on this "
            "worktree just now):\n"
            "The build SUCCEEDED. Do not report that a file does not compile, does not "
            "type-check, or uses a nonexistent API: that claim is disproved by evidence above "
            "you. If you believe the code is wrong, report a behaviour, security or "
            "correctness defect and describe what it does wrong at runtime.")
    return (
        "\n\nBUILD RESULT (measured by running this profile's build commands on this "
        "worktree just now):\n"
        "The build FAILED. The compiler output below is authoritative -- cite it, do not "
        "paraphrase it from memory.\n```\n" + build["output"] + "\n```")


def reconcile(verdict, profile, worktree, timeout=DEFAULT_TIMEOUT, build=None):
    """Return `(verdict, evidence)` with a disproved build claim removed.

    The verdict is returned unchanged when no finding claims a build failure, when the profile
    has no build commands, or when the build genuinely fails. Only a *successful* build against
    a *claimed* failure changes anything, because that is the one case where the reviewer is
    demonstrably wrong about a fact rather than expressing a judgement.
    """
    findings = list(verdict.get("findings") or [])
    claimed = claims_build_failure(findings)
    test_claimed = claims_test_failure(findings)
    # A finding can assert both ("this test does not compile"), so a claim is only tested once.
    checkable = [finding for finding in findings
                 if finding in claimed or finding in test_claimed]
    if not checkable:
        return verdict, {"checked": False, "reason": "no finding claims a build or test failure"}

    # `zig build` covers build claims; `zig test` covers claims about test bodies, which the
    # build never type-checks or runs. Both are run when both kinds of claim are present, so a
    # wrong claim of either kind is caught.
    # Reuse the build the reviewer was shown when the caller already ran it. Recompiling the same
    # worktree twice would double the one cost this guard adds.
    outcome = build if build is not None else verify(profile, worktree, timeout=timeout)
    tested = None
    if test_claimed and outcome["ran"] and outcome["ok"]:
        tested = verify_test(worktree, profile, timeout=timeout)
        if tested["ran"] and not tested["ok"]:
            # The tests genuinely fail; keep everything and report the compiler's output.
            outcome = tested
        elif tested["ran"]:
            outcome = tested

    evidence = {"checked": True,
                "claimed": [claim[:400] for claim in claimed],
                "test_claimed": [claim[:400] for claim in test_claimed],
                "ran": outcome["ran"], "ok": outcome["ok"],
                "test_ran": bool(tested and tested["ran"]),
                "output": outcome["output"][-4000:]}

    if not (outcome["ran"] and outcome["ok"]):
        # The compiler agrees with the reviewer, or could not settle it. Either way the finding
        # stands and the output is attached as evidence.
        return verdict, evidence

    # The build (and every test) succeeded, so every claim that something does not compile or
    # does not type-check is wrong. Drop exactly those findings and keep the rest: a reviewer
    # that also flagged a real security issue is still right about that.
    surviving = [finding for finding in findings if finding not in checkable]
    disproved = len(checkable)
    updated = dict(verdict)
    updated["findings"] = surviving
    # A rejection whose only reason was a disproved claim falls with it. If other findings
    # remain, the verdict is unchanged and those findings still decide.
    if verdict.get("approved") is False and not surviving:
        updated["approved"] = True
        updated["build_claim_disproved"] = disproved
    evidence["disproved"] = disproved
    evidence["surviving_findings"] = len(surviving)
    return updated, evidence
