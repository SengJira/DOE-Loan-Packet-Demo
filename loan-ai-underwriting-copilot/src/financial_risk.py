"""Node 4 - assess_financial_risk.

Deterministic, fully auditable demo heuristics. This is NOT a credit score and
must never be presented as one: it only ranks how much underwriter attention a
packet needs. Every flag carries the evidence that produced it.
"""

from __future__ import annotations

from typing import Any

import extraction
from config import Config, load_config
from normalization import relative_difference, to_float
from schemas import FINANCIAL_RISK_SCHEMA, validate

# code -> (severity, points). Kept in one table so the README can document it.
RISK_POINTS: dict[str, int] = {
    "INCOME_VARIANCE_HIGH": 25,
    "INCOME_VARIANCE_MEDIUM": 12,
    "SALARY_DEPOSIT_INCONSISTENT": 18,
    "UNSTABLE_INCOME_HIGH": 25,
    "UNSTABLE_INCOME_MEDIUM": 15,
    "LOAN_TO_INCOME_HIGH": 20,
    "LOAN_TO_INCOME_MEDIUM": 10,
    "HIGH_RECURRING_OBLIGATIONS": 10,
    "NEGATIVE_BALANCE_EVENTS_HIGH": 18,
    "NEGATIVE_BALANCE_EVENTS": 10,
    "RETURNED_PAYMENTS": 20,
    "UNUSUAL_TRANSACTION": 8,
    "DUPLICATE_DOCUMENT_SUSPECTED": 10,
    "IDENTITY_INCONSISTENCY": 25,
    "MISSING_REQUIRED_DOCUMENT": 12,
}

HIGH_RISK_SCORE = 50
MEDIUM_RISK_SCORE = 25
UNSTABLE_INCOME_HIGH_CV = 30.0
OBLIGATION_RATIO_LIMIT = 0.8
UNUSUAL_CREDIT_MULTIPLIER = 3.0


