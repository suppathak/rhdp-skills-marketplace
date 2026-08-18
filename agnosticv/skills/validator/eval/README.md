# AgnosticV Validator — Eval Suite

A regression test suite for the `agnosticv:validator` skill. It provides two
evaluation approaches: a fast deterministic Python checker and an LLM-based
evaluator that invokes Claude via Vertex AI to run the actual validation rules.

## What this is

The `agnosticv:validator` skill is a 2700-line Markdown prompt that validates
RHDP catalog YAML configurations. It runs 27+ checks via 5 sub-agents. Every
check is fully deterministic (regex, string matching, YAML parsing — no LLM
reasoning), so we provide two ways to test for regressions:

1. **Python checker** (`agv_checker.py` + `score_eval.py`) — re-implements 10
   of the most impactful checks in plain Python. Fast, free, repeatable.
2. **LLM evaluator** (`score_eval_llm.py`) — sends each fixture's YAML to
   Claude via Vertex AI along with the full validation rules, then compares
   the LLM's structured JSON output against `expected.json`. Tests the actual
   skill behavior end-to-end.

## Checks implemented

10 checks from `schema-checker` and `metadata-checker`:

| #  | ID                  | What it validates                                          |
|----|---------------------|------------------------------------------------------------|
| 1  | file_structure      | `common.yaml` required; `dev.yaml`, `description.adoc` recommended |
| 2  | uuid_format         | `__meta__.asset_uuid` is a valid RFC 4122 UUID             |
| 3  | category_validation | `__meta__.catalog.category` is a valid enum value          |
| 4  | yaml_syntax         | YAML files parse without errors                            |
| 9  | best_practices      | Display name length, keyword count/quality, owner defined  |
| 10 | stage_files         | `dev.yaml` exists and parses cleanly                       |
| 19 | password_pattern    | No hardcoded or hash-based passwords                       |
| 21 | ee_image_date       | Execution environment image not stale (> 90 days)          |
| 23 | untagged_images     | No `:latest` or untagged images in prod/event catalogs     |
| 24 | catalog_name_length | Directory name is 50 characters or fewer                   |

## How to run

### Python checker (fast, no API calls)

```bash
pip install pyyaml

# Run the full eval suite
python3 agnosticv/skills/validator/eval/score_eval.py

# Run with JSON output
python3 agnosticv/skills/validator/eval/score_eval.py --json

# Run the checker on a single catalog directory
python3 agnosticv/skills/validator/eval/agv_checker.py <path-to-catalog-dir>
```

### LLM evaluator (requires Vertex AI access)

```bash
pip install 'anthropic[vertex]' pyyaml
gcloud auth application-default login

# Run the LLM eval suite (uses Claude Sonnet 4.6 by default)
python3 agnosticv/skills/validator/eval/score_eval_llm.py

# Run with JSON output
python3 agnosticv/skills/validator/eval/score_eval_llm.py --json
```

Environment variables:
- `ANTHROPIC_VERTEX_PROJECT_ID` — GCP project (default: `itpc-gcp-octo-eng-claude`)
- `CLOUD_ML_REGION` — GCP region (default: `global`)
- `EVAL_MODEL` — Claude model to use (default: `claude-sonnet-4-6`)

## Test fixtures (dataset)

All fixtures live under `eval/fixtures/`. Each fixture is a directory
containing catalog YAML files that simulate a real RHDP catalog item.

```
fixtures/
  clean/                  # Valid catalogs — must produce 0 errors
    ocp-demo/             #   Sourced from catalog-builder/examples/ocp-demo
    sandbox-tenant/       #   Sourced from catalog-builder/examples/sandbox-tenant

  broken/                 # Catalogs with one planted issue each
    missing-uuid/         #   asset_uuid removed → expects uuid_format error
    bad-uuid/             #   UUID set to "not-a-valid-uuid" → expects uuid_format error
    bad-category/         #   Category set to "InvalidCategory" → expects category_validation error
    hardcoded-password/   #   Password is literal string → expects password_pattern error
    no-display-name/      #   display_name removed → expects best_practices warning
    long-dirname-.../     #   Dir name is 61 chars (limit 50) → expects catalog_name_length error
    bad-yaml-syntax/      #   Malformed YAML → expects yaml_syntax error
    missing-common/       #   No common.yaml present → expects file_structure error
```

Each broken fixture includes an `expected.json` that declares what the checker
must find:

```json
{
  "expect_errors": [{"check": "uuid_format"}],
  "expect_warnings": [],
  "description": "asset_uuid removed; checker must flag missing UUID"
}
```

## How scoring works

- **Clean fixtures**: checker must produce 0 errors (warnings are OK).
- **Broken fixtures**: checker output is compared against `expected.json` —
  the declared error or warning must appear in the results.
- Exit code: `0` if all pass, `1` if any fail.

## Adding a new fixture

1. Create a directory under `fixtures/broken/<name>/`
2. Add `common.yaml` with exactly one planted issue
3. Add `expected.json` declaring the expected check name
4. Run `python3 score_eval.py` to verify

## File overview

| File                | Purpose                                                    |
|---------------------|------------------------------------------------------------|
| `agv_checker.py`    | Standalone Python checker — runs 10 checks on a catalog dir |
| `score_eval.py`     | Test harness — runs Python checker on all fixtures          |
| `score_eval_llm.py` | LLM evaluator — sends fixtures to Claude via Vertex AI     |
| `prompt_template.md`| Validation rules prompt sent to the LLM                    |
| `fixtures/`         | Golden test dataset (2 clean + 8 broken)                   |
