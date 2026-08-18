#!/usr/bin/env python3
"""
AgnosticV Validator Eval Suite — scoring script.

Discovers all fixtures in fixtures/clean/ and fixtures/broken/, runs
agv_checker against each, and compares results to expectations.

- Clean fixtures must produce 0 errors.
- Broken fixtures must produce the specific errors/warnings listed in
  their expected.json.

Exit code: 0 if all pass, 1 if any fail.

Usage:
    python score_eval.py           # human-readable report
    python score_eval.py --json    # machine-readable JSON output
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from agv_checker import run_checks  # noqa: E402


FIXTURES_DIR = os.path.join(SCRIPT_DIR, "fixtures")
CLEAN_DIR = os.path.join(FIXTURES_DIR, "clean")
BROKEN_DIR = os.path.join(FIXTURES_DIR, "broken")


def discover_fixtures(base_dir: str) -> list[str]:
    """Return sorted list of fixture directory paths under *base_dir*."""
    if not os.path.isdir(base_dir):
        return []
    return sorted(
        os.path.join(base_dir, d)
        for d in os.listdir(base_dir)
        if os.path.isdir(os.path.join(base_dir, d))
    )


def evaluate_clean(fixture_path: str) -> dict:
    """Run checker on a clean fixture; expect 0 errors."""
    name = os.path.basename(fixture_path)
    result = run_checks(fixture_path)
    error_count = len(result["errors"])
    passed = error_count == 0
    return {
        "name": name,
        "type": "clean",
        "passed": passed,
        "detail": f"0 errors (expected: 0)" if passed
                  else f"{error_count} errors (expected: 0): "
                       + ", ".join(e["check"] for e in result["errors"]),
        "checker_output": result,
    }


def evaluate_broken(fixture_path: str) -> dict:
    """Run checker on a broken fixture; compare against expected.json."""
    name = os.path.basename(fixture_path)
    expected_file = os.path.join(fixture_path, "expected.json")

    if not os.path.isfile(expected_file):
        return {
            "name": name,
            "type": "broken",
            "passed": False,
            "detail": "missing expected.json",
            "checker_output": None,
        }

    with open(expected_file) as f:
        expected = json.load(f)

    result = run_checks(fixture_path)
    issues: list[str] = []

    actual_error_checks = {e["check"] for e in result["errors"]}
    for exp_err in expected.get("expect_errors", []):
        if exp_err["check"] not in actual_error_checks:
            issues.append(f"expected error '{exp_err['check']}' not raised")

    actual_warning_checks = {w["check"] for w in result["warnings"]}
    for exp_warn in expected.get("expect_warnings", []):
        if exp_warn["check"] not in actual_warning_checks:
            issues.append(f"expected warning '{exp_warn['check']}' not raised")

    passed = len(issues) == 0
    if passed:
        caught = []
        for e in expected.get("expect_errors", []):
            caught.append(f"{e['check']} error")
        for w in expected.get("expect_warnings", []):
            caught.append(f"{w['check']} warning")
        detail = "caught: " + ", ".join(caught)
    else:
        detail = "; ".join(issues)

    return {
        "name": name,
        "type": "broken",
        "passed": passed,
        "detail": detail,
        "checker_output": result,
    }


def main():
    output_json = "--json" in sys.argv

    clean_fixtures = discover_fixtures(CLEAN_DIR)
    broken_fixtures = discover_fixtures(BROKEN_DIR)

    if not clean_fixtures and not broken_fixtures:
        print("No fixtures found.", file=sys.stderr)
        sys.exit(2)

    results: list[dict] = []

    for fp in clean_fixtures:
        results.append(evaluate_clean(fp))

    for fp in broken_fixtures:
        results.append(evaluate_broken(fp))

    total = len(results)
    passed = sum(1 for r in results if r["passed"])

    if output_json:
        output = {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "results": [
                {k: v for k, v in r.items() if k != "checker_output"}
                for r in results
            ],
        }
        print(json.dumps(output, indent=2))
    else:
        print()
        print("AgnosticV Validator Eval Suite")
        print("=" * 40)

        clean_results = [r for r in results if r["type"] == "clean"]
        broken_results = [r for r in results if r["type"] == "broken"]

        if clean_results:
            print("\nClean fixtures:")
            for r in clean_results:
                status = "PASS" if r["passed"] else "FAIL"
                print(f"  [{status}] {r['name']:30s} -- {r['detail']}")

        if broken_results:
            print("\nBroken fixtures:")
            for r in broken_results:
                status = "PASS" if r["passed"] else "FAIL"
                print(f"  [{status}] {r['name']:30s} -- {r['detail']}")

        print(f"\nResult: {passed}/{total} passed")
        print()

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
