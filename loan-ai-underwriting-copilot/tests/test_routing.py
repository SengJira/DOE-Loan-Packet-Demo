import pytest

from src.config import load_config
from src.demo_cases import load_cases
from src.pipeline_runner import analyze_packet
from src.routing import HUMAN_REVIEW, READY_FOR_UNDERWRITER, route_by_confidence_and_risk
from src.schemas import ROUTING_SCHEMA, is_valid


def _reason_codes(routing):
    return {reason["code"] for reason in routing["reasons"]}


@pytest.fixture
def baseline():
    completeness = {
        "packet_id": "SYN-LP-0001",
        "complete": True,
        "present_documents": ["loan_application"],
        "missing_documents": [],
        "missing_fields": [],
        "unreadable_documents": [],
        "completeness_score": 1.0,
    }
    cross = {"packet_id": "SYN-LP-0001", "consistency_score": 1.0, "checks": []}
    risk = {
        "packet_id": "SYN-LP-0001",
        "risk_level": "LOW",
        "risk_score": 0,
        "risk_flags": [],
        "positive_indicators": [],
        "insufficient_data": [],
        "calculation_details": {},
    }
    summary = {
        "packet_id": "SYN-LP-0001",
        "recommendation": "READY_FOR_UNDERWRITER",
        "confidence": 0.95,
        "executive_summary": "ok",
        "key_findings": [],
        "risk_flags": [],
        "missing_information": [],
        "required_actions": [],
        "evidence_references": [],
        "model": "nvidia/llama-3.1-nemotron-70b-instruct",
        "generated_at": "2026-01-01T00:00:00Z",
        "disclaimer": "AI-generated analysis for human review; not a final lending decision.",
        "model_output_valid": True,
    }
    return completeness, cross, risk, summary


def test_clean_inputs_route_to_ready(baseline):
    routing = route_by_confidence_and_risk(*baseline)
    assert is_valid(routing, ROUTING_SCHEMA)
    assert routing["route"] == READY_FOR_UNDERWRITER
    assert routing["final_decision_owner"] == "human_underwriter"


def test_low_confidence_routes_to_human(baseline):
    completeness, cross, risk, summary = baseline
    summary["confidence"] = 0.84
    routing = route_by_confidence_and_risk(completeness, cross, risk, summary)
    assert routing["route"] == HUMAN_REVIEW
    assert "LOW_AI_CONFIDENCE" in _reason_codes(routing)


def test_low_completeness_routes_to_human(baseline):
    completeness, cross, risk, summary = baseline
    completeness["completeness_score"] = 0.89
    routing = route_by_confidence_and_risk(completeness, cross, risk, summary)
    assert "LOW_COMPLETENESS" in _reason_codes(routing)


def test_high_risk_routes_to_human(baseline):
    completeness, cross, risk, summary = baseline
    risk["risk_level"] = "HIGH"
    assert route_by_confidence_and_risk(completeness, cross, risk, summary)["route"] == HUMAN_REVIEW


def test_high_severity_mismatch_routes_to_human(baseline):
    completeness, cross, risk, summary = baseline
    cross["checks"] = [
        {
            "check_id": "declared_vs_payslip_income",
            "field": "declared_monthly_income",
            "status": "MISMATCH",
            "severity": "HIGH",
            "observed_values": [],
            "evidence": [{"document_type": "payslip", "field": "net_monthly_income"}],
            "explanation": "mismatch",
        }
    ]
    routing = route_by_confidence_and_risk(completeness, cross, risk, summary)
    assert {"HIGH_SEVERITY_MISMATCH", "IDENTITY_UNCERTAIN"} & _reason_codes(routing)
    assert routing["route"] == HUMAN_REVIEW


def test_missing_document_routes_to_human(baseline):
    completeness, cross, risk, summary = baseline
    completeness["missing_documents"] = ["bank_statement"]
    routing = route_by_confidence_and_risk(completeness, cross, risk, summary)
    assert "MISSING_DOCUMENT" in _reason_codes(routing)


def test_invalid_model_output_routes_to_human(baseline):
    completeness, cross, risk, summary = baseline
    summary["model_output_valid"] = False
    summary["error"] = "NvidiaApiError: timeout"
    routing = route_by_confidence_and_risk(completeness, cross, risk, summary)
    assert "MODEL_OUTPUT_INVALID" in _reason_codes(routing)


def test_thresholds_are_configurable(baseline, monkeypatch):
    completeness, cross, risk, summary = baseline
    summary["confidence"] = 0.70
    monkeypatch.setenv("AI_CONFIDENCE_THRESHOLD", "0.60")
    routing = route_by_confidence_and_risk(completeness, cross, risk, summary, config=load_config())
    assert routing["route"] == READY_FOR_UNDERWRITER
    assert routing["thresholds"]["AI_CONFIDENCE_THRESHOLD"] == 0.60


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["case_id"])
def test_demo_cases_follow_expected_route(case, stub_client):
    result = analyze_packet(case["packet"], client=stub_client())
    assert result["routing"]["route"] == case["expected_route"]


def test_rerun_is_idempotent(clean_packet, stub_client):
    first = analyze_packet(clean_packet, client=stub_client())
    second = analyze_packet(clean_packet, client=stub_client())
    assert first["input_fingerprint"] == second["input_fingerprint"]
    assert first["routing"]["route"] == second["routing"]["route"]
    assert first["financial_risk"] == second["financial_risk"]
