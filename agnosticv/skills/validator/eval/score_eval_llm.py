#!/usr/bin/env python3
"""
AgnosticV Validator Eval Suite — LLM-based scoring script.

Sends each fixture's YAML content to Claude via Vertex AI along with
the validation rules, then compares the LLM's structured JSON output
against expected.json.

Requires:
    pip install anthropic[vertex] pyyaml
    gcloud auth application-default login

Environment variables (auto-detected from gcloud if not set):
    ANTHROPIC_VERTEX_PROJECT_ID  — GCP project ID
    CLOUD_ML_REGION              — GCP region (default: global)
    EVAL_MODEL                   — Claude model (default: claude-sonnet-4-6)

Usage:
    python score_eval_llm.py           # human-readable report
    python score_eval_llm.py --json    # machine-readable JSON output
"""

import json
import os
import re
import sys
from datetime import date

try:
    from anthropic import AnthropicVertex
except ImportError:
    sys.exit(
        "ERROR: anthropic[vertex] is required.\n"
        "Install with:  pip install 'anthropic[vertex]'"
    )

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURES_DIR = os.path.join(SCRIPT_DIR, "fixtures")
CLEAN_DIR = os.path.join(FIXTURES_DIR, "clean")
BROKEN_DIR = os.path.join(FIXTURES_DIR, "broken")
PROMPT_TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "prompt_template.md")

PROJECT_ID = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
REGION = os.environ.get("CLOUD_ML_REGION", "")
MODEL = os.environ.get("EVAL_MODEL", "claude-sonnet-4-6")

if not PROJECT_ID or not REGION:
    sys.exit(
        "ERROR: Set ANTHROPIC_VERTEX_PROJECT_ID and CLOUD_ML_REGION.\n"
        "Example:\n"
        "  export ANTHROPIC_VERTEX_PROJECT_ID=my-gcp-project\n"
        "  export CLOUD_ML_REGION=us-east5"
    )


def load_prompt_template() -> str:
    with open(PROMPT_TEMPLATE_PATH) as f:
        return f.read()


def build_prompt(template: str, fixture_path: str) -> str:
    """Build the full prompt with fixture YAML content and file listing."""
    dir_name = os.path.basename(os.path.normpath(fixture_path))
    files = sorted(os.listdir(fixture_path))
    display_files = [f for f in files if f != "expected.json"]

    yaml_contents = {}
    for f in display_files:
        fpath = os.path.join(fixture_path, f)
        if os.path.isfile(fpath) and (f.endswith(".yaml") or f.endswith(".adoc")):
            try:
                with open(fpath) as fh:
                    yaml_contents[f] = fh.read()
            except Exception:
                yaml_contents[f] = "<could not read file>"

    prompt = template.replace("{today_date}", str(date.today()))

    parts = [prompt, "", "---", ""]
    parts.append(f"CATALOG_DIRECTORY_NAME: {dir_name}")
    parts.append("")
    parts.append(f"FILES PRESENT IN DIRECTORY: {', '.join(display_files)}")
    parts.append("")

    for filename, content in yaml_contents.items():
        parts.append(f"--- {filename} ---")
        parts.append(content)
        parts.append("")

    return "\n".join(parts)


def extract_json(text: str) -> dict:
    """Extract the first complete JSON object from text using brace balancing."""
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON found in LLM response: {text[:200]}")

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])

    raise ValueError(f"Unbalanced JSON in LLM response: {text[:200]}")


def call_llm(client: AnthropicVertex, prompt: str) -> dict:
    """Send prompt to Claude and parse JSON response."""
    message = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text.strip()
    return extract_json(response_text)


def discover_fixtures(base_dir: str) -> list[str]:
    if not os.path.isdir(base_dir):
        return []
    return sorted(
        os.path.join(base_dir, d)
        for d in os.listdir(base_dir)
        if os.path.isdir(os.path.join(base_dir, d))
    )


def evaluate_clean(client: AnthropicVertex, template: str, fixture_path: str) -> dict:
    name = os.path.basename(fixture_path)
    try:
        prompt = build_prompt(template, fixture_path)
        result = call_llm(client, prompt)
        error_count = len(result.get("errors", []))
        passed = error_count == 0
        return {
            "name": name,
            "type": "clean",
            "passed": passed,
            "detail": "0 errors (expected: 0)" if passed
                      else f"{error_count} errors (expected: 0): "
                           + ", ".join(e.get("check", "?") for e in result["errors"]),
            "llm_output": result,
        }
    except Exception as exc:
        return {
            "name": name,
            "type": "clean",
            "passed": False,
            "detail": f"LLM call failed: {exc}",
            "llm_output": None,
        }


def evaluate_broken(client: AnthropicVertex, template: str, fixture_path: str) -> dict:
    name = os.path.basename(fixture_path)
    expected_file = os.path.join(fixture_path, "expected.json")

    if not os.path.isfile(expected_file):
        return {
            "name": name,
            "type": "broken",
            "passed": False,
            "detail": "missing expected.json",
            "llm_output": None,
        }

    with open(expected_file) as f:
        expected = json.load(f)

    try:
        prompt = build_prompt(template, fixture_path)
        result = call_llm(client, prompt)
    except Exception as exc:
        return {
            "name": name,
            "type": "broken",
            "passed": False,
            "detail": f"LLM call failed: {exc}",
            "llm_output": None,
        }

    issues: list[str] = []

    actual_error_checks = {e.get("check", "") for e in result.get("errors", [])}
    for exp_err in expected.get("expect_errors", []):
        if exp_err["check"] not in actual_error_checks:
            issues.append(f"expected error '{exp_err['check']}' not raised")

    actual_warning_checks = {w.get("check", "") for w in result.get("warnings", [])}
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
        "llm_output": result,
    }


def main():
    output_json = "--json" in sys.argv

    template = load_prompt_template()
    client = AnthropicVertex(region=REGION, project_id=PROJECT_ID)

    print(f"\nConnecting to Vertex AI ({PROJECT_ID} / {REGION})")
    print(f"Model: {MODEL}\n")

    clean_fixtures = discover_fixtures(CLEAN_DIR)
    broken_fixtures = discover_fixtures(BROKEN_DIR)

    if not clean_fixtures and not broken_fixtures:
        print("No fixtures found.", file=sys.stderr)
        sys.exit(2)

    results: list[dict] = []
    total = len(clean_fixtures) + len(broken_fixtures)

    for i, fp in enumerate(clean_fixtures, 1):
        name = os.path.basename(fp)
        if not output_json:
            print(f"  [{i}/{total}] Evaluating clean/{name}...", end=" ", flush=True)
        r = evaluate_clean(client, template, fp)
        results.append(r)
        if not output_json:
            print("PASS" if r["passed"] else "FAIL")

    for i, fp in enumerate(broken_fixtures, len(clean_fixtures) + 1):
        name = os.path.basename(fp)
        if not output_json:
            print(f"  [{i}/{total}] Evaluating broken/{name}...", end=" ", flush=True)
        r = evaluate_broken(client, template, fp)
        results.append(r)
        if not output_json:
            print("PASS" if r["passed"] else "FAIL")

    passed = sum(1 for r in results if r["passed"])

    if output_json:
        output = {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "model": MODEL,
            "results": [
                {k: v for k, v in r.items() if k != "llm_output"}
                for r in results
            ],
        }
        print(json.dumps(output, indent=2))
    else:
        print()
        print("AgnosticV Validator Eval Suite (LLM)")
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
        print(f"Model:  {MODEL}")
        print()

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
