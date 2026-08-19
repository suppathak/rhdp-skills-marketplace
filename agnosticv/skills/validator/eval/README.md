# AgnosticV Validator — Eval Suite

An LLM-based evaluation suite for the `agnosticv:validator` skill. It runs the
**full SKILL.md + all 5 sub-agent definitions** as the system prompt, gives
Claude a bash tool, and uses an agentic tool-use loop via Vertex AI. This tests
the actual skill behavior end-to-end.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    score_eval_skill.py                       │
│                      (test runner)                           │
└──────────────────────┬──────────────────────────────────────┘
                       │
          ┌────────────▼────────────┐
          │   For each fixture ...  │
          └────────────┬────────────┘
                       │
     ┌─────────────────▼──────────────────┐
     │         System Prompt              │
     │  ┌──────────────────────────────┐  │
     │  │  SKILL.md (orchestrator)     │  │
     │  │  + schema-checker.md         │  │
     │  │  + metadata-checker.md       │  │
     │  │  + workload-checker.md       │  │
     │  │  + sandbox-checker.md        │  │
     │  │  + ocp-infra-checker.md      │  │
     │  │         (~190K chars)        │  │
     │  └──────────────────────────────┘  │
     └─────────────────┬──────────────────┘
                       │
     ┌─────────────────▼──────────────────┐
     │     Claude (Vertex AI)             │
     │                                    │
     │  User msg: ph_payload (headless)   │
     │  Tool:     bash (ls, grep, cat...) │──── executes bash
     │                                    │     against fixture
     │  Returns:  structured JSON         │     directory
     └─────────────────┬──────────────────┘
                       │
     ┌─────────────────▼──────────────────┐
     │           Scoring                  │
     │                                    │
     │  Clean fixture:                    │
     │    errors == 0  →  PASS            │
     │    errors >  0  →  FAIL            │
     │                                    │
     │  Broken fixture:                   │
     │    expected check found →  PASS    │
     │    expected check missing → FAIL   │
     └─────────────────┬──────────────────┘
                       │
          ┌────────────▼────────────┐
          │   Result: 20/22 passed  │
          └─────────────────────────┘
```

## How it works

`score_eval_skill.py` loads the complete validator skill (SKILL.md,
schema-checker.md, metadata-checker.md, workload-checker.md, sandbox-checker.md,
ocp-infra-checker.md) into a single system prompt. For each fixture, it invokes
Claude in headless mode with a bash tool and compares the structured JSON output
against `expected.json`.

## What the score means

The golden dataset (fixtures/) is the **ground truth**. Each fixture has a known
correct answer. The score measures how well the validator skill follows its own
rules:

- **22/22 (100%)** — skill is working perfectly
- **20/22 (91%)** — skill has minor issues or LLM non-determinism
- **Score drops after a skill edit** — you introduced a regression
- **Score increases after a skill edit** — confirmed improvement

Run the eval multiple times to account for LLM non-determinism. The score is a
**benchmark** — track it over time to catch regressions and measure improvements.

## How to run

```bash
pip install 'anthropic[vertex]' pyyaml
gcloud auth application-default login

export ANTHROPIC_VERTEX_PROJECT_ID=<your-gcp-project>
export CLOUD_ML_REGION=<region>

python3 agnosticv/skills/validator/eval/score_eval_skill.py          # human-readable
python3 agnosticv/skills/validator/eval/score_eval_skill.py --json   # machine-readable
```

Environment variables:
- `ANTHROPIC_VERTEX_PROJECT_ID` — GCP project ID (required)
- `CLOUD_ML_REGION` — GCP region (required)
- `EVAL_MODEL` — Claude model (default: `claude-sonnet-4-6`)

## Checks with fixtures

| Check | Fixture(s) | Sub-agent | What it validates |
|---|---|---|---|
| file_structure | missing-common, missing-description | schema-checker | common.yaml required; description.adoc recommended |
| uuid | missing-uuid, bad-uuid, missing-meta | schema-checker | asset_uuid is valid RFC 4122 UUID |
| category | bad-category, demos-multiuser | schema-checker | category is valid enum; Demos+multiuser conflict |
| yaml_syntax | bad-yaml-syntax | schema-checker | YAML files parse without errors |
| deployer | missing-deployer | schema-checker | __meta__.deployer section present |
| reporting_labels | reporting-labels | schema-checker | reportingLabels.primaryBU present |
| anarchy_namespace | anarchy-namespace | schema-checker | anarchy.namespace not in common.yaml |
| catalog_name_length | long-dirname-... | schema-checker | directory name ≤ 50 characters |
| best_practices | no-display-name, generic-keywords | metadata-checker | display name, keyword quality |
| stage_files | bad-dev-yaml | metadata-checker | dev.yaml exists and parses cleanly |
| password_pattern | hardcoded-password | metadata-checker | no hardcoded passwords |
| ee_image_date | stale-ee-image | metadata-checker | EE image not stale (> 90 days) |
| untagged_images | latest-tag-image | metadata-checker | no :latest or untagged images |
| workloads | bad-workload-format | workload-checker | namespace.collection.role format |
| collections | missing-tag-variable | workload-checker | top-level tag: variable required |

Clean fixtures (must produce 0 errors):
- `clean/ocp-demo` — full real-world catalog item
- `clean/sandbox-tenant` — sandbox/tenant config

## Fixture format

```
fixtures/
  clean/              # Valid catalogs — 0 errors expected
    ocp-demo/
    sandbox-tenant/
  broken/             # Each has exactly one planted bug
    missing-uuid/
    ...
```

Each broken fixture contains:
- `common.yaml` — minimal valid YAML with one planted issue
- `expected.json` — declares which check must fire:

```json
{
  "expect_errors": [{"check": "reporting_labels"}],
  "expect_warnings": [],
  "description": "reportingLabels.primaryBU absent; must flag as error"
}
```

## Sample output

```
Full Skill Evaluation (SKILL.md + sub-agents with tool use)
Model: claude-sonnet-4-6
Skill prompt size: 190,230 characters

  [1/22] Evaluating clean/ocp-demo...          PASS
  [2/22] Evaluating clean/sandbox-tenant...    PASS
  [3/22] Evaluating broken/bad-category...     PASS
  ...
  [22/22] Evaluating broken/stale-ee-image...  PASS

AgnosticV Validator Eval Suite (Full Skill)
==================================================

Clean fixtures:
  [PASS] ocp-demo            -- 0 errors (expected: 0)
  [PASS] sandbox-tenant      -- 0 errors (expected: 0)

Broken fixtures:
  [PASS] bad-category        -- caught: category error
  [PASS] bad-uuid            -- caught: uuid error
  [PASS] hardcoded-password  -- caught: password_pattern error
  ...

Result: 22/22 passed
```

## Scoring rules

- **Clean fixtures**: must produce 0 errors (warnings OK)
- **Broken fixtures**: output compared against expected.json — declared error/warning must appear
- Exit code: `0` if all pass, `1` if any fail

## Adding a fixture

1. Create `fixtures/broken/<name>/`
2. Add `common.yaml` with one planted issue
3. Add `expected.json` declaring the expected check
4. Add the check name to `CHECK_ALIASES` in `score_eval_skill.py` if needed
5. Run the eval to verify
