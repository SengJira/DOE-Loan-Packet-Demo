# Loan AI Underwriting Copilot — demo script (~10 minutes)

Audience: lending operations / underwriting leadership.
Message: the copilot removes the manual reconciliation work, shows its evidence,
and hands every packet to a human underwriter.

## 0. Before the room joins (2 min)

```bash
cd loan-ai-underwriting-copilot
export NVIDIA_API_KEY=...            # never shown on screen
python -m pytest -q                  # 54 tests, all green
python demo/run_demo.py --json /tmp/demo_artifacts.json
```

Open the Dataloop console on the project and have these tabs ready:

1. `loan-structured-output` dataset (input)
2. `loan-ai-underwriting-copilot-v1` pipeline canvas
3. the human-review task
4. `loan-ground-truth` → folder `ai-underwriting-copilot-v1`

## 1. Framing (1 min)

"The existing pipeline already turns a loan packet into structured JSON. The
question underwriters actually ask next is: *is this packet complete, does it
agree with itself, and where is the risk?* That is what this copilot answers —
it never approves or declines a loan."

## 2. The pipeline canvas (2 min)

Walk the six nodes left to right: completeness → borrower context →
cross-document reasoning → financial risk → LLM summary → routing. Point out:

- the first four nodes are deterministic; the LLM only writes the narrative
- routing is deterministic too — the model cannot promote a packet to "ready"
- both branches end in a human queue

## 3. Case 1 — the clean packet (1 min)

```bash
python demo/run_demo.py --case case-1-clean
```

Completeness 1.00, consistency 1.00, risk LOW, confidence ≥ 0.85 →
`READY_FOR_UNDERWRITER`. Say plainly: this is still a recommendation; the
underwriter sees a pre-read, not a decision.

## 4. Case 2 — income mismatch (2 min)

```bash
python demo/run_demo.py --case case-2-income-mismatch
```

Application declares THB 80,000; payslip shows THB 65,000. Show the check
object: `declared_vs_payslip_income`, status `MISMATCH`, severity `HIGH`, plus
the exact document and field it came from. Route: `HUMAN_REVIEW`.

"Every finding carries its evidence. If the AI cannot point at a document and a
field, it does not get to make the claim."

## 5. Cases 3–5 — address mismatch, missing document, unstable income (2 min)

```bash
python demo/run_demo.py
```

One table shows all five: four exceptions to human review for four different
reasons (identity uncertain, missing bank statement, HIGH risk from salary
volatility, plus the income mismatch).

## 6. Fail-safe (1 min)

Unset the key and run again:

```bash
env -u NVIDIA_API_KEY python demo/run_demo.py --case case-1-clean
```

The summary node returns `INSUFFICIENT_DATA` / confidence 0.0 and the packet
routes to a human. Model outage, timeout, 429, or malformed JSON all behave the
same way. There is no silent failure mode that can push a packet through.

## 7. Human review and ground truth (2 min)

In the console, open the review task: documents, extraction, AI summary,
confidence, risk flags with evidence, mismatches, missing information, required
actions. The reviewer corrects values, confirms or dismisses each flag, writes
the reason and sets the final result. On approval the copilot writes a **new**
item under `loan-ground-truth/ai-underwriting-copilot-v1` — existing ground
truth is never overwritten, and re-running the same packet reuses the same
deterministic filename instead of piling up duplicates.

## 8. Close (30 s)

- Verified model: see README ("Model selection").
- The copilot compresses the reconciliation work, not the decision.
- Final approval remains with a human underwriter.
