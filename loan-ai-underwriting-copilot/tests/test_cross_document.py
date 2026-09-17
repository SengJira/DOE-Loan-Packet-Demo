from borrower_context import build_borrower_context
from cross_document import (
    cross_document_reasoning,
    has_high_severity_mismatch,
    identity_uncertain,
)
from schemas import CROSS_DOCUMENT_SCHEMA, is_valid


def _run(packet):
    return cross_document_reasoning(packet, build_borrower_context(packet))


def _check(result, check_id):
    return next(c for c in result["checks"] if c["check_id"] == check_id)


def test_clean_packet_has_no_mismatch(clean_packet):
    result = _run(clean_packet)
    assert is_valid(result, CROSS_DOCUMENT_SCHEMA)
    assert result["consistency_score"] == 1.0
    assert not has_high_severity_mismatch(result)
    assert not identity_uncertain(result)


def test_cosmetic_differences_do_not_trigger_mismatch(clean_packet):
    for doc in clean_packet["documents"]:
        if doc["document_type"] == "identification":
            doc["fields"]["applicant_name"]["value"] = "  somchai   JAIDEE "
            doc["fields"]["identification_number"]["value"] = "1 1023 45678 90 1"
            doc["fields"]["date_of_birth"]["value"] = "12 April 1988"
    result = _run(clean_packet)
    assert _check(result, "name_consistency")["status"] == "MATCH"
    assert _check(result, "identification_number_consistency")["status"] == "MATCH"
    assert _check(result, "date_of_birth_consistency")["status"] == "MATCH"


def test_income_mismatch_is_high_severity_with_evidence(packet_factory):
    result = _run(packet_factory("case-2-income-mismatch"))
    check = _check(result, "declared_vs_payslip_income")
    assert check["status"] == "MISMATCH"
    assert check["severity"] == "HIGH"
    assert check["evidence"] and all("document_type" in e for e in check["evidence"])
    assert has_high_severity_mismatch(result)


def test_address_mismatch_marks_identity_uncertain(packet_factory):
    result = _run(packet_factory("case-3-address-mismatch"))
    assert _check(result, "address_consistency")["status"] == "MISMATCH"
    assert identity_uncertain(result)


def test_missing_source_yields_insufficient_data(packet_factory):
    result = _run(packet_factory("case-4-missing-document"))
    assert _check(result, "payslip_vs_bank_credits")["status"] == "INSUFFICIENT_DATA"
    assert result["consistency_score"] < 1.0


def test_every_mismatch_cites_evidence(packet_factory):
    for case_id in ("case-2-income-mismatch", "case-3-address-mismatch", "case-5-unstable-income"):
        result = _run(packet_factory(case_id))
        for check in result["checks"]:
            if check["status"] == "MISMATCH":
                assert check["evidence"], f"{case_id}/{check['check_id']} has no evidence"


def test_employer_mismatch_detected(clean_packet):
    for doc in clean_packet["documents"]:
        if doc["document_type"] == "payslip":
            doc["fields"]["employer"]["value"] = "Bangkok Logistics PLC"
    result = _run(clean_packet)
    assert _check(result, "employer_consistency")["status"] == "MISMATCH"
