"""Turn a failing gate into a repair instruction a worker can act on.

Phase 0 closed, so a build can now run to a published artifact. What it cannot do is survive a
failing acceptance case: `verify()` raises, the job dies, and the operator is left with a
report and a screenshot to read by hand. This module is the missing link -- it reads the
report the tester already wrote and renders the failure as an instruction.

Two design choices matter more than the code.

First, the instruction quotes **what the test expected and what the page actually had**, not
just the case name. A worker told "homepage_content failed" will guess; a worker told which
selector was asserted, with which expected text, and what the element contained can act.

Second, the repair worker receives the **failing evidence, not the whole transcript**. The
point of the gate is independent judgement; handing the worker its own previous transcript
would invite it to argue with the reviewer instead of fixing the page.
"""
from pathlib import Path
import json


MAX_CASES = 8


def failing_cases(report_path: Path) -> list[dict]:
    """Read a tester report and return the cases that failed, with their messages."""
    if not report_path.is_file():
        return []
    try:
        report = json.loads(report_path.read_text())
    except (ValueError, OSError):
        return []
    failures = []
    for case in report.get("cases", []):
        if case.get("status") == "passed":
            continue
        failures.append({
            "flow_id": case.get("flow_id", ""),
            "name": case.get("name", ""),
            "kind": case.get("kind", ""),
            "status": case.get("status", ""),
            "message": (case.get("message") or case.get("error") or "")[:1500],
            "screenshot": case.get("screenshot", ""),
        })
    return failures[:MAX_CASES]


def describe_evidence(work: Path) -> dict:
    """Everything a repair worker should be told about why the gate closed."""
    failures = failing_cases(work / "testing" / "report.json")
    verification = work / "verification.txt"
    return {
        "failures": failures,
        # A build or operator-check failure has no per-case report; the log is the evidence.
        "verification_tail": (verification.read_text()[-4000:] if verification.is_file() else ""),
        "evidence_dir": str(work / "testing"),
    }


def repair_instruction(evidence: dict) -> str:
    """Render the evidence as a single, concrete repair brief."""
    failures = evidence.get("failures") or []
    lines = [
        "A verification gate failed on this project. Your job is to make the failing checks "
        "pass by fixing the application, not by weakening the checks.",
        "",
        "Rules:",
        "- Do not edit or delete the test suite, the checks, or anything under a `checks/` path.",
        "- Do not delete an assertion to make it pass, and do not change expected copy to match "
        "a wrong page. The expected values are the contract the operator approved.",
        "- Work only inside this directory.",
        "",
    ]
    if failures:
        lines.append("FAILING ACCEPTANCE CASES:")
        for index, case in enumerate(failures, 1):
            lines.append(f"{index}. [{case['kind']}] {case['flow_id']} - {case['name']}")
            if case["message"]:
                for chunk in case["message"].splitlines():
                    if chunk.strip():
                        lines.append("   " + chunk.strip())
            if case["screenshot"]:
                lines.append(f"   Screenshot recorded at: {case['screenshot']}")
        lines.append("")
        lines.append("Each message above is the assertion that failed. Read the page code, find "
                     "why the rendered result differs from what was asserted, and fix the page.")
    elif evidence.get("verification_tail"):
        lines.append("THE BUILD OR AN OPERATOR CHECK FAILED. Tail of the verification log:")
        lines.append("")
        lines.append(evidence["verification_tail"])
        lines.append("")
        lines.append("Fix the cause of that failure. Do not remove the failing command or check.")
    else:
        lines.append("The gate failed but no evidence was captured. Inspect the build and the "
                     "application for an obvious defect, and report what you changed.")
    return "\n".join(lines)
