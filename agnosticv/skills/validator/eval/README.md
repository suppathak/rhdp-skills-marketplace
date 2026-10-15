# AgnosticV Validator — Eval Suite

An LLM-based evaluation suite for the `agnosticv:validator` skill. It runs the
**full SKILL.md + all 5 sub-agent definitions** as the system prompt, gives
a judge model a bash tool, and runs an agentic tool-use loop against any
Anthropic-compatible gateway or Vertex AI. This tests the actual skill behavior end-to-end.

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
     │  Judge model (any backend)         │
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
the judge model in headless mode with a bash tool and compares the structured
JSON output against `expected.json`. The tool-use loop is bounded (25 rounds
per fixture) and resilient to thinking models: a stalled turn — no tool calls
and no JSON, e.g. an output budget spent entirely on reasoning — is nudged
back on track instead of failing the fixture.

## What the score means

The golden dataset (fixtures/) is the **ground truth**. Each fixture has a known
correct answer. The target is **22/22 (100%)** — every fixture must pass.

If any fixture fails:

1. **Check it is a FAIL, not an ERROR** — an `ERROR` (exit code 2) is an infrastructure problem; just re-run
2. **Re-run** — LLMs are non-deterministic; a single failure may pass on retry
3. **If it fails consistently** — the skill has a bug or the fixture needs updating
4. **If the score dropped after a skill edit** — you introduced a regression

Track the score over time. It should always be 22/22.

## Quick start

1. **Install** — `pip install anthropic pyyaml` (add `'anthropic[vertex]'` for
   Vertex mode). `pyyaml` is needed because the judge's own bash commands run
   `python3 -c "import yaml"`.
2. **Set up a judge model** — pick one setup below (A–D). There is **no
   default model**: you must choose one.
3. **Verify** — `--list-models`, then one fixture.
4. **Run** — one fixture, one check, or the full suite.

```bash
python3 agnosticv/skills/validator/eval/score_eval_skill.py --list-models        # 1. backend reachable?
python3 agnosticv/skills/validator/eval/score_eval_skill.py --fixture bad-uuid   # 2. one fixture end to end
python3 agnosticv/skills/validator/eval/score_eval_skill.py --check uuid         # run all fixtures for a check
python3 agnosticv/skills/validator/eval/score_eval_skill.py                      # full suite
```

Reasoning models are slow — budget roughly 5–10 minutes per fixture, so the
full 22 can take hours. Use `--fixture` / `--check` while iterating and run
the full suite once before merging.

## Setting up a judge model

The suite is model-agnostic: any model the backend serves can be the judge.
The fixtures are the ground truth; the model is only the instrument. Track the
score per judge model — an edit that passes with one judge but fails with
another is still a regression signal.

Pick **one** setup. Never commit URLs, keys, or project IDs — keep them in
your shell only.

| Variable | Used by | Purpose |
|---|---|---|
| `EVAL_MODEL` | all | Judge model id. **Required, no default.** Find ids with `--list-models`. |
| `EVAL_BASE_URL` | gateway | Anthropic-compatible endpoint URL (a trailing `/v1` is fine) |
| `EVAL_API_KEY` | gateway | API key for that endpoint — any provider's key, e.g. an EnMaaS key |
| `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION` | Vertex | GCP project and region |

If both gateway and Vertex settings are present, the gateway wins.

The `EVAL_*` names are provider-neutral. The Anthropic SDK's own `ANTHROPIC_BASE_URL`
and `ANTHROPIC_API_KEY` still work as a fallback, but `EVAL_*` wins when both are set.

### Setup A — Anthropic-compatible gateway (any hosted model)

```bash
export EVAL_BASE_URL=https://<your-gateway-host>
export EVAL_API_KEY=<your-key>
python3 agnosticv/skills/validator/eval/score_eval_skill.py --list-models
export EVAL_MODEL=<model-id>        # pick one from the list above
```

#### Using EnMaaS (Red Hat's internal AI gateway)

EnMaaS is an Anthropic-compatible gateway, so it works with Setup A as-is.
You need an **EnMaaS key** — not an Anthropic key.

1. **Get a key** from the EnMaaS onboarding page (corporate VPN required) and
   keep it in a secret manager. Don't request keys from individual operators.
2. **Get the gateway URL** from the same page. This repo is public, so never
   paste the URL or key into a committed file.
