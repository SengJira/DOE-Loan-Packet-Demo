# Loan AI Underwriting Copilot (`loan-ai-underwriting-copilot-v1`)

AI-assisted underwriting support for the `DDOE demo - Loan Packet Processing`
Dataloop project. It consumes the structured JSON produced by the existing
`loan-packet-processing` pipeline, checks the packet, reconciles it against
itself, quantifies risk, writes an evidence-grounded brief with an
NVIDIA-hosted LLM, and routes every packet to a human queue.

**Final approval remains with a human underwriter.** The copilot never approves
or declines a loan; both routes are recommendations, and the LLM has no control
over routing.

Nothing in this directory modifies the existing pipelines, datasets, tasks,
services or annotations. All new assets are additive and prefixed
`loan-ai-underwriting-copilot`.

---

## Architecture

```text
loan-structured-output  (existing dataset, read-only)
        |
        v
1. validate_packet_completeness    deterministic
        v
2. build_borrower_context          deterministic
        v
3. cross_document_reasoning        deterministic (normalised comparisons)
        v
4. assess_financial_risk           deterministic heuristics
        v
5. generate_underwriting_summary   NVIDIA LLM, schema-validated, fails safe
        v
6. route_by_confidence_and_risk    deterministic rules only
        |
        +--> READY_FOR_UNDERWRITER --> underwriter queue (human)
        |
        +--> HUMAN_REVIEW ---------> human review task
                                       |
                                       v
                             capture_reviewer_feedback
                                       |
                                       v
                loan-ground-truth/ai-underwriting-copilot-v1  (new items only)
```

Design rules:

- Deterministic first. Nodes 1–4 run before the model, and the model is given
  their output — not the raw documents.
- The LLM writes narrative only. Routing is computed from the deterministic
  artefacts plus the model's confidence.
- Fail safe. Missing key, timeout, 429, 5xx, non-JSON, or schema-invalid output
  all produce `INSUFFICIENT_DATA` / confidence `0.0`, which routes to a human.
- Evidence or silence. Every mismatch and risk flag carries the document id,
  document type and field it came from.

### Repository layout

```text
loan-ai-underwriting-copilot/
├── README.md, requirements.txt, .env.example
├── src/            config, schemas, the six nodes, NVIDIA client, logging, Dataloop wrapper
├── prompts/        system + user prompt templates
├── deployment/     package manifest, service deploy, pipeline creation, review configuration
├── tests/          54 offline unit tests (no network)
└── demo/           five synthetic packets, runner, presenter script
```

Supporting modules not in the original spec: `src/normalization.py` (shared
comparison helpers), `src/extraction.py` (tolerant accessors for the upstream
JSON), `src/pipeline_runner.py` (Dataloop-free orchestration used by tests and
the demo), `src/demo_cases.py` (loads the synthetic cases, resolving relative
date tokens at runtime).

---

## Model selection

`GET https://integrate.api.nvidia.com/v1/models` was called before choosing
(HTTP 200, 82 models listed), and every candidate was then verified with a
live `/chat/completions` probe — the catalog lists deprecated models that
still return 404/410 on inference.

| Preference | Model | Listed? | Serves inference? | Used |
|---|---|---|---|---|
| 1 | `meta/llama-3.3-70b-instruct` | **no** | – | – |
| 2 | `nvidia/llama-3.1-nemotron-70b-instruct` | yes | **no (404, deprecated)** | – |
| – | `nvidia/llama-3.1-nemotron-51b-instruct` | yes | **no (404, deprecated)** | – |
| – | `nvidia/nemotron-3-ultra-550b-a55b` | yes | yes, but exceeds the 120 s budget | fallback |
| 3 | `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` | yes | **yes, ~33 s/call, clean JSON** | **default** |
| – | `nvidia/nemotron-3.5-lightning-30b-a3b` | yes | yes | fallback |

The preferred Llama 3.3 model is not exposed by this endpoint and both ~49B–70B
Nemotron instruct models are deprecated, so the default is the strongest model
verified to return schema-valid JSON inside the request budget:
**`nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`**. Override with
`NVIDIA_MODEL`; `NvidiaClient.resolve_model()` re-checks the live list and
probes each candidate, so the default self-corrects if a preferred model comes
online.

