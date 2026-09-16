"""Run the five synthetic packets through nodes 1-6 and print the outcome.

With NVIDIA_API_KEY set, the real hosted model produces the executive summary.
Without it the LLM node fails safe (INSUFFICIENT_DATA, confidence 0.0) and the
deterministic rules still decide the route - which is itself part of the demo.
Use --stub-llm to exercise the full ready path with a deterministic stub
instead of calling NVIDIA.

Usage:
    python demo/run_demo.py                     # all cases, table output
    python demo/run_demo.py --stub-llm          # no network, deterministic
    python demo/run_demo.py --case case-1-clean # one case
    python demo/run_demo.py --json out.json     # full artefacts to a file
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from demo_cases import load_cases  # noqa: E402
from pipeline_runner import analyze_packet  # noqa: E402
from schemas import LLM_SUMMARY_SCHEMA, validate  # noqa: E402

COLUMNS = "{:<26} {:<22} {:<22} {:<7} {:<12} {:<6}"


class StubLlmClient:
    """Deterministic stand-in for NvidiaClient; mirrors a well-formed response."""

    model = "stub/deterministic-underwriting-summariser"

    def chat_json(self, system_prompt, user_prompt, schema=None, model=None, **_):
        payload = {
            "recommendation": "READY_FOR_UNDERWRITER",
            "confidence": 0.92,
            "executive_summary": (
                "Deterministic stub summary: the packet was analysed offline; "
                "see the completeness, cross-document and risk artefacts for the findings."
            ),
            "key_findings": [],
            "risk_flags": [],
            "missing_information": [],
            "required_actions": ["Underwriter to confirm the final decision."],
            "evidence_references": [],
        }
        if schema is not None:
            validate(payload, schema or LLM_SUMMARY_SCHEMA, name="stub_llm_summary")
        return payload

    def resolve_model(self, preferred=None):
        return self.model


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", default=None, help="run a single case id")
    parser.add_argument("--json", dest="json_out", default=None, help="write full artefacts to this path")
    parser.add_argument("--stub-llm", action="store_true", help="use a deterministic stub instead of NVIDIA")
    args = parser.parse_args()

    client = StubLlmClient() if args.stub_llm else None
    if not args.stub_llm and not os.environ.get("NVIDIA_API_KEY"):
        print("NVIDIA_API_KEY not set - the summary node will fail safe to HUMAN_REVIEW inputs.\n")

    cases = [c for c in load_cases() if args.case in (None, c["case_id"])]
    if not cases:
        print(f"no such case: {args.case}")
        return 2

    artefacts = []
    failures = 0
    print(COLUMNS.format("case", "expected", "actual", "score", "risk", "conf"))
    print("-" * 100)
    for case in cases:
        result = analyze_packet(case["packet"], client=client, cycle_id=f"demo-{case['case_id']}")
        artefacts.append({"case_id": case["case_id"], "expected_route": case["expected_route"], "result": result})
        route = result["routing"]["route"]
        ok = route == case["expected_route"]
        failures += 0 if ok else 1
        print(
            COLUMNS.format(
                ("PASS " if ok else "FAIL ") + case["case_id"],
                case["expected_route"],
                route,
                f"{result['completeness']['completeness_score']:.2f}",
                f"{result['financial_risk']['risk_level']}/{result['financial_risk']['risk_score']}",
                f"{result['underwriting_summary']['confidence']:.2f}",
            )
        )
        for reason in result["routing"]["reasons"]:
            print(f"    - {reason['code']}: {reason['detail']}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(artefacts, indent=2, default=str))
        print(f"\nartefacts written to {args.json_out}")

    print("\nEvery route - including READY_FOR_UNDERWRITER - is a recommendation.")
    print("Final approval remains with a human underwriter.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