3. **Export both**, entering the key without echoing it or saving it to shell
   history (zsh):

   ```bash
   read -s "EVAL_API_KEY?EnMaaS key: " && export EVAL_API_KEY; echo
   export EVAL_BASE_URL="<gateway URL from the EnMaaS page>"   # a trailing /v1 is fine
   ```

   (bash: `read -s -p "EnMaaS key: " EVAL_API_KEY && export EVAL_API_KEY; echo`)

   If the key already lives in a variable such as `ENMAAS_KEY`, alias it:
   `export EVAL_API_KEY="$ENMAAS_KEY"`.
4. **Smoke-test the key** against the hosted GLM model:

   ```bash
   curl -s "$EVAL_BASE_URL/v1/messages" \
     -H "x-api-key: $EVAL_API_KEY" \
     -H "anthropic-version: 2023-06-01" \
     -H "content-type: application/json" \
     -d '{"model":"rits/zai-org/glm-5-3","max_tokens":128,"messages":[{"role":"user","content":"Reply with exactly OK."}]}'
   ```

   A JSON reply containing `"OK"` means the key works. A `thinking` block
   before the answer is normal.
5. **Pick the model and run one fixture:**

   ```bash
   export EVAL_MODEL=rits/zai-org/glm-5-3
   python3 agnosticv/skills/validator/eval/score_eval_skill.py --fixture bad-uuid
   ```

**`--list-models` may not show GLM.** The EnMaaS catalog depends on the
credential and can omit hosted models. That does not mean the model is
unavailable — set `EVAL_MODEL` to the exact id (as above) and it works. Hosted
GLM is text-only, which is fine for this suite. This script talks to the
Anthropic Messages endpoint (`/v1/messages`), which EnMaaS serves for GLM.

**Other gateways** work the same way: any internal or hosted model gateway that
speaks the Anthropic Messages protocol. Model ids usually look like
`provider/org/model`, so copy them from `--list-models` or your gateway docs
verbatim.

Reasoning models served this way are slow (several minutes per fixture) and
spend part of their output budget on thinking. The script streams responses
and nudges a stalled turn, so a slow fixture is normal — not a hang.

### Setup B — Vertex AI (Claude models)

```bash
gcloud auth application-default login
export ANTHROPIC_VERTEX_PROJECT_ID=<your-gcp-project>
export CLOUD_ML_REGION=<region>
export EVAL_MODEL=<claude-model-id-enabled-in-your-project>
```

If your org blocks end-user credentials for `aiplatform.endpoints.predict`,
use a service account or Setup A instead.

### Setup C — Local model with Ollama

Ollama serves an Anthropic-compatible endpoint, so it uses the gateway path.
The API key is required by the SDK but ignored by Ollama — any value works.

```bash
ollama pull <model>
export EVAL_BASE_URL=http://localhost:11434
export EVAL_API_KEY=ollama
export EVAL_MODEL=<model>           # the name shown by `ollama list`
```

**Caveat — context window.** The skill prompt is ~190K characters (~50K
tokens). Ollama's default context window is far smaller, so the prompt can be
silently truncated and the model never finishes. Use a model with a large
context and raise it when starting the server, e.g.
`OLLAMA_CONTEXT_LENGTH=65536 ollama serve`. A small local model may still
stall here; treat local models as experimental until a full run reaches 22/22.

### Setup D — OpenAI-style or other providers via LiteLLM