Inference settings: temperature `0.1`, top-p `0.9`, max output `3000` tokens,
timeout `120 s`, up to `3` retries with exponential backoff and jitter on
429/5xx/timeouts. Responses are de-fenced, JSON-parsed and schema-validated.

A vision fallback for unreadable documents is supported but **disabled by
default** (`ENABLE_VISION_FALLBACK=false`); the main path is JSON-only.

---

## Environment variables

See `.env.example`. Summary:

| Variable | Default | Purpose |
|---|---|---|
| `NVIDIA_API_KEY` | – | required; read at runtime, never logged or persisted |
| `NVIDIA_BASE_URL` | `https://integrate.api.nvidia.com/v1` | OpenAI-compatible endpoint |
| `NVIDIA_MODEL` | `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` | verified model id |
| `NVIDIA_TEMPERATURE` / `NVIDIA_TOP_P` | `0.1` / `0.9` | sampling |
| `NVIDIA_MAX_OUTPUT_TOKENS` | `3000` | output budget |
| `NVIDIA_REQUEST_TIMEOUT` | `120` | seconds |
| `NVIDIA_MAX_RETRIES` / `NVIDIA_RETRY_BACKOFF_SECONDS` | `3` / `2` | backoff |
| `ENABLE_VISION_FALLBACK` | `false` | optional multimodal fallback |
| `AI_CONFIDENCE_THRESHOLD` | `0.85` | routing threshold |
| `PACKET_COMPLETENESS_THRESHOLD` | `0.90` | routing threshold |
| `DATALOOP_PROJECT_ID` | `37b46e9c-…e5eefd3` | target project |
| `SOURCE_DATASET` / `GROUND_TRUTH_DATASET` | `loan-structured-output` / `loan-ground-truth` | datasets |
| `GROUND_TRUTH_FOLDER` | `/ai-underwriting-copilot-v1` | separated ground-truth folder |
| `LOG_LEVEL` | `INFO` | structured JSON logs to stderr |

In Dataloop, store the NVIDIA key as an organization **secret** named
`NVIDIA_API_KEY` and bind it to the service — do not put it in
`environmentVariables`.

---

## Pipeline nodes

**1. `validate_packet_completeness`** — checks required document categories
(loan application, identification, payslip/proof of income, bank statement,
proof of address when required) and required fields (name, id number, date of
birth, address, employer, declared income, verified income, requested amount,
employment status). Unreadable documents are reported, never guessed. Emits
`present_documents`, `missing_documents`, `missing_fields`,
`unreadable_documents`, `completeness_score`.

**2. `build_borrower_context`** — normalises the extraction into one borrower
view plus a banking summary (average salary credit, credit variability, average
outflow, negative-balance events, returned payments), keeping a
`source_references` entry (document id, document type, field) for every
important value.

**3. `cross_document_reasoning`** — normalised deterministic comparisons of
name, id number, date of birth, address, declared vs payslip income, payslip vs
bank salary credits, employer, document dates, requested amount and purpose.
Capitalisation, whitespace, punctuation, common abbreviations and date formats
are normalised so cosmetic differences do not raise mismatches. Each check
reports `status` (`MATCH`/`MISMATCH`/`INSUFFICIENT_DATA`), `severity`,
`observed_values`, `evidence` and an explanation; `consistency_score` is the
share of conclusive checks that matched.

**4. `assess_financial_risk`** — see scoring below.

**5. `generate_underwriting_summary`** — builds a minimised, identifier-masked
payload and asks the model for strict JSON (recommendation, confidence,
executive summary, key findings, risk flags, missing information, required
actions, evidence references). `model`, `generated_at` and the disclaimer are
attached locally, not taken from the model.

**6. `route_by_confidence_and_risk`** — deterministic; see routing below.

**7. `capture_reviewer_feedback`** — writes the reviewer's corrections,
flag decisions, comments, result and reason as a **new** JSON item under
`loan-ground-truth/ai-underwriting-copilot-v1`.

---

## JSON schemas

Machine-readable definitions live in `src/schemas.py` and are enforced on every
node output (and on every model response) via `validate()`; `jsonschema` is used
when installed, with an equivalent built-in validator as fallback.

