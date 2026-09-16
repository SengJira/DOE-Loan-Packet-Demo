from src.completeness import validate_packet_completeness
from src.schemas import COMPLETENESS_SCHEMA, is_valid


def test_clean_packet_is_complete(clean_packet):
    result = validate_packet_completeness(clean_packet)
    assert is_valid(result, COMPLETENESS_SCHEMA)
    assert result["complete"] is True
    assert result["completeness_score"] == 1.0
    assert result["missing_documents"] == []
    assert result["missing_fields"] == []
    assert set(result["present_documents"]) == {
        "loan_application",
        "identification",
        "payslip",
        "bank_statement",
        "proof_of_address",
    }


def test_missing_bank_statement_is_reported(packet_factory):
    result = validate_packet_completeness(packet_factory("case-4-missing-document"))
    assert result["complete"] is False
    assert result["missing_documents"] == ["bank_statement"]
    assert result["completeness_score"] < 1.0


def test_missing_field_is_reported_without_inventing_values(clean_packet):
    for doc in clean_packet["documents"]:
        doc["fields"].pop("employment_status", None)
    result = validate_packet_completeness(clean_packet)
    assert "employment_status" in result["missing_fields"]
    assert result["complete"] is False


def test_unreadable_document_counts_as_absent(clean_packet):
    for doc in clean_packet["documents"]:
        if doc["document_type"] == "identification":
            doc["readable"] = False
    result = validate_packet_completeness(clean_packet)
    assert result["unreadable_documents"] == ["SYN-LP-0001-id"]
    assert "identification" in result["missing_documents"]
    assert result["complete"] is False


def test_proof_of_address_can_be_waived(clean_packet):
    clean_packet["documents"] = [
        d for d in clean_packet["documents"] if d["document_type"] != "proof_of_address"
    ]
    clean_packet["requirements"] = {"proof_of_address_required": False}
    result = validate_packet_completeness(clean_packet)
    assert result["missing_documents"] == []
    assert result["completeness_score"] == 1.0
