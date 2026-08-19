#!/usr/bin/env python3
"""
AgnosticV Validator Eval Suite — Full skill evaluation with tool use.

Runs the ACTUAL validator skill (SKILL.md + sub-agent definitions) against
each fixture using Claude via Vertex AI with an agentic tool-use loop.
Claude can call bash commands just like it would in Claude Code.

This is the real end-to-end test — not a condensed prompt, but the full
2700-line skill with all sub-agent logic.

Requires:
    pip install anthropic[vertex] pyyaml
    gcloud auth application-default login

Environment variables:
    ANTHROPIC_VERTEX_PROJECT_ID  — GCP project ID (required)
    CLOUD_ML_REGION              — GCP region (required)
    EVAL_MODEL                   — Claude model (default: claude-sonnet-4-6)

Usage:
    python score_eval_skill.py           # human-readable report
    python score_eval_skill.py --json    # machine-readable JSON output
"""

import json
import os
import re
import subprocess
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
SKILL_DIR = os.path.dirname(SCRIPT_DIR)  # agnosticv/skills/validator/
AGENTS_DIR = os.path.join(os.path.dirname(os.path.dirname(SKILL_DIR)), "agents")
FIXTURES_DIR = os.path.join(SCRIPT_DIR, "fixtures")
CLEAN_DIR = os.path.join(FIXTURES_DIR, "clean")
BROKEN_DIR = os.path.join(FIXTURES_DIR, "broken")

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

MAX_TOOL_ROUNDS = 25
BASH_TIMEOUT = 10

CHECK_ALIASES = {
    "uuid_format": {"uuid_format", "uuid"},
    "uuid": {"uuid", "uuid_format"},
    "category_validation": {"category_validation", "category"},
    "category": {"category", "category_validation"},
    "best_practices": {"best_practices"},
    "file_structure": {"file_structure"},
    "yaml_syntax": {"yaml_syntax"},
    "password_pattern": {"password_pattern"},
    "ee_image_date": {"ee_image_date"},
    "untagged_images": {"untagged_images"},
    "catalog_name_length": {"catalog_name_length"},
    "deployer": {"deployer"},
    "anarchy_namespace": {"anarchy_namespace"},
    "stage_files": {"stage_files", "yaml_syntax"},
    "reporting_labels": {"reporting_labels"},
    "workloads": {"workloads", "workload_format"},
    "workload_format": {"workload_format", "workloads"},
    "collections": {"collections", "collection_versions", "tag_pattern"},
    "collection_versions": {"collection_versions", "collections"},
    "tag_pattern": {"tag_pattern", "collections"},
    "description": {"description", "file_structure"},
}

BASH_TOOL = {
    "name": "bash",
    "description": (
        "Execute a bash command and return stdout+stderr. "
        "Use for ls, cat, grep, python3 one-liners, etc."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to execute",
            }
        },
        "required": ["command"],
    },
}


def load_skill_files() -> str:
    """Load SKILL.md and all sub-agent .md files into a combined prompt."""
    parts = []

    skill_path = os.path.join(SKILL_DIR, "SKILL.md")
    with open(skill_path) as f:
        parts.append("# === MAIN SKILL: agnosticv:validator ===\n")
        parts.append(f.read())

    agent_files = [
        "schema-checker.md",
        "metadata-checker.md",
        "workload-checker.md",
        "sandbox-checker.md",
        "ocp-infra-checker.md",
    ]
    for agent_file in agent_files:
        agent_path = os.path.join(AGENTS_DIR, agent_file)
        if os.path.isfile(agent_path):
            with open(agent_path) as f:
                parts.append(f"\n\n# === SUB-AGENT: {agent_file} ===\n")
                parts.append(f.read())

    return "\n".join(parts)


def run_bash(command: str, fixture_path: str) -> str:
    """Execute a bash command, sandboxed to the fixture area."""
    try:
        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=BASH_TIMEOUT,
            cwd=fixture_path,
        )
        output = result.stdout
        if result.stderr:
            output += result.stderr
        return output.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return "(command timed out after 10s)"
    except Exception as e:
        return f"(error: {e})"


def build_headless_payload(fixture_path: str) -> str:
    """Build the ph_payload JSON for headless mode invocation."""
    payload = {
        "catalog_path": fixture_path,
        "agv_path": fixture_path,
        "validation_scope": "standard",
        "event_context": "none",
        "lab_id": "",
    }
    return json.dumps(payload, indent=2)


def extract_json_from_text(text: str) -> dict | None:
    """Extract the first complete JSON object from text using brace balancing."""
    start = text.find("{")
    if start == -1:
        return None

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
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    start = text.find("{", i + 1)
                    if start == -1:
                        return None
                    depth = 0

    return None


