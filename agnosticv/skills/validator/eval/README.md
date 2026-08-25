# AgnosticV Validator — Eval Suite

An LLM-based evaluation suite for the `agnosticv:validator` skill. It runs the
**full SKILL.md + all 5 sub-agent definitions** as the system prompt, gives
Claude a bash tool, and uses an agentic tool-use loop via Vertex AI. This tests
the actual skill behavior end-to-end.

## Skill structure

The validator skill is not a single file — it is an orchestrator that
dispatches 5 specialist sub-agents at runtime:

| File | Role | Checks |
|---|---|---|
| `skills/validator/SKILL.md` | Orchestrator — parses YAML, classifies CI type, dispatches sub-agents | Pre-flight (yaml_syntax) |
| `agents/schema-checker.md` | File structure, UUID, category, deployer, labels, anarchy namespace, dir length | 9 |
| `agents/metadata-checker.md` | Best practices, passwords, EE image age, untagged images, AsciiDoc, events | 9 |
| `agents/workload-checker.md` | Workload format, collections, LiteMaaS, duplicate includes, placement | 8 |
| `agents/sandbox-checker.md` | Tenant/shared-pool specific checks (sandboxes block, catch_all, deployer actions) | 8 |
| `agents/ocp-infra-checker.md` | Dedicated-cluster checks (OCP version, auth, bastion, showroom, multi-user) | 8 |

The eval reads all 6 files and concatenates them into a single ~190K character
system prompt. Any edit to the skill is automatically tested on the next eval run.

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
          │   Result: 22/22 passed  │
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
correct answer. The target is **22/22 (100%)** — every fixture must pass.

If any fixture fails:

1. **Re-run** — LLMs are non-deterministic; a single failure may pass on retry
2. **If it fails consistently** — the skill has a bug or the fixture needs updating
3. **If the score dropped after a skill edit** — you introduced a regression

Track the score over time. It should always be 22/22.

## How to run

```bash
pip install 'anthropic[vertex]' pyyaml
gcloud auth application-default login

export ANTHROPIC_VERTEX_PROJECT_ID=<your-gcp-project>
export CLOUD_ML_REGION=<region>

python3 agnosticv/skills/validator/eval/score_eval_skill.py                        # run all fixtures
python3 agnosticv/skills/validator/eval/score_eval_skill.py --json                 # machine-readable
python3 agnosticv/skills/validator/eval/score_eval_skill.py --fixture bad-uuid     # run one fixture
python3 agnosticv/skills/validator/eval/score_eval_skill.py --check uuid           # run all uuid fixtures
python3 agnosticv/skills/validator/eval/score_eval_skill.py --list                 # show available fixtures/checks
```

Filtering options:
- `--fixture NAME` — run only the named fixture(s). Can be repeated.
- `--check CHECK` — run only fixtures that test the named check. Can be repeated.
- `--list` — list all available fixtures and which checks they cover.
- `--json` — machine-readable JSON output.

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

## Coverage summary

The validator skill runs ~42 checks across 5 sub-agents. This eval suite
currently covers **15 unique checks** with **22 fixtures** (2 clean + 20 broken).

| Sub-agent | Total checks | Covered | Not yet covered |
|---|---|---|---|
| schema-checker | 9 | 8 | 1 |
| metadata-checker | 9 | 5 | 4 |
| workload-checker | 8 | 2 | 6 |
| sandbox-checker | 8 | 0 | 8 |
| ocp-infra-checker | 8 | 0 | 8 |
| **Total** | **42** | **15** | **27** |

### Checks not yet covered

These checks require specialized fixtures (specific CI types, event contexts,
or infrastructure configurations):

**schema-checker:**
- `workshop_user_mode` — workshop_user_mode field validation

**metadata-checker:**
- `asciidoc` — description.adoc and info-message-template.adoc checks
- `event_catalog` — Brand_Event label, event keywords, directory naming (requires `event_context != none`)
- `event_restriction` — event access restriction includes (requires `event_context != none`)
- `showroom_namespace` — showroom namespace override check (requires `ci_type == tenant_namespace`)

**workload-checker:**
- `litemaas` — LiteMaaS include validation
- `duplicate_includes` — cross-file `#include` deduplication
- `requirements_content_position` — collections block within first 200 lines
- `runtime_automation` — runtime automation image and workload consistency
- `litellm_placement` — LiteLLM workload must not be in shared-pool cluster CI
- `showroom_placement` — Showroom workload must not be in shared-pool cluster CI

**sandbox-checker (all):**
- Checks 6C–6G (tenant): sandboxes block, catch_all, namespaced_workloads, remove_workloads, deployer actions
- Checks 6H–6J (cluster): required includes, propagate_provision_data, deployer actions
- Requires fixtures with `config: namespace` or `config: openshift-cluster`

**ocp-infra-checker (all):**
- Check 6A: cloud-vms-base instances and bastion image
- Check 6B: OCP version, SNO limits, GPU
- Check 7: authentication workload validation
- Check 8: showroom workloads co-presence
- Check 11: multi-user configuration
- Check 12: bastion configuration for OCP
- Check 15: component propagation
- Check 17-OCP: LiteMaaS workload, model, and duration
- Requires fixtures with `config: openshift-workloads` and specific cloud_provider/workload combinations

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

## Next steps

### Expand check coverage (15/42 to 42/42)

Current coverage is strongest on schema-checker (8/9) and metadata-checker
(5/9). Priority order for expansion:

- **workload-checker** (6 uncovered) — litemaas, duplicate includes,
  requirements_content position, runtime automation, litellm/showroom placement
- **metadata-checker** (4 uncovered) — asciidoc, event catalog, event
  restriction, showroom namespace
- **sandbox-checker** (8 uncovered) — needs fixtures with `config: namespace`
  and `config: openshift-cluster` to test tenant/shared-pool checks
- **ocp-infra-checker** (8 uncovered) — needs fixtures with
  `config: openshift-workloads` and specific cloud_provider/workload combos

See the "Coverage summary" section above for the full breakdown.

### CI integration with GitHub Actions slash commands

Goal: run the eval automatically on PRs that touch the validator skill.

- **Slash command** — a `/eval` comment on a PR triggers the workflow via
  `issue_comment` event. Supports the same filtering flags as the CLI:
  - `/eval` — run all 22+ fixtures
  - `/eval --check uuid` — run only uuid-related fixtures
  - `/eval --fixture bad-uuid` — run a single fixture
- **Auto-trigger** — optionally run a fast smoke subset on PR open/push when
  files under `agnosticv/skills/validator/` or `agnosticv/agents/` change
- **Results posted as PR comment** — pass/fail scorecard with the check-level
  breakdown so reviewers can see the impact of skill edits at a glance

### Self-hosted runner for Vertex AI access

The eval calls Claude via Vertex AI, which requires GCP credentials. A
self-hosted GitHub Actions runner avoids storing long-lived secrets in the repo.

- **Why self-hosted** — pre-authenticated GCP environment, cost control,
  can be on the same VPC as Vertex AI for lower latency
- **Requirements** — Linux VM (or container) with Python 3.10+, `gcloud` CLI
  with a service account authenticated for Vertex AI, GitHub Actions runner
  agent installed and registered
- **Runner labels** — `self-hosted`, `eval-runner`; workflows target with
  `runs-on: [self-hosted, eval-runner]`
- **Alternative** — for initial setup, GitHub-hosted runners (`ubuntu-latest`)
  work with GCP credentials stored as repository secrets