The script speaks the Anthropic protocol only. To use OpenAI-compatible
providers (or 100+ others), run a [LiteLLM](https://docs.litellm.ai) proxy,
which exposes an Anthropic-style endpoint, and use it as a gateway (Setup A).
Direct OpenAI support is not built in.

```bash
python3 -m venv .venv-litellm && .venv-litellm/bin/pip install 'litellm[proxy]'

cat > litellm-config.yaml <<'EOF'
model_list:
  - model_name: my-judge                  # the id you will use as EVAL_MODEL
    litellm_params:
      model: <provider>/<model>           # e.g. ollama_chat/<model>, openai/<model>
      api_base: <provider-url>            # omit for providers with a default
      api_key: os.environ/PROVIDER_API_KEY
EOF

export LITELLM_MASTER_KEY=sk-<any-strong-random-string>   # the proxy refuses to start without one
.venv-litellm/bin/litellm --config litellm-config.yaml --port 4000

# in another shell:
export EVAL_BASE_URL=http://localhost:4000
export EVAL_API_KEY=$LITELLM_MASTER_KEY
export EVAL_MODEL=my-judge
```

Keep `litellm-config.yaml` and the venv out of commits — they hold provider
settings and keys.

Verified against a local Ollama model: `--list-models` lists the proxy's
models, and streamed responses with tool calls come back in the Anthropic
shape the eval loop expects. A full fixture run through the proxy has not
been done; the judge still needs a large context window and reliable tool
calling.

## Running the suite

```bash
python3 agnosticv/skills/validator/eval/score_eval_skill.py [options]
```

- `--fixture NAME` — run only the named fixture(s). Can be repeated.
- `--check CHECK` — run only fixtures that test the named check. Can be repeated.
- `--list` — list available fixtures and which checks they cover.
- `--list-models` — list judge models available on the configured backend.
- `--json` — machine-readable JSON output.
- `--output FILE` — save the run record to FILE instead of `outputs/`.

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | Every selected fixture passed |
| `1` | At least one fixture **FAILED** — a regression, or a fixture that needs updating |
| `2` | No failures, but at least one fixture **ERRORED** (gateway timeout, connection error, stalled model). Infrastructure, not a regression — re-run it |

**Security.** The judge model runs bash commands on your machine with your
user's file permissions. Commands run inside the fixture directory with a
timeout and a scrubbed environment (API keys and cloud credentials are not
passed through), but this is not a filesystem sandbox. Only use judge models
you trust.

## Running from an AI coding agent (Claude Code / OpenCode)

The eval is a plain Python script, so any coding agent that can run shell
commands can drive it. Export the setup variables (above) in the shell that
launches the agent — it inherits them.

Short prompts to give the agent:

| Goal | Prompt |
|---|---|
| Quick check of what I touched | `I edited agnosticv/agents/schema-checker.md. Run the validator eval for the related checks (python3 agnosticv/skills/validator/eval/score_eval_skill.py --check uuid) and tell me if anything regressed.` |
| Full regression gate | `Run the full validator eval in the background (python3 agnosticv/skills/validator/eval/score_eval_skill.py), then report the Result line and any FAIL fixtures.` |
| Explain a failure | `The eval failed. Read the newest file in agnosticv/skills/validator/eval/outputs/ and explain why fixture <name> failed.` |
| Check setup | `Run score_eval_skill.py --list-models and tell me which judge models are available.` |

Tips for the agent: exit code `0` means all passed, `1` means a regression,
`2` means an infrastructure error (re-run); the full forensic record is in
`outputs/eval-<timestamp>.json`; and slow models should run in the background
rather than blocking the session.

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
Backend: gateway
Model: <your-model-id>
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
- Exit code: `0` all pass, `1` any fail, `2` infrastructure errors only (see Running the suite)

## Run artifacts

Every run saves a full JSON record — including the raw judge verdicts for each
fixture — to `outputs/eval-<timestamp>.json` (gitignored, never committed).
`--output FILE` redirects it. The record includes `passed`, `failed`, and
`errored` counts. The file is written when the run finishes, so a
run that is killed or times out leaves no record. Stdout stays a lean summary; the file is the
forensic record, which makes "why did this fixture fail?" answerable after the
fact and enables score-over-time tracking.

A committed example of what a record looks like (local paths redacted):
[`sample_output/eval-sample-missing-uuid.json`](sample_output/eval-sample-missing-uuid.json).

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

### Runner and credentials for the judge model

The eval needs credentials for whichever judge backend you use (gateway API
key, or GCP credentials for Vertex AI). Storing long-lived secrets in the repo
is not an option, so run it on a self-hosted GitHub Actions runner that already
has access, or pass the credentials as repository secrets.

- **Why self-hosted** — pre-authenticated environment, cost control, and it
  can sit next to the model endpoint (same network or VPC) for lower latency
  and so internal gateways need no public exposure
- **Requirements** — Linux VM (or container) with Python 3.10+, `anthropic`
  and `pyyaml`, the judge backend's credentials (gateway key, or `gcloud` with
  a service account for Vertex AI), and the GitHub Actions runner agent
  installed and registered
- **Runner labels** — `self-hosted`, `eval-runner`; workflows target with
  `runs-on: [self-hosted, eval-runner]`
- **Judge model** — set `EVAL_MODEL` explicitly in the workflow; there is no
  default
- **Exit codes in CI** — `1` fails the PR check (regression); `2` means an
  infrastructure error and should be retried, not treated as a regression
- **Alternative** — for initial setup, GitHub-hosted runners (`ubuntu-latest`)
  work with credentials stored as repository secrets