```jsonc
// node 1
{"packet_id": "", "complete": true, "present_documents": [], "missing_documents": [],
 "missing_fields": [], "unreadable_documents": [], "completeness_score": 0.0}

// node 2
{"packet_id": "", "borrower": {"full_name": null, "date_of_birth": null,
 "identification_number": null, "declared_address": null, "document_address": null,
 "employer": null, "employment_status": null, "declared_monthly_income": 0.0,
 "verified_monthly_income": 0.0, "requested_loan_amount": 0.0},
 "banking_summary": {"average_monthly_salary_credit": 0.0, "salary_credit_variability": 0.0,
 "average_monthly_outflow": 0.0, "negative_balance_events": 0, "returned_payment_events": 0},
 "source_references": []}

// node 3
{"packet_id": "", "consistency_score": 0.0, "checks": [{"check_id": "", "field": "",
 "status": "MATCH|MISMATCH|INSUFFICIENT_DATA", "severity": "LOW|MEDIUM|HIGH",
 "observed_values": [], "evidence": [], "explanation": ""}]}

// node 4
{"packet_id": "", "risk_level": "LOW|MEDIUM|HIGH|UNKNOWN", "risk_score": 0,
 "risk_flags": [{"code": "", "severity": "", "description": "", "evidence": []}],
 "positive_indicators": [], "insufficient_data": [], "calculation_details": {}}

// node 5 (model must return the first nine keys; the rest are added locally)
{"packet_id": "", "recommendation": "READY_FOR_UNDERWRITER|MANUAL_REVIEW|INSUFFICIENT_DATA",
 "confidence": 0.0, "executive_summary": "", "key_findings": [], "risk_flags": [],
 "missing_information": [], "required_actions": [], "evidence_references": [],
 "model": "", "generated_at": "ISO-8601", "disclaimer": "AI-generated analysis for human review; not a final lending decision.",
 "model_output_valid": true}

// node 6
{"packet_id": "", "route": "READY_FOR_UNDERWRITER|HUMAN_REVIEW",
 "reasons": [{"code": "", "detail": ""}], "thresholds": {}, "final_decision_owner": "human_underwriter"}
```

---

## Risk-scoring rules (transparent demo heuristics)

Not a credit score, not a lending decision — a ranking of how much underwriter
attention a packet needs. Points are summed and clamped to 0–100
(`src/financial_risk.py`, `RISK_POINTS`):

| Code | Points | Raised when |
|---|---|---|
| `INCOME_VARIANCE_HIGH` | 25 | declared vs verified income differs > 20 % |
| `INCOME_VARIANCE_MEDIUM` | 12 | differs > 10 % |
| `SALARY_DEPOSIT_INCONSISTENT` | 18 | payslip income vs bank salary credits differ > 15 % (HIGH severity above 20 %) |
| `UNSTABLE_INCOME_HIGH` | 25 | salary-credit coefficient of variation > 30 % |
| `UNSTABLE_INCOME_MEDIUM` | 15 | coefficient of variation > 15 % |
| `LOAN_TO_INCOME_HIGH` | 20 | requested amount > 5× annual income |
| `LOAN_TO_INCOME_MEDIUM` | 10 | > 3× annual income |
| `HIGH_RECURRING_OBLIGATIONS` | 10 | average outflow > 80 % of income |
| `NEGATIVE_BALANCE_EVENTS_HIGH` | 18 | ≥ 3 negative-balance days |
| `NEGATIVE_BALANCE_EVENTS` | 10 | 1–2 negative-balance days |
| `RETURNED_PAYMENTS` | 20 | returned/NSF/bounced payment observed |
| `UNUSUAL_TRANSACTION` | 8 | credit > 3× the average salary credit |
| `DUPLICATE_DOCUMENT_SUSPECTED` | 10 | two documents share type, checksum, date and amount |
| `IDENTITY_INCONSISTENCY` | 25 | name / id / date-of-birth mismatch from node 3 |
| `MISSING_REQUIRED_DOCUMENT` | 12 | node 1 reported a missing document |

