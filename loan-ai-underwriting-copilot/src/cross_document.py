"""Node 3 - cross_document_reasoning.

Deterministic, normalised comparisons run first and own the verdict. An
optional LLM pass can add commentary, but it can only ever add advisory checks -
it cannot overturn a deterministic result or introduce unsupported facts.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

import extraction
from normalization import (
    normalize_address,
    normalize_date,
    normalize_employer,
    normalize_id_number,
    normalize_name,
    parse_date,
    relative_difference,
    to_float,
)
from schemas import CROSS_DOCUMENT_SCHEMA, validate

SEVERITY_WEIGHT = {"HIGH": 1.0, "MEDIUM": 0.5, "LOW": 0.25}
MAX_DOCUMENT_AGE_DAYS = 120
INCOME_MISMATCH_PCT = 10.0


def _evidence(hits: Iterable[extraction.FieldHit]) -> list[dict[str, Any]]:
    return [
        {
            "document_id": hit.document_id,
            "document_type": hit.document_type,
            "field": hit.source_field,
            "value": str(hit.value),
        }
        for hit in hits
    ]


def _check(
    check_id: str,
    field: str,
    status: str,
    severity: str,
    observed_values: list[Any],
    evidence: list[dict[str, Any]],
    explanation: str,
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "field": field,
        "status": status,
        "severity": severity,
        "observed_values": observed_values,
        "evidence": evidence,
        "explanation": explanation,
    }


def _agreement_check(
    packet: dict[str, Any],
    check_id: str,
    field: str,
    severity: str,
    normalizer,
    label: str,
    document_types: list[str] | None = None,
) -> dict[str, Any]:
    hits = extraction.find_field(packet, field, document_types=document_types)
    observed = [str(hit.value) for hit in hits]
    if len(hits) < 2:
        return _check(
            check_id,
            field,
            "INSUFFICIENT_DATA",
            severity,
            observed,
            _evidence(hits),
            f"{label} appears in fewer than two documents, so it cannot be cross-checked.",
        )
    normalized = {normalizer(hit.value) for hit in hits}
    if len(normalized) == 1:
        return _check(
            check_id,
            field,
            "MATCH",
            severity,
            observed,
            _evidence(hits),
            f"{label} agrees across {len(hits)} documents after normalisation.",
        )
    return _check(
        check_id,
        field,
        "MISMATCH",
        severity,
        observed,
        _evidence(hits),
        f"{label} differs between documents after normalisation: {sorted(normalized)}.",
    )


def _address_check(packet: dict[str, Any]) -> dict[str, Any]:
    declared = extraction.find_field(packet, "current_address", document_types=["loan_application"])
    documented = extraction.find_field(
        packet, "current_address", document_types=["proof_of_address", "identification", "bank_statement"]
    )
    hits = declared + documented
    observed = [str(hit.value) for hit in hits]
    if not declared or not documented:
        return _check(
            "address_consistency",
            "current_address",
            "INSUFFICIENT_DATA",
            "MEDIUM",
            observed,
            _evidence(hits),
            "Address is not available from both the application and a supporting document.",
        )
    if normalize_address(declared[0].value) == normalize_address(documented[0].value):
        return _check(
            "address_consistency",
            "current_address",
            "MATCH",
            "MEDIUM",
            observed,
            _evidence(hits),
            "Declared address matches the address on the supporting document.",
        )
    return _check(
        "address_consistency",
        "current_address",
        "MISMATCH",
        "MEDIUM",
        observed,
        _evidence(hits),
        "Declared address differs from the address on the supporting document.",
    )


def _declared_vs_payslip_income(packet: dict[str, Any]) -> dict[str, Any]:
    declared = extraction.first_field(packet, "declared_monthly_income")
    payslip = extraction.first_field(packet, "verified_monthly_income", document_types=["payslip"])
    hits = [h for h in (declared, payslip) if h]
    observed = [str(h.value) for h in hits]
    declared_amount = to_float(declared.value) if declared else None
    payslip_amount = to_float(payslip.value) if payslip else None
    difference = relative_difference(declared_amount, payslip_amount)
    if difference is None:
        return _check(
            "declared_vs_payslip_income",
            "declared_monthly_income",
            "INSUFFICIENT_DATA",
            "HIGH",
            observed,
            _evidence(hits),
            "Declared income or payslip income is missing, so the comparison cannot be made.",
        )
    if difference <= INCOME_MISMATCH_PCT:
        return _check(
            "declared_vs_payslip_income",
            "declared_monthly_income",
            "MATCH",
            "HIGH",
            observed,
            _evidence(hits),
            f"Declared income is within {difference:.1f}% of the payslip income.",
        )
    return _check(
        "declared_vs_payslip_income",
        "declared_monthly_income",
        "MISMATCH",
        "HIGH",
        observed,
        _evidence(hits),
        (
            f"Declared monthly income {declared_amount:,.2f} differs from payslip income "
            f"{payslip_amount:,.2f} by {difference:.1f}%."
        ),
    )


def _payslip_vs_bank_credits(packet: dict[str, Any], banking_summary: dict[str, Any]) -> dict[str, Any]:
    payslip = extraction.first_field(packet, "verified_monthly_income", document_types=["payslip"])
    average_credit = to_float(banking_summary.get("average_monthly_salary_credit"))
    payslip_amount = to_float(payslip.value) if payslip else None
    evidence = _evidence([payslip] if payslip else [])
    if average_credit:
        evidence.append(
            {
                "document_id": "bank_statement",
                "document_type": "bank_statement",
                "field": "average_monthly_salary_credit",
                "value": f"{average_credit:,.2f}",
            }
        )
    observed = [str(payslip.value) if payslip else None, average_credit]
    difference = relative_difference(payslip_amount, average_credit if average_credit else None)
    if difference is None or not average_credit:
        return _check(
            "payslip_vs_bank_credits",
            "verified_monthly_income",
            "INSUFFICIENT_DATA",
            "HIGH",
            observed,
            evidence,
            "Payslip income or bank salary credits are unavailable for comparison.",
        )
    if difference <= INCOME_MISMATCH_PCT:
        return _check(
            "payslip_vs_bank_credits",
            "verified_monthly_income",
            "MATCH",
            "HIGH",
            observed,
            evidence,
            f"Bank salary credits are within {difference:.1f}% of the payslip income.",
        )
    return _check(
        "payslip_vs_bank_credits",
        "verified_monthly_income",
        "MISMATCH",
        "HIGH",
        observed,
        evidence,
        (
            f"Payslip income {payslip_amount:,.2f} differs from average monthly salary credits "
            f"{average_credit:,.2f} by {difference:.1f}%."
        ),
    )


def _employer_check(packet: dict[str, Any]) -> dict[str, Any]:
    hits = extraction.find_field(packet, "employer", document_types=["loan_application", "payslip"])
    deposit_employers = []
    for transaction in extraction.bank_transactions(packet):
        description = str(transaction.get("description") or "")
        if any(word in description.lower() for word in ("salary", "payroll")):
            deposit_employers.append((transaction.get("document_id", "bank_statement"), description))
    observed = [str(hit.value) for hit in hits] + [d for _, d in deposit_employers]
    evidence = _evidence(hits) + [
        {
            "document_id": doc_id,
            "document_type": "bank_statement",
            "field": "transactions[].description",
            "value": description,
        }
        for doc_id, description in deposit_employers
    ]
    if len(hits) < 2 and not (hits and deposit_employers):
        return _check(
            "employer_consistency",
            "employer",
            "INSUFFICIENT_DATA",
            "MEDIUM",
            observed,
            evidence,
            "Employer name is available from fewer than two independent sources.",
        )
    normalized = {normalize_employer(hit.value) for hit in hits}
    if len(normalized) > 1:
        return _check(
            "employer_consistency",
            "employer",
            "MISMATCH",
            "MEDIUM",
            observed,
            evidence,
            f"Employer name differs between documents: {sorted(normalized)}.",
        )
    employer = next(iter(normalized), "")
    if deposit_employers and employer:
        matched = any(
            all(token in normalize_employer(description) for token in employer.split())
            for _, description in deposit_employers
        )
        if not matched:
            return _check(
                "employer_consistency",
                "employer",
                "MISMATCH",
                "MEDIUM",
                observed,
                evidence,
                "Salary deposit descriptions do not reference the stated employer.",
            )
    return _check(
        "employer_consistency",
        "employer",
        "MATCH",
        "MEDIUM",
        observed,
        evidence,
        "Employer name is consistent across the available sources.",
    )


def _document_dates_check(packet: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    reference = today or date.today()
    stale: list[str] = []
    evidence: list[dict[str, Any]] = []
    observed: list[Any] = []
    for doc in extraction.documents(packet):
        fields = extraction.document_fields(doc)
        raw = None
        for alias in extraction.FIELD_ALIASES["document_date"]:
            if alias in fields:
                raw = extraction.raw_value(fields[alias])
                break
        parsed = parse_date(raw)
        if not parsed:
            continue
        observed.append(f"{doc['document_type']}={normalize_date(raw)}")
        evidence.append(
            {
                "document_id": doc["document_id"],
                "document_type": doc["document_type"],
                "field": "document_date",
                "value": normalize_date(raw),
            }
        )
        age_days = (reference - parsed).days
        if age_days > MAX_DOCUMENT_AGE_DAYS:
            stale.append(f"{doc['document_type']} ({age_days} days old)")
    if not evidence:
        return _check(
            "document_dates",
            "document_date",
            "INSUFFICIENT_DATA",
            "LOW",
            observed,
            evidence,
            "No document dates were extracted.",
        )
    if stale:
        return _check(
            "document_dates",
            "document_date",
            "MISMATCH",
            "LOW",
            observed,
            evidence,
            f"Documents older than {MAX_DOCUMENT_AGE_DAYS} days: {', '.join(stale)}.",
        )
    return _check(
        "document_dates",
        "document_date",
        "MATCH",
        "LOW",
        observed,
        evidence,
        "All dated documents fall inside the accepted recency window.",
    )


def _loan_request_check(packet: dict[str, Any]) -> dict[str, Any]:
    amount_hit = extraction.first_field(packet, "requested_loan_amount")
    purpose_hit = extraction.first_field(packet, "loan_purpose")
    hits = [h for h in (amount_hit, purpose_hit) if h]
    observed = [str(h.value) for h in hits]
    if not amount_hit or not purpose_hit:
        return _check(
            "loan_request_declaration",
            "requested_loan_amount",
            "INSUFFICIENT_DATA",
            "LOW",
            observed,
            _evidence(hits),
            "Requested loan amount or declared purpose is missing from the application.",
        )
    return _check(
        "loan_request_declaration",
        "requested_loan_amount",
        "MATCH",
        "LOW",
        observed,
        _evidence(hits),
        "Requested loan amount and declared purpose are both present on the application.",
    )


def consistency_score(checks: list[dict[str, Any]]) -> float:
    total_weight = sum(SEVERITY_WEIGHT[c["severity"]] for c in checks) or 1.0
    penalty = 0.0
    for check in checks:
        weight = SEVERITY_WEIGHT[check["severity"]]
        if check["status"] == "MISMATCH":
            penalty += weight
        elif check["status"] == "INSUFFICIENT_DATA":
            penalty += weight * 0.5
    return round(max(0.0, 1.0 - penalty / total_weight), 4)


def cross_document_reasoning(
    packet: dict[str, Any],
    borrower_context: dict[str, Any] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    banking_summary = (borrower_context or {}).get("banking_summary", {})
    checks = [
        _agreement_check(
            packet,
            "name_consistency",
            "applicant_name",
            "HIGH",
            normalize_name,
            "Applicant name",
            document_types=["loan_application", "identification", "payslip", "bank_statement"],
        ),
        _agreement_check(
            packet,
            "identification_number_consistency",
            "identification_number",
            "HIGH",
            normalize_id_number,
            "Identification number",
            document_types=["identification", "loan_application"],
        ),
        _agreement_check(
            packet,
            "date_of_birth_consistency",
            "date_of_birth",
            "HIGH",
            normalize_date,
            "Date of birth",
            document_types=["identification", "loan_application"],
        ),
        _address_check(packet),
        _declared_vs_payslip_income(packet),
        _payslip_vs_bank_credits(packet, banking_summary),
        _employer_check(packet),
        _document_dates_check(packet, today=today),
        _loan_request_check(packet),
    ]
    result = {
        "packet_id": extraction.packet_id(packet),
        "consistency_score": consistency_score(checks),
        "checks": checks,
    }
    return validate(result, CROSS_DOCUMENT_SCHEMA, "cross_document")


def has_high_severity_mismatch(cross_document_result: dict[str, Any]) -> bool:
    return any(
        c["status"] == "MISMATCH" and c["severity"] == "HIGH"
        for c in cross_document_result.get("checks", [])
    )


def identity_uncertain(cross_document_result: dict[str, Any]) -> bool:
    identity_checks = {
        "name_consistency",
        "identification_number_consistency",
        "date_of_birth_consistency",
    }
    for check in cross_document_result.get("checks", []):
        if check["check_id"] in identity_checks and check["status"] in {"MISMATCH", "INSUFFICIENT_DATA"}:
            return True
        # A declared address that contradicts KYC documents is an identity concern.
        if check["check_id"] == "address_consistency" and check["status"] == "MISMATCH":
            return True
    return False
