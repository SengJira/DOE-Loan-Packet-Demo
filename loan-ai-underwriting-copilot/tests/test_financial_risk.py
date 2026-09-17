from borrower_context import build_borrower_context
from completeness import validate_packet_completeness
from cross_document import cross_document_reasoning
from financial_risk import assess_financial_risk
from schemas import FINANCIAL_RISK_SCHEMA, is_valid


def _assess(packet):
    completeness = validate_packet_completeness(packet)
    borrower = build_borrower_context(packet)
    cross = cross_document_reasoning(packet, borrower)
    return assess_financial_risk(packet, borrower, cross, completeness)


def _codes(result):
    return {flag["code"] for flag in result["risk_flags"]}


def test_clean_packet_is_low_risk(clean_packet):
    result = _assess(clean_packet)
    assert is_valid(result, FINANCIAL_RISK_SCHEMA)
    assert result["risk_level"] == "LOW"
    assert result["risk_score"] == 0
    assert result["risk_flags"] == []
    assert result["positive_indicators"]


def test_income_variance_flagged(packet_factory):
    result = _assess(packet_factory("case-2-income-mismatch"))
    assert "INCOME_VARIANCE_MEDIUM" in _codes(result) or "INCOME_VARIANCE_HIGH" in _codes(result)
    assert result["calculation_details"]["income_variance_pct"] > 10


def test_unstable_income_is_high_risk(packet_factory):
    result = _assess(packet_factory("case-5-unstable-income"))
    assert "UNSTABLE_INCOME_HIGH" in _codes(result)
    assert result["risk_level"] == "HIGH"


def test_missing_document_produces_flag_and_insufficient_data(packet_factory):
    result = _assess(packet_factory("case-4-missing-document"))
    assert "MISSING_REQUIRED_DOCUMENT" in _codes(result)
    assert "document:bank_statement" in result["insufficient_data"]


def test_returned_payments_and_negative_balance(clean_packet):
    for doc in clean_packet["documents"]:
        if doc["document_type"] == "bank_statement":
            doc["transactions"].append(
                {
                    "date": doc["fields"]["document_date"]["value"],
                    "description": "RETURNED DIRECT DEBIT - INSUFFICIENT FUNDS",
                    "amount": -2500,
                    "balance": -1500,
                }
            )
    result = _assess(clean_packet)
    assert "RETURNED_PAYMENTS" in _codes(result)
    assert "NEGATIVE_BALANCE_EVENTS" in _codes(result)
    assert result["risk_level"] == "HIGH"


def test_loan_to_income_ratio(clean_packet):
    for doc in clean_packet["documents"]:
        if doc["document_type"] == "loan_application":
            doc["fields"]["requested_loan_amount"]["value"] = 6_000_000
    result = _assess(clean_packet)
    assert "LOAN_TO_INCOME_HIGH" in _codes(result)
    assert result["calculation_details"]["loan_to_annual_income_ratio"] > 5


def test_every_flag_has_evidence_and_score_is_bounded(packet_factory):
    for case_id in ("case-2-income-mismatch", "case-4-missing-document", "case-5-unstable-income"):
        result = _assess(packet_factory(case_id))
        assert 0 <= result["risk_score"] <= 100
        for flag in result["risk_flags"]:
            assert flag["evidence"], f"{case_id}/{flag['code']} has no evidence"


def test_empty_packet_is_unknown_risk():
    packet = {"packet_id": "SYN-EMPTY", "documents": []}
    result = _assess(packet)
    assert result["risk_level"] in {"UNKNOWN", "HIGH"}
    assert result["insufficient_data"]