Levels: `HIGH` at score ≥ 50 **or** any HIGH-severity flag; `MEDIUM` at ≥ 25 or
any flag; `UNKNOWN` when three or more inputs are missing and nothing could be
computed; otherwise `LOW`. Thresholds are configurable in `src/config.py`.

---

## Routing rules

`HUMAN_REVIEW` when **any** of these holds (reason codes in brackets):

- AI confidence < `AI_CONFIDENCE_THRESHOLD` (`LOW_AI_CONFIDENCE`)
- completeness < `PACKET_COMPLETENESS_THRESHOLD` (`LOW_COMPLETENESS`)
- risk level `HIGH` (`RISK_LEVEL`)
- a HIGH-severity mismatch (`HIGH_SEVERITY_MISMATCH`)
- a required document missing (`MISSING_DOCUMENT`) or required field missing
  (`MISSING_FIELD`) or an unreadable document (`UNREADABLE_DOCUMENT`)
- identity not confirmable across documents (`IDENTITY_UNCERTAIN`)
- invalid model output or API failure (`MODEL_OUTPUT_INVALID`)
- the model reported insufficient data (`AI_INSUFFICIENT_DATA`) or key evidence
  is missing (`INSUFFICIENT_EVIDENCE`)

Otherwise `READY_FOR_UNDERWRITER` — still a recommendation for a human.

---

## Human-review process

`deployment/configure_human_review.py` provisions the reviewer form on the
copilot's own review recipe (existing recipes are not edited).

The reviewer sees the original documents, the structured extraction, the AI
executive summary, confidence and model id, the risk flags, cross-document
mismatches, missing information, the evidence behind each issue and the
required actions (all stored on the item under
`metadata.user.aiUnderwriting`). The reviewer can correct extracted values
(`corrected_fields`), confirm or dismiss each risk flag
(`risk_flag_decision`), add comments (`reviewer_comments`), set
`final_review_result` and record `decision_reason`.

On approval, `capture_reviewer_feedback` writes
`loan-ground-truth/ai-underwriting-copilot-v1/<packet_id>__<version>.json`.
Existing ground-truth items are never modified; reruns reuse the deterministic
filename rather than accumulating duplicates.

---

## Deployment

Prerequisites: Python 3.10+, `pip install -r requirements.txt`, Dataloop
credentials (`DATALOOP_API_KEY` or M2M), and an organization secret
`NVIDIA_API_KEY`.

```bash
cd loan-ai-underwriting-copilot
cp .env.example .env && $EDITOR .env      # never commit .env

python deployment/deploy_service.py --dry-run     # inspect the plan
python deployment/deploy_service.py               # push package + deploy service
python deployment/create_pipeline.py --dry-run
python deployment/create_pipeline.py              # create the pipeline (NOT installed)
python deployment/configure_human_review.py       # reviewer form + instructions
```

`create_pipeline.py` refuses to run if `loan-ai-underwriting-copilot-v1`
already exists, and creates the source dataset node with
`load_existing_data=False`, so installing the pipeline does **not** back-fill
existing items.

### Controlled test run

1. Install the pipeline: `python deployment/create_pipeline.py --install`, or
   from the console.
2. Feed 1–5 items deliberately (console: select items → *Run on pipeline*; SDK:
   `pipeline.execute(execution_input=...)` per item).
3. Inspect `metadata.user.aiUnderwriting` on each item and the resulting review
   task entries.
4. Only after the outputs look right, discuss widening the input filter. Bulk
   processing of the existing dataset requires explicit approval and is not
   enabled by any script here.

### Rollback

1. Pause the pipeline (console → pipeline → Pause), or `pipeline.pause()`.
2. Pause/delete the service `loan-ai-underwriting-copilot-v1`:
   `project.services.get(service_name=...).pause()`.
3. Delete the pipeline if required: `pipeline.delete()`.
4. Delete the copilot's items under `loan-ground-truth/ai-underwriting-copilot-v1`
   (that folder contains only copilot output).
5. Optional: remove `metadata.user.aiUnderwriting` from touched items in
   `loan-structured-output` — no other field is written.

No rollback step touches `loan-packet-processing`,
`loan-nemotron-classifier-v7`, or pre-existing ground truth.

