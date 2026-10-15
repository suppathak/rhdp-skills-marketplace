#!/usr/bin/env python3
"""
AgnosticV Validator Eval Suite — Full skill evaluation with tool use.

Runs the ACTUAL validator skill (SKILL.md + sub-agent definitions) against
each fixture using a judge model of your choice (any Anthropic-compatible
gateway, or Vertex AI) with an agentic tool-use loop. The judge can call bash
commands just like it would in Claude Code.

This is the real end-to-end test — not a condensed prompt, but the full
skill (~190K characters) with all sub-agent logic.

SECURITY: the judge's bash commands run on THIS machine with your user's
file permissions. The working directory is the fixture and the environment is
scrubbed of credentials, but this is not a filesystem sandbox — only run
judge models you trust.

Requires:
    pip install anthropic pyyaml       (pyyaml: the judge's bash commands
                                        run `python3 -c "import yaml"`)
    pip install 'anthropic[vertex]'    (Vertex AI mode only)
    gcloud auth application-default login   (Vertex AI mode only)

Exit codes: 0 = all passed, 1 = at least one fixture FAILED (regression),
2 = no failures but at least one fixture ERRORED (infrastructure problem —
re-run; not evidence of a regression).

Environment variables:
    EVAL_BASE_URL                — Anthropic-compatible gateway URL (gateway
                                    mode). Falls back to ANTHROPIC_BASE_URL.
    EVAL_API_KEY                 — API key for that gateway (any provider's key,
                                    e.g. an EnMaaS key). Falls back to
                                    ANTHROPIC_API_KEY.
    ANTHROPIC_VERTEX_PROJECT_ID  — GCP project ID (Vertex mode)
    CLOUD_ML_REGION              — GCP region (Vertex mode)
    EVAL_MODEL                   — judge model id (required to run; no default.
                                    Find ids with --list-models)

Usage:
    python score_eval_skill.py                          # run all fixtures
    python score_eval_skill.py --json                   # machine-readable JSON
    python score_eval_skill.py --fixture bad-uuid       # run one fixture
    python score_eval_skill.py --fixture bad-uuid --fixture hardcoded-password
    python score_eval_skill.py --check uuid             # run all fixtures testing a check
    python score_eval_skill.py --list                   # list available fixtures and checks
    python score_eval_skill.py --list-models            # list judge models on the backend
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime

try:
    from anthropic import Anthropic, AnthropicVertex
except ImportError:
    sys.exit(
        "ERROR: the anthropic package is required.\n"
        "Install with:  pip install anthropic   "
        "(Vertex AI mode: pip install 'anthropic[vertex]')"
    )

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)  # agnosticv/skills/validator/
AGENTS_DIR = os.path.join(os.path.dirname(os.path.dirname(SKILL_DIR)), "agents")
FIXTURES_DIR = os.path.join(SCRIPT_DIR, "fixtures")
CLEAN_DIR = os.path.join(FIXTURES_DIR, "clean")
BROKEN_DIR = os.path.join(FIXTURES_DIR, "broken")

# Provider-neutral names win; the Anthropic SDK's own names remain a fallback.
# A trailing "/v1" is dropped because the SDK appends /v1/messages itself.
BASE_URL = (
    os.environ.get("EVAL_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or ""
).rstrip("/")
if BASE_URL.endswith("/v1"):
    BASE_URL = BASE_URL[: -len("/v1")]
API_KEY = os.environ.get("EVAL_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or None
PROJECT_ID = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
REGION = os.environ.get("CLOUD_ML_REGION", "")
MODEL = os.environ.get("EVAL_MODEL", "")  # no default: the judge is always explicit

# Gateway mode: any Anthropic-compatible endpoint (set EVAL_BASE_URL).
# Vertex mode: Google Cloud Vertex AI (set project + region).
USE_GATEWAY = bool(BASE_URL)
if USE_GATEWAY and (PROJECT_ID or REGION):
    print(
        "NOTE: both gateway and Vertex settings are present — using the "
        "gateway (EVAL_BASE_URL / ANTHROPIC_BASE_URL takes precedence).",
        file=sys.stderr,
    )

if not USE_GATEWAY and (not PROJECT_ID or not REGION):
    sys.exit(
        "ERROR: Set EVAL_BASE_URL (gateway mode) or both\n"
        "ANTHROPIC_VERTEX_PROJECT_ID and CLOUD_ML_REGION (Vertex mode).\n"
        "Examples:\n"
        "  export EVAL_BASE_URL=https://your-gateway.example\n"
        "  export EVAL_API_KEY=<key>\n"
        "or:\n"
        "  export ANTHROPIC_VERTEX_PROJECT_ID=my-gcp-project\n"
        "  export CLOUD_ML_REGION=us-east5"
    )

MAX_TOOL_ROUNDS = 25
MAX_STALLS = 3  # nudges before giving up on a stalled (no-tool-call, no-JSON) turn
BASH_TIMEOUT = 10
# Per-turn output budget. Thinking models can spend the entire budget on
# reasoning before emitting tool calls or the verdict, so keep headroom.
MAX_TOKENS = 16384
# Environment passed to judge-issued bash commands (allowlist — no secrets).
SAFE_ENV_VARS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "VIRTUAL_ENV")

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


def make_client():
    """Build the API client — gateway (EVAL_BASE_URL) or Vertex AI."""
    if USE_GATEWAY:
        return Anthropic(base_url=BASE_URL, api_key=API_KEY)
    return AnthropicVertex(region=REGION, project_id=PROJECT_ID)


def list_models():
    """List judge models available on the configured backend."""
    try:
        models = make_client().models.list()
    except Exception as e:
        sys.exit(f"ERROR: Model listing not supported on this backend: {e}")
    print("Available judge models (select with EVAL_MODEL):")
    for m in models.data:
        print(f"  {m.id}")


def status_of(r: dict) -> str:
    """PASS / FAIL (regression) / ERROR (infrastructure — re-run)."""
    if r.get("errored"):
        return "ERROR"
    return "PASS" if r["passed"] else "FAIL"


def save_results(
    results: list[dict], total: int, output_path: str | None, filtered: bool
) -> str:
    """Write the full run record — including raw judge verdicts — as JSON.

    Defaults to outputs/eval-<timestamp>.json so every run leaves a trace;
    --output overrides the destination.
    """
    passed = sum(1 for r in results if r["passed"])
    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "total": total,
        "passed": passed,
        "failed": sum(1 for r in results if status_of(r) == "FAIL"),
        "errored": sum(1 for r in results if status_of(r) == "ERROR"),
        "model": MODEL,
        "mode": "full_skill",
        "filtered": filtered,
        "results": results,
    }

    if output_path:
        path = output_path
    else:
        out_dir = os.path.join(SCRIPT_DIR, "outputs")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(out_dir, f"eval-{stamp}.json")
        n = 1
        while os.path.exists(path):  # never overwrite a previous record
            path = os.path.join(out_dir, f"eval-{stamp}-{n}.json")
            n += 1

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(record, f, indent=2)
    return path


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
    """Execute a judge-issued bash command in the fixture directory.

    NOT a filesystem sandbox: the command runs with the caller's file
    permissions. What it does enforce: cwd is the fixture, a timeout, and a
    scrubbed environment so API keys and cloud credentials never reach the
    judge via `env`.
    """
    safe_env = {
        k: os.environ[k]
        for k in SAFE_ENV_VARS
        if k in os.environ
    }
    try:
        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=BASH_TIMEOUT,
            cwd=fixture_path,
            env=safe_env,
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
    client: Anthropic | AnthropicVertex, system_prompt: str, fixture_path: str
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
    stalls = 0

    for _ in range(MAX_TOOL_ROUNDS):
        # Streamed: non-streaming calls can exceed gateway connection limits
        # (~10 min) when a thinking model reasons long on the large skill
        # prompt. Streaming keeps the connection alive for the whole turn.
        with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=messages,
            tools=[BASH_TOOL],
        ) as stream:
            response = stream.get_final_message()

        tool_calls = [b for b in response.content if b.type == "tool_use"]

        # Final-answer turn (or a stall): no tool calls, so look for the JSON
        # verdict in any text blocks. "end_turn" is the standard Anthropic
        # stop reason; "end_of_turn" is kept for gateway compatibility.
        if response.stop_reason in ("end_turn", "end_of_turn") or not tool_calls:
            for block in response.content:
                if hasattr(block, "text") and block.text:
                    result = extract_json_from_text(block.text)
                    if result:
                        return result

            # Stalled: no tool calls and no JSON — e.g. a thinking model that
            # spent its whole output budget reasoning, or prose without the
            # verdict. Nudge it back on track instead of failing the fixture.
            stalls += 1
            if stalls > MAX_STALLS:
                raise ValueError(
                    "No tool calls and no JSON found "
                    f"(last stop_reason: {response.stop_reason})"
                )
            # Echo only text blocks back — thinking blocks may be truncated
            # mid-stream and rejected by the API on the next turn.
            text_blocks = [
                {"type": "text", "text": b.text}
                for b in response.content
                if b.type == "text" and b.text
            ]
            messages.append(
                {
                    "role": "assistant",
                    "content": text_blocks or [{"type": "text", "text": "(no output)"}],
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Continue the validation. Use the bash tool to inspect "
                        "the catalog files, or if all checks are done, return "
                        "ONLY the final JSON verdict — no prose, no explanation."
                    ),
                }
            )
            continue

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


def get_fixture_check_map(broken_dir: str) -> dict[str, list[str]]:
    """Build a mapping of check_name -> [fixture_names] from expected.json files."""
    check_to_fixtures: dict[str, list[str]] = {}
    if not os.path.isdir(broken_dir):
        return check_to_fixtures
    for fixture_name in sorted(os.listdir(broken_dir)):
        expected_path = os.path.join(broken_dir, fixture_name, "expected.json")
        if not os.path.isfile(expected_path):
            continue
        with open(expected_path) as f:
            expected = json.load(f)
        for entry in expected.get("expect_errors", []):
            check = entry.get("check", "")
            check_to_fixtures.setdefault(check, []).append(fixture_name)
        for entry in expected.get("expect_warnings", []):
            check = entry.get("check", "")
            check_to_fixtures.setdefault(check, []).append(fixture_name)
        for entry in expected.get("expect_suggestions", []):
            check = entry.get("check", "")
            check_to_fixtures.setdefault(check, []).append(fixture_name)
    return check_to_fixtures


def list_fixtures_and_checks():
    """Print available fixtures and checks, then exit."""
    clean = discover_fixtures(CLEAN_DIR)
    broken = discover_fixtures(BROKEN_DIR)
    check_map = get_fixture_check_map(BROKEN_DIR)

    print("Available fixtures:\n")
    print("  Clean:")
    for fp in clean:
        print(f"    {os.path.basename(fp)}")
    print("\n  Broken:")
    for fp in broken:
        name = os.path.basename(fp)
        expected_path = os.path.join(fp, "expected.json")
        checks = []
        if os.path.isfile(expected_path):
            with open(expected_path) as f:
                exp = json.load(f)
            for e in exp.get("expect_errors", []):
                checks.append(e.get("check", "?"))
            for w in exp.get("expect_warnings", []):
                checks.append(w.get("check", "?"))
            for s in exp.get("expect_suggestions", []):
                checks.append(s.get("check", "?"))
        check_str = f" ({', '.join(checks)})" if checks else ""
        print(f"    {name}{check_str}")

    print("\nAvailable checks (use with --check):\n")
    for check_name, fixtures in sorted(check_map.items()):
        print(f"  {check_name:30s} -> {', '.join(fixtures)}")

    print(f"\nTotal: {len(clean)} clean + {len(broken)} broken = {len(clean) + len(broken)} fixtures")
    print(f"Checks with fixtures: {len(check_map)}")


def filter_fixtures(
    clean: list[str],
    broken: list[str],
    fixture_names: list[str] | None,
    check_names: list[str] | None,
) -> tuple[list[str], list[str]]:
    """Filter fixtures based on --fixture and --check arguments."""
    if not fixture_names and not check_names:
        return clean, broken

    selected_names: set[str] = set()

    if fixture_names:
        selected_names.update(fixture_names)

    if check_names:
        check_map = get_fixture_check_map(BROKEN_DIR)
        for check in check_names:
            aliases = CHECK_ALIASES.get(check, {check})
            for alias in aliases:
                if alias in check_map:
                    selected_names.update(check_map[alias])

    if not selected_names:
        return clean, broken

    filtered_clean = [
        fp for fp in clean if os.path.basename(fp) in selected_names
    ]
    filtered_broken = [
        fp for fp in broken if os.path.basename(fp) in selected_names
    ]

    return filtered_clean, filtered_broken


def evaluate_clean(
    client: Anthropic | AnthropicVertex, system_prompt: str, fixture_path: str
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
            "errored": True,  # infrastructure problem, not a regression
            "detail": f"LLM call failed: {exc}",
            "llm_output": None,
        }


def evaluate_broken(
    client: Anthropic | AnthropicVertex, system_prompt: str, fixture_path: str
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
            "errored": True,  # infrastructure problem, not a regression
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


def parse_args():
    parser = argparse.ArgumentParser(
        description="AgnosticV Validator Eval Suite — Full skill evaluation with tool use."
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Machine-readable JSON output",
    )
    parser.add_argument(
        "--fixture", action="append", metavar="NAME",
        help="Run only the named fixture(s). Can be repeated.",
    )
    parser.add_argument(
        "--check", action="append", metavar="CHECK",
        help="Run only fixtures that test the named check(s). Can be repeated.",
    )
    parser.add_argument(
        "--list", action="store_true", dest="list_mode",
        help="List available fixtures and checks, then exit.",
    )
    parser.add_argument(
        "--list-models", action="store_true", dest="list_models_mode",
        help="List judge models available on the backend, then exit.",
    )
    parser.add_argument(
        "--output", metavar="FILE",
        help="Save the full run record (with raw verdicts) to FILE. "
             "Default: outputs/eval-<timestamp>.json.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.list_mode:
        list_fixtures_and_checks()
        sys.exit(0)

    if args.list_models_mode:
        list_models()
        sys.exit(0)

    if not MODEL:
        sys.exit(
            "ERROR: EVAL_MODEL is not set — choose a judge model explicitly.\n"
            "  python3 score_eval_skill.py --list-models   # see what the backend offers\n"
            "  export EVAL_MODEL=<model-id>"
        )

    system_prompt = load_skill_files()
    client = make_client()

    all_clean = discover_fixtures(CLEAN_DIR)
    all_broken = discover_fixtures(BROKEN_DIR)
    clean_fixtures, broken_fixtures = filter_fixtures(
        all_clean, all_broken, args.fixture, args.check
    )

    if not clean_fixtures and not broken_fixtures:
        if args.fixture or args.check:
            print("No fixtures matched the filter.", file=sys.stderr)
            print("Run with --list to see available fixtures and checks.", file=sys.stderr)
        else:
            print("No fixtures found.", file=sys.stderr)
        sys.exit(2)

    total = len(clean_fixtures) + len(broken_fixtures)
    is_filtered = args.fixture or args.check
    filter_label = ""
    if is_filtered:
        filter_label = f" (filtered: {total} of {len(all_clean) + len(all_broken)})"

    if not args.json:
        print(f"\nFull Skill Evaluation (SKILL.md + sub-agents with tool use)")
        if USE_GATEWAY:
            print("Backend: gateway")
        else:
            print("Backend: Vertex AI")
        print(f"Model: {MODEL}")
        print(f"Skill prompt size: {len(system_prompt):,} characters")
        print(f"Fixtures: {total}{filter_label}\n")

    results: list[dict] = []

    for i, fp in enumerate(clean_fixtures, 1):
        name = os.path.basename(fp)
        if not args.json:
            print(f"  [{i}/{total}] Evaluating clean/{name}...", end=" ", flush=True)
        r = evaluate_clean(client, system_prompt, fp)
        results.append(r)
        if not args.json:
            print("PASS" if r["passed"] else f"{status_of(r)} — {r['detail']}")

    for i, fp in enumerate(broken_fixtures, len(clean_fixtures) + 1):
        name = os.path.basename(fp)
        if not args.json:
            print(f"  [{i}/{total}] Evaluating broken/{name}...", end=" ", flush=True)
        r = evaluate_broken(client, system_prompt, fp)
        results.append(r)
        if not args.json:
            print("PASS" if r["passed"] else f"{status_of(r)} — {r['detail']}")

    passed = sum(1 for r in results if r["passed"])
    saved_path = save_results(results, total, args.output, is_filtered)

    if args.json:
        output = {
            "total": total,
            "passed": passed,
            "failed": sum(1 for r in results if status_of(r) == "FAIL"),
            "errored": sum(1 for r in results if status_of(r) == "ERROR"),
            "model": MODEL,
            "mode": "full_skill",
            "filtered": bool(is_filtered),
            "saved_to": saved_path,
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
                print(f"  [{status_of(r)}] {r['name']:40s} -- {r['detail']}")

        if broken_results:
            print("\nBroken fixtures:")
            for r in broken_results:
                print(f"  [{status_of(r)}] {r['name']:40s} -- {r['detail']}")

        print(f"\nResult: {passed}/{total} passed")
        n_err = sum(1 for r in results if status_of(r) == "ERROR")
        if n_err:
            print(f"Errors: {n_err} fixture(s) hit infrastructure errors "
                  f"(not regressions) — re-run those")
        print(f"Model:  {MODEL}")
        print(f"Mode:   Full skill (SKILL.md + sub-agents + tool use)")
        if is_filtered:
            print(f"Filter: {total} of {len(all_clean) + len(all_broken)} fixtures")
        print(f"Saved:  {saved_path}")
        print()

    failed = sum(1 for r in results if status_of(r) == "FAIL")
    errored = sum(1 for r in results if status_of(r) == "ERROR")
    sys.exit(1 if failed else 2 if errored else 0)


if __name__ == "__main__":
    main()
