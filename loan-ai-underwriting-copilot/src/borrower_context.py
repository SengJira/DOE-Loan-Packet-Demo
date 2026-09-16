"""Node 2 - build_borrower_context.

Normalises the extracted packet into a single borrower view plus a banking
summary, keeping a source reference (document + field) for every value.
"""

from __future__ import annotations

import re
from typing import Any

from . import extraction
from .normalization import coefficient_of_variation, parse_date, to_float
from .schemas import BORROWER_CONTEXT_SCHEMA, validate

SALARY_KEYWORDS = ("salary", "payroll", "wage", "income", "เงินเดือน")
RETURNED_KEYWORDS = ("returned", "nsf", "insufficient funds", "bounced", "reversal", "unpaid")
_RETURNED_PATTERN = re.compile(
    r"\b(?:" + "|".join(k.replace(" ", r"\s+") for k in RETURNED_KEYWORDS) + r")\b"
)


def is_returned_payment(description: str) -> bool:
    return bool(_RETURNED_PATTERN.search(description.lower()))


def _month_key(value: Any) -> str | None:
    parsed = parse_date(value)
    return f"{parsed.year:04d}-{parsed.month:02d}" if parsed else None


def _is_salary_credit(transaction: dict[str, Any]) -> bool:
    amount = to_float(transaction.get("amount"))
    if amount is None or amount <= 0:
        return False
    category = str(transaction.get("category") or "").lower()
    description = str(transaction.get("description") or "").lower()
    if "salary" in category or "payroll" in category:
        return True
    return any(keyword in description for keyword in SALARY_KEYWORDS)


def summarize_banking(packet: dict[str, Any]) -> dict[str, Any]:
    """Aggregate bank-statement evidence; pre-aggregated summaries win."""
    summaries = extraction.monthly_summaries(packet)
    transactions = extraction.bank_transactions(packet)

    salary_by_month: dict[str, float] = {}
    outflow_by_month: dict[str, float] = {}
    negative_balance_events = 0
    returned_payment_events = 0
    sources: list[dict[str, Any]] = []

    for entry in summaries:
        month = str(entry.get("month") or entry.get("period") or len(salary_by_month))
        salary = to_float(entry.get("salary_credit") or entry.get("salary_deposit"))
        if salary is not None:
            salary_by_month[month] = salary_by_month.get(month, 0.0) + salary
        outflow = to_float(entry.get("total_outflow") or entry.get("outflow"))
        if outflow is not None:
            outflow_by_month[month] = outflow_by_month.get(month, 0.0) + abs(outflow)
        negative_balance_events += int(to_float(entry.get("negative_balance_events")) or 0)
        returned_payment_events += int(to_float(entry.get("returned_payment_events")) or 0)
        min_balance = to_float(entry.get("min_balance"))
        if min_balance is not None and min_balance < 0:
            negative_balance_events += 1
        sources.append(
            {
                "field": "banking_summary",
                "document_id": entry.get("document_id"),
                "document_type": "bank_statement",
                "source_field": f"monthly_summary[{month}]",
            }
        )

    for transaction in transactions:
        month = _month_key(transaction.get("date")) or "unknown"
        amount = to_float(transaction.get("amount"))
        balance = to_float(transaction.get("balance"))
        description = str(transaction.get("description") or "").lower()
        if amount is not None:
            if _is_salary_credit(transaction) and not summaries:
                salary_by_month[month] = salary_by_month.get(month, 0.0) + amount
            elif amount < 0 and not summaries:
                outflow_by_month[month] = outflow_by_month.get(month, 0.0) + abs(amount)
        if balance is not None and balance < 0:
            negative_balance_events += 1
        if is_returned_payment(description):
            returned_payment_events += 1
    if transactions:
        sources.append(
            {
                "field": "banking_summary",
                "document_id": transactions[0].get("document_id"),
                "document_type": "bank_statement",
                "source_field": "transactions",
            }
        )

    salary_values = list(salary_by_month.values())
    outflow_values = list(outflow_by_month.values())
    summary = {
        "average_monthly_salary_credit": round(sum(salary_values) / len(salary_values), 2)
        if salary_values
        else 0.0,
        "salary_credit_variability": round(coefficient_of_variation(salary_values) or 0.0, 2),
        "average_monthly_outflow": round(sum(outflow_values) / len(outflow_values), 2)
        if outflow_values
        else 0.0,
        "negative_balance_events": negative_balance_events,
        "returned_payment_events": returned_payment_events,
    }
    return {
        "summary": summary,
        "sources": sources,
        "monthly_salary_credits": {k: round(v, 2) for k, v in salary_by_month.items()},
        "months_observed": len(salary_by_month),
    }


def build_borrower_context(packet: dict[str, Any]) -> dict[str, Any]:
    pid = extraction.packet_id(packet)
    references: list[dict[str, Any]] = []

    def text_field(name: str, document_types: list[str] | None = None) -> str | None:
        hit = extraction.first_field(packet, name, document_types=document_types)
        if not hit:
            return None
        references.append(hit.reference())
        value = hit.value
        return str(value).strip() if value is not None else None

    def amount_field(name: str, document_types: list[str] | None = None) -> float:
        hit = extraction.first_field(packet, name, document_types=document_types)
        if not hit:
            return 0.0
        references.append(hit.reference())
        return float(to_float(hit.value) or 0.0)

    banking = summarize_banking(packet)
    references.extend(banking["sources"])

    context = {
        "packet_id": pid,
        "borrower": {
            "full_name": text_field("applicant_name"),
            "date_of_birth": text_field("date_of_birth"),
            "identification_number": text_field("identification_number"),
            "declared_address": text_field("current_address", ["loan_application"]),
            "document_address": text_field(
                "current_address", ["proof_of_address", "identification", "bank_statement"]
            ),
            "employer": text_field("employer"),
            "employment_status": text_field("employment_status"),
            "declared_monthly_income": amount_field("declared_monthly_income"),
            "verified_monthly_income": amount_field("verified_monthly_income"),
            "requested_loan_amount": amount_field("requested_loan_amount"),
        },
        "banking_summary": banking["summary"],
        "source_references": references,
        "derived": {
            "loan_purpose": extraction.first_field(packet, "loan_purpose").value
            if extraction.first_field(packet, "loan_purpose")
            else None,
            "monthly_salary_credits": banking["monthly_salary_credits"],
            "months_observed": banking["months_observed"],
        },
    }
    return validate(context, BORROWER_CONTEXT_SCHEMA, "borrower_context")