---

## Tests

```bash
cd loan-ai-underwriting-copilot
python -m pytest -q          # 54 tests, offline, no network and no key needed
```

Coverage: completeness (missing documents/fields, unreadable, waivers),
cross-document (cosmetic-difference tolerance, each mismatch type, evidence
present on every mismatch), financial risk (each flag family, bounds,
insufficient data), LLM handling (fence stripping, prose-wrapped JSON,
truncated JSON, schema violations, 429/5xx retry and give-up, fail-safe
fallbacks, payload minimisation and identifier masking, log redaction), and
routing (each rule, configurable thresholds, the five demo cases, idempotency).

## Demo

```bash
python demo/run_demo.py --stub-llm     # deterministic, no network
python demo/run_demo.py                # real NVIDIA call when NVIDIA_API_KEY is set
python demo/run_demo.py --case case-2-income-mismatch
python demo/run_demo.py --json /tmp/demo_artifacts.json
```

| Case | Scenario | Expected | Actual |
|---|---|---|---|
| 1 | clean packet | `READY_FOR_UNDERWRITER` | matches |
| 2 | THB 80,000 declared vs THB 65,000 payslip | `HUMAN_REVIEW` | matches (`HIGH_SEVERITY_MISMATCH`) |
| 3 | application vs ID address mismatch | `HUMAN_REVIEW` | matches (`IDENTITY_UNCERTAIN`) |
| 4 | bank statement absent | `HUMAN_REVIEW` | matches (`MISSING_DOCUMENT`) |
| 5 | salary deposits vary across three months | `HUMAN_REVIEW` | matches (`RISK_LEVEL` HIGH) |

All data is synthetic; dates are relative tokens resolved at load time so the
packets never go stale. `demo/demo_script.md` is the presenter walkthrough.

---

## Logging and security

Structured JSON logs (stderr) carry packet id, pipeline cycle id, node name,
model, duration, status and error type. Redaction is applied before emission:
API keys, tokens, prompts, payloads, raw transactions and bank statements are
dropped, and identification numbers are masked to the last four characters
anywhere they appear.

The model payload is minimised: nodes' structured findings only — no raw
documents, no transaction lines — with identification numbers masked (node 3
does its comparisons locally, before masking).

Handled failure modes: missing metadata, malformed JSON, model timeout, HTTP
429, HTTP 5xx, invalid model response, missing documents, duplicate pipeline
events. Idempotency comes from `analysis_version` plus a SHA-256 fingerprint of
the input: a rerun with unchanged input reuses the cached node results and
rewrites the same deterministic ground-truth filename.

---

## Known limitations

- Risk scoring is a transparent demo heuristic, not a validated credit model;
  weights are illustrative and unvalidated against outcomes.
- Address and employer matching is string-normalisation based; it has no
  address-registry or company-registry lookup, so unusual formats can produce
  `INSUFFICIENT_DATA` rather than a confident match.
- The copilot trusts the upstream extraction; it detects disagreement between
  documents, not OCR errors that are consistent across them.
- Confidence is self-reported by the model and is only used as one of several
  routing gates; it is not calibrated.
- The vision fallback is stubbed behind a flag and has not been exercised.
- Duplicate-document detection is fingerprint-based (type, checksum, date,
  amount), not perceptual.
- The demo packets are synthetic and Thailand-shaped (THB, Thai id format).

## Production hardening

- Replace the heuristic weights with a validated, monitored model and add
  fairness/adverse-action review before any production lending use.
- Add golden-set regression tests and drift monitoring on model output quality,
  plus alerting on `FAILED_SAFE` rates.
- Pin the model version, and record model id + prompt hash on every item for
  auditability (model id is already recorded).
- Move prompts to a versioned, reviewed artefact store; add prompt-injection
  defences on any free-text field that reaches the model.
- Add per-item access control and retention rules on the copilot's ground-truth
  folder; tokenise identifiers at extraction time rather than at prompt time.
- Add a dead-letter queue and replay tooling for service-level failures, and
  rate-limit the NVIDIA client at the service level.
- Human factors: measure reviewer override rates to detect automation bias.

**Final approval remains with a human underwriter.**