def _flag(code: str, severity: str, description: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    return {"code": code, "severity": severity, "description": description, "evidence": evidence}


def _bank_evidence(field: str, value: Any) -> dict[str, Any]:
    return {
        "document_id": "bank_statement",
        "document_type": "bank_statement",
        "field": field,
        "value": str(value),
    }


def assess_financial_risk(
    packet: dict[str, Any],
    borrower_context: dict[str, Any],
    cross_document_result: dict[str, Any] | None = None,
    completeness_result: dict[str, Any] | None = None,
    config: Config | None = None,
) -> dict[str, Any]:
    cfg = config or load_config()
    borrower = borrower_context.get("borrower", {})
    banking = borrower_context.get("banking_summary", {})

    flags: list[dict[str, Any]] = []
    positives: list[str] = []
    insufficient: list[str] = []
    details: dict[str, Any] = {}

    declared = to_float(borrower.get("declared_monthly_income")) or 0.0
    verified = to_float(borrower.get("verified_monthly_income")) or 0.0
    requested = to_float(borrower.get("requested_loan_amount")) or 0.0
    salary_credit = to_float(banking.get("average_monthly_salary_credit")) or 0.0
    variability = to_float(banking.get("salary_credit_variability")) or 0.0
    outflow = to_float(banking.get("average_monthly_outflow")) or 0.0
    negative_events = int(to_float(banking.get("negative_balance_events")) or 0)
    returned_events = int(to_float(banking.get("returned_payment_events")) or 0)

    # 1. declared vs verified income variance
    if declared > 0 and verified > 0:
        variance = relative_difference(declared, verified) or 0.0
        details["income_variance_pct"] = round(variance, 2)
        evidence = [
            {"field": "declared_monthly_income", "value": f"{declared:,.2f}", "document_type": "loan_application"},
            {"field": "verified_monthly_income", "value": f"{verified:,.2f}", "document_type": "payslip"},
        ]
        if variance > cfg.income_variance_high_pct:
            flags.append(
                _flag(
                    "INCOME_VARIANCE_HIGH",
                    "HIGH",
                    f"Declared income differs from verified income by {variance:.1f}%.",
                    evidence,
                )
            )
        elif variance > cfg.income_variance_warning_pct:
            flags.append(
                _flag(
                    "INCOME_VARIANCE_MEDIUM",
                    "MEDIUM",
                    f"Declared income differs from verified income by {variance:.1f}%.",
                    evidence,
                )
            )
        else:
            positives.append("Declared income is supported by verified payslip income.")
    else:
        insufficient.append("declared_or_verified_monthly_income")

    # 2. salary deposit consistency (payslip vs bank credits)
    if verified > 0 and salary_credit > 0:
        deposit_gap = relative_difference(verified, salary_credit) or 0.0
        details["payslip_vs_deposit_gap_pct"] = round(deposit_gap, 2)
        if deposit_gap > cfg.salary_variability_warning_pct:
            flags.append(
                _flag(
                    "SALARY_DEPOSIT_INCONSISTENT",
                    "HIGH" if deposit_gap > cfg.income_variance_high_pct else "MEDIUM",
                    f"Bank salary credits differ from payslip income by {deposit_gap:.1f}%.",
                    [
                        {"field": "verified_monthly_income", "value": f"{verified:,.2f}", "document_type": "payslip"},
                        _bank_evidence("average_monthly_salary_credit", f"{salary_credit:,.2f}"),
                    ],
                )
            )
        else:
            positives.append("Bank salary credits corroborate the payslip income.")
    else:
        insufficient.append("bank_salary_credits")

    # 3. income stability
    months_observed = int((borrower_context.get("derived") or {}).get("months_observed") or 0)
    if months_observed >= 2:
        details["salary_credit_variability_pct"] = round(variability, 2)
        evidence = [_bank_evidence("salary_credit_variability", f"{variability:.2f}%")]
        if variability > UNSTABLE_INCOME_HIGH_CV:
            flags.append(
                _flag(
                    "UNSTABLE_INCOME_HIGH",
                    "HIGH",
                    f"Monthly salary credits vary by {variability:.1f}% across {months_observed} months.",
                    evidence,
                )
            )
        elif variability > cfg.salary_variability_warning_pct:
            flags.append(
                _flag(
                    "UNSTABLE_INCOME_MEDIUM",
                    "MEDIUM",
                    f"Monthly salary credits vary by {variability:.1f}% across {months_observed} months.",
                    evidence,
                )
            )
        else:
            positives.append(f"Salary credits are stable across {months_observed} months.")
    else:
        insufficient.append("monthly_salary_history")

    # 4. loan size relative to income
    income_for_ratio = verified or salary_credit or declared
    if income_for_ratio > 0 and requested > 0:
        ratio = requested / (income_for_ratio * 12)
        details["loan_to_annual_income_ratio"] = round(ratio, 3)
        evidence = [
            {"field": "requested_loan_amount", "value": f"{requested:,.2f}", "document_type": "loan_application"},
            {"field": "monthly_income_used", "value": f"{income_for_ratio:,.2f}", "document_type": "payslip"},
        ]
        if ratio > cfg.max_loan_to_annual_income_ratio:
            flags.append(
                _flag(
                    "LOAN_TO_INCOME_HIGH",
                    "HIGH",
                    f"Requested amount is {ratio:.2f}x annual income.",
                    evidence,
                )
            )
        elif ratio > cfg.max_loan_to_annual_income_ratio * 0.6:
            flags.append(
                _flag(
                    "LOAN_TO_INCOME_MEDIUM",
                    "MEDIUM",
                    f"Requested amount is {ratio:.2f}x annual income.",
                    evidence,
                )
            )
        else:
            positives.append("Requested amount is modest relative to verified income.")
    else:
        insufficient.append("loan_to_income_inputs")

    # 5. recurring obligations
    if outflow > 0 and income_for_ratio > 0:
        obligation_ratio = outflow / income_for_ratio
        details["obligation_ratio"] = round(obligation_ratio, 3)
        if obligation_ratio > OBLIGATION_RATIO_LIMIT:
            flags.append(
                _flag(
                    "HIGH_RECURRING_OBLIGATIONS",
                    "MEDIUM",
                    f"Average monthly outflow is {obligation_ratio * 100:.0f}% of monthly income.",
                    [_bank_evidence("average_monthly_outflow", f"{outflow:,.2f}")],
                )
            )
        else:
            positives.append("Monthly outflow leaves headroom against monthly income.")
    else:
        insufficient.append("average_monthly_outflow")

    # 6/7. negative balance and returned payments
    if negative_events:
        flags.append(
            _flag(
                "NEGATIVE_BALANCE_EVENTS_HIGH" if negative_events >= 3 else "NEGATIVE_BALANCE_EVENTS",
                "HIGH" if negative_events >= 3 else "MEDIUM",
                f"{negative_events} negative balance event(s) observed in the bank statement.",
                [_bank_evidence("negative_balance_events", negative_events)],
            )
        )
    if returned_events:
        flags.append(
            _flag(
                "RETURNED_PAYMENTS",
                "HIGH",
                f"{returned_events} returned payment event(s) observed in the bank statement.",
                [_bank_evidence("returned_payment_events", returned_events)],
            )
        )
    if not negative_events and not returned_events and salary_credit:
        positives.append("No negative balance or returned payment events observed.")

    # 8. unusual transactions
    transactions = extraction.bank_transactions(packet)
    if transactions and salary_credit:
        threshold = salary_credit * UNUSUAL_CREDIT_MULTIPLIER
        for transaction in transactions:
            amount = to_float(transaction.get("amount"))
            if amount is not None and amount > threshold:
                flags.append(
                    _flag(
                        "UNUSUAL_TRANSACTION",
                        "MEDIUM",
                        (
                            f"Credit of {amount:,.2f} exceeds {UNUSUAL_CREDIT_MULTIPLIER:.0f}x the "
                            "average monthly salary credit."
                        ),
                        [
                            {
                                "document_id": transaction.get("document_id", "bank_statement"),
                                "document_type": "bank_statement",
                                "field": "transactions[].amount",
                                "value": f"{amount:,.2f} on {transaction.get('date')}",
                            }
                        ],
                    )
                )

    # 9. duplicate documents
    for code, description, evidence in _duplicate_document_flags(packet):
        flags.append(_flag(code, "MEDIUM", description, evidence))

    # 10. identity inconsistency (from node 3)
    if cross_document_result:
        for check in cross_document_result.get("checks", []):
            if check["check_id"] in {
                "name_consistency",
                "identification_number_consistency",
                "date_of_birth_consistency",
            } and check["status"] == "MISMATCH":
                flags.append(
                    _flag(
                        "IDENTITY_INCONSISTENCY",
                        "HIGH",
                        f"Identity field '{check['field']}' is inconsistent across documents.",
                        check["evidence"],
                    )
                )

    # 11. missing required documents (from node 1)
    for missing in (completeness_result or {}).get("missing_documents", []):
        flags.append(
            _flag(
                "MISSING_REQUIRED_DOCUMENT",
                "HIGH" if missing in {"identification", "loan_application"} else "MEDIUM",
                f"Required document '{missing}' is absent from the packet.",
                [{"document_type": missing, "field": "document_presence", "value": "missing"}],
            )
        )
        insufficient.append(f"document:{missing}")

    score = min(100, sum(RISK_POINTS.get(flag["code"], 5) for flag in flags))
    if len(insufficient) >= 3 and not flags:
        level = "UNKNOWN"
    elif score >= HIGH_RISK_SCORE or any(f["severity"] == "HIGH" for f in flags):
        level = "HIGH"
    elif score >= MEDIUM_RISK_SCORE or flags:
        level = "MEDIUM"
    elif insufficient:
        level = "UNKNOWN" if len(insufficient) >= 3 else "LOW"
    else:
        level = "LOW"

    details["score_breakdown"] = {flag["code"]: RISK_POINTS.get(flag["code"], 5) for flag in flags}
    details["scoring_note"] = (
        "Transparent demo heuristics; not a credit score and not a lending decision."
    )

    result = {
        "packet_id": borrower_context.get("packet_id") or extraction.packet_id(packet),
        "risk_level": level,
        "risk_score": score,
        "risk_flags": flags,
        "positive_indicators": positives,
        "insufficient_data": sorted(set(insufficient)),
        "calculation_details": details,
    }
    return validate(result, FINANCIAL_RISK_SCHEMA, "financial_risk")


def _duplicate_document_flags(packet: dict[str, Any]):
    seen: dict[tuple, str] = {}
    out = []
    for doc in extraction.documents(packet):
        fields = extraction.document_fields(doc)
        fingerprint = (
            doc["document_type"],
            str(doc.get("checksum") or doc.get("hash") or ""),
            str(extraction.raw_value(fields.get("document_date")) or ""),
            str(extraction.raw_value(fields.get("verified_monthly_income")) or ""),
        )
        if fingerprint[1] == "" and fingerprint[2] == "" and fingerprint[3] == "":
            continue
        if fingerprint in seen:
            out.append(
                (
                    "DUPLICATE_DOCUMENT_SUSPECTED",
                    f"Documents '{seen[fingerprint]}' and '{doc['document_id']}' look identical.",
                    [
                        {
                            "document_id": doc["document_id"],
                            "document_type": doc["document_type"],
                            "field": "document_fingerprint",
                            "value": "matches " + seen[fingerprint],
                        }
                    ],
                )
            )
        else:
            seen[fingerprint] = doc["document_id"]
    return out