def run_skill_on_fixture(
    client: AnthropicVertex, system_prompt: str, fixture_path: str
) -> dict:
    """Run the full validator skill against a fixture using agentic tool-use loop."""
    payload = build_headless_payload(fixture_path)

    user_message = (
        f"ph_payload:\n```json\n{payload}\n```\n\n"
        f"Today's date is {date.today()}.\n\n"
        "You are running in HEADLESS MODE. You have the full SKILL.md and all "
        "sub-agent definitions in your system prompt. Act as both the orchestrator "
        "and all sub-agents. Run all validation checks for 'standard' scope.\n\n"
        "Use the bash tool to inspect files in the catalog directory. "
        "When done, return ONLY the final JSON output per the headless output contract. "
        "No prose, no explanation — just the JSON."
    )

    messages = [{"role": "user", "content": user_message}]

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=8192,
            system=system_prompt,
            messages=messages,
            tools=[BASH_TOOL],
        )

        if response.stop_reason == "end_of_turn":
            for block in response.content:
                if hasattr(block, "text") and block.text:
                    result = extract_json_from_text(block.text)
                    if result:
                        return result
            raise ValueError("No JSON in final response")

        tool_calls = [b for b in response.content if b.type == "tool_use"]
        if not tool_calls:
            for block in response.content:
                if hasattr(block, "text") and block.text:
                    result = extract_json_from_text(block.text)
                    if result:
                        return result
            raise ValueError("No tool calls and no JSON found")

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for tc in tool_calls:
            cmd = tc.input.get("command", "echo 'no command'")
            output = run_bash(cmd, fixture_path)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": output,
                }
            )

        messages.append({"role": "user", "content": tool_results})

    raise ValueError(f"Exceeded {MAX_TOOL_ROUNDS} tool rounds without final JSON")


def discover_fixtures(base_dir: str) -> list[str]:
    if not os.path.isdir(base_dir):
        return []
    return sorted(
        os.path.join(base_dir, d)
        for d in os.listdir(base_dir)
        if os.path.isdir(os.path.join(base_dir, d))
    )


def evaluate_clean(
    client: AnthropicVertex, system_prompt: str, fixture_path: str
) -> dict:
    name = os.path.basename(fixture_path)
    try:
        result = run_skill_on_fixture(client, system_prompt, fixture_path)
        error_count = len(result.get("errors", []))
        passed = error_count == 0
        return {
            "name": name,
            "type": "clean",
            "passed": passed,
            "detail": (
                "0 errors (expected: 0)"
                if passed
                else f"{error_count} errors (expected: 0): "
                + ", ".join(e.get("check", "?") for e in result["errors"])
            ),
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


def evaluate_broken(
    client: AnthropicVertex, system_prompt: str, fixture_path: str
) -> dict:
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
        result = run_skill_on_fixture(client, system_prompt, fixture_path)
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
        expected_name = exp_err["check"]
        acceptable = CHECK_ALIASES.get(expected_name, {expected_name})
        if not actual_error_checks & acceptable:
            issues.append(f"expected error '{expected_name}' not raised")

    actual_warning_checks = {w.get("check", "") for w in result.get("warnings", [])}
    for exp_warn in expected.get("expect_warnings", []):
        expected_name = exp_warn["check"]
        acceptable = CHECK_ALIASES.get(expected_name, {expected_name})
        if not actual_warning_checks & acceptable:
            issues.append(f"expected warning '{expected_name}' not raised")

    actual_suggestion_checks = {
        s.get("check", "") for s in result.get("suggestions", [])
    }
    for exp_sug in expected.get("expect_suggestions", []):
        expected_name = exp_sug["check"]
        acceptable = CHECK_ALIASES.get(expected_name, {expected_name})
        if not actual_suggestion_checks & acceptable:
            issues.append(f"expected suggestion '{expected_name}' not raised")

    passed = len(issues) == 0
    if passed:
        caught = []
        for e in expected.get("expect_errors", []):
            caught.append(f"{e['check']} error")
        for w in expected.get("expect_warnings", []):
            caught.append(f"{w['check']} warning")
        for s in expected.get("expect_suggestions", []):
            caught.append(f"{s['check']} suggestion")
        detail = "caught: " + ", ".join(caught) if caught else "all checks matched"
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

    system_prompt = load_skill_files()
    client = AnthropicVertex(region=REGION, project_id=PROJECT_ID)

    print(f"\nFull Skill Evaluation (SKILL.md + sub-agents with tool use)")
    print(f"Connecting to Vertex AI ({PROJECT_ID} / {REGION})")
    print(f"Model: {MODEL}")
    print(f"Skill prompt size: {len(system_prompt):,} characters\n")

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
        r = evaluate_clean(client, system_prompt, fp)
        results.append(r)
        if not output_json:
            print("PASS" if r["passed"] else f"FAIL — {r['detail']}")

    for i, fp in enumerate(broken_fixtures, len(clean_fixtures) + 1):
        name = os.path.basename(fp)
        if not output_json:
            print(f"  [{i}/{total}] Evaluating broken/{name}...", end=" ", flush=True)
        r = evaluate_broken(client, system_prompt, fp)
        results.append(r)
        if not output_json:
            print("PASS" if r["passed"] else f"FAIL — {r['detail']}")

    passed = sum(1 for r in results if r["passed"])

    if output_json:
        output = {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "model": MODEL,
            "mode": "full_skill",
            "results": [
                {k: v for k, v in r.items() if k != "llm_output"} for r in results
            ],
        }
        print(json.dumps(output, indent=2))
    else:
        print()
        print("AgnosticV Validator Eval Suite (Full Skill)")
        print("=" * 50)

        clean_results = [r for r in results if r["type"] == "clean"]
        broken_results = [r for r in results if r["type"] == "broken"]

        if clean_results:
            print("\nClean fixtures:")
            for r in clean_results:
                status = "PASS" if r["passed"] else "FAIL"
                print(f"  [{status}] {r['name']:40s} -- {r['detail']}")

        if broken_results:
            print("\nBroken fixtures:")
            for r in broken_results:
                status = "PASS" if r["passed"] else "FAIL"
                print(f"  [{status}] {r['name']:40s} -- {r['detail']}")

        print(f"\nResult: {passed}/{total} passed")
        print(f"Model:  {MODEL}")
        print(f"Mode:   Full skill (SKILL.md + sub-agents + tool use)")
        print()

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
