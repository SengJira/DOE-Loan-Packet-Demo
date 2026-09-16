"""Tolerant accessors over the structured output of the extraction pipeline.

The upstream `loan-packet-processing` pipeline writes one JSON item per loan
packet. Field values may be plain scalars or ``{"value": ..., "confidence": ...}``
objects, and document/field naming varies slightly between extractors, so every
read goes through the alias tables below.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .schemas import DOCUMENT_CATEGORIES

DOCUMENT_TYPE_ALIASES: dict[str, str] = {
    "loan_application": "loan_application",
    "application": "loan_application",
    "loan_app": "loan_application",
    "credit_application": "loan_application",
    "identification": "identification",
    "id": "identification",
    "id_card": "identification",
    "national_id": "identification",
    "passport": "identification",
    "drivers_license": "identification",
    "payslip": "payslip",
    "pay_slip": "payslip",
    "salary_slip": "payslip",
    "proof_of_income": "payslip",
    "income_statement": "payslip",
    "bank_statement": "bank_statement",
    "bank_statements": "bank_statement",
    "statement": "bank_statement",
    "proof_of_address": "proof_of_address",
    "utility_bill": "proof_of_address",
    "address_proof": "proof_of_address",
}

FIELD_ALIASES: dict[str, list[str]] = {
    "applicant_name": ["applicant_name", "full_name", "name", "borrower_name", "account_holder"],
    "identification_number": [
        "identification_number",
        "id_number",
        "national_id",
        "document_number",
        "passport_number",
        "kyc_reference",
    ],
    "date_of_birth": ["date_of_birth", "dob", "birth_date"],
    "current_address": ["current_address", "address", "residential_address", "home_address"],
    "employer": ["employer", "employer_name", "company", "company_name"],
    "declared_monthly_income": [
        "declared_monthly_income",
        "monthly_income",
        "declared_income",
        "stated_monthly_income",
    ],
    "verified_monthly_income": [
        "verified_monthly_income",
        "net_monthly_income",
        "net_pay",
        "net_salary",
        "monthly_net_pay",
        "gross_monthly_income",
        "gross_pay",
    ],
    "requested_loan_amount": ["requested_loan_amount", "loan_amount", "amount_requested"],
    "employment_status": ["employment_status", "employment_type", "job_status"],
    "loan_purpose": ["loan_purpose", "purpose", "reason_for_loan"],
    "document_date": ["document_date", "issue_date", "statement_date", "pay_period_end", "date"],
}

# Which document categories are allowed to provide each field, in priority order.
FIELD_SOURCE_PRIORITY: dict[str, list[str]] = {
    "applicant_name": ["loan_application", "identification", "payslip", "bank_statement"],
    "identification_number": ["identification", "loan_application"],
    "date_of_birth": ["identification", "loan_application"],
    "current_address": ["loan_application", "proof_of_address", "identification", "bank_statement"],
    "employer": ["loan_application", "payslip"],
    "declared_monthly_income": ["loan_application"],
    "verified_monthly_income": ["payslip", "bank_statement"],
    "requested_loan_amount": ["loan_application"],
    "employment_status": ["loan_application", "payslip"],
    "loan_purpose": ["loan_application"],
}


@dataclass(frozen=True)
class FieldHit:
    """One extracted value together with where it came from."""

    field: str
    value: Any
    document_id: str
    document_type: str
    source_field: str
    confidence: float | None = None

    def reference(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "document_id": self.document_id,
            "document_type": self.document_type,
            "source_field": self.source_field,
            "confidence": self.confidence,
        }


def canonical_document_type(raw: Any) -> str:
    key = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in DOCUMENT_TYPE_ALIASES:
        return DOCUMENT_TYPE_ALIASES[key]
    for alias, canonical in DOCUMENT_TYPE_ALIASES.items():
        if alias in key:
            return canonical
    return key or "unknown"


def packet_id(packet: dict[str, Any]) -> str:
    for key in ("packet_id", "packetId", "id", "item_id"):
        value = packet.get(key)
        if value:
            return str(value)
    metadata = packet.get("metadata") or {}
    user_meta = metadata.get("user") or {}
    for key in ("packet_id", "packetId"):
        if user_meta.get(key):
            return str(user_meta[key])
    return "unknown-packet"


def documents(packet: dict[str, Any]) -> list[dict[str, Any]]:
    raw_docs = packet.get("documents") or packet.get("items") or []
    result: list[dict[str, Any]] = []
    for index, doc in enumerate(raw_docs):
        if not isinstance(doc, dict):
            continue
        enriched = dict(doc)
        enriched["document_type"] = canonical_document_type(
            doc.get("document_type") or doc.get("type") or doc.get("category")
        )
        enriched["document_id"] = str(
            doc.get("document_id") or doc.get("id") or doc.get("file_name") or f"doc-{index + 1}"
        )
        result.append(enriched)
    return result


def is_readable(document: dict[str, Any]) -> bool:
    if document.get("readable") is False:
        return False
    status = str(document.get("status") or document.get("ocr_status") or "").lower()
    return status not in {"unreadable", "failed", "error", "corrupt"}


def raw_value(entry: Any) -> Any:
    if isinstance(entry, dict):
        for key in ("value", "text", "content"):
            if key in entry:
                return entry[key]
        return None
    return entry


def raw_confidence(entry: Any) -> float | None:
    if isinstance(entry, dict):
        confidence = entry.get("confidence")
        if isinstance(confidence, (int, float)):
            return float(confidence)
    return None


def document_fields(document: dict[str, Any]) -> dict[str, Any]:
    fields = document.get("fields")
    if isinstance(fields, dict):
        return fields
    extracted = document.get("extracted_fields")
    return extracted if isinstance(extracted, dict) else {}


def find_field(
    packet: dict[str, Any],
    field: str,
    document_types: Iterable[str] | None = None,
    include_unreadable: bool = False,
) -> list[FieldHit]:
    """All values found for ``field``, ordered by source-document priority."""
    aliases = FIELD_ALIASES.get(field, [field])
    allowed = list(document_types) if document_types else FIELD_SOURCE_PRIORITY.get(field, list(DOCUMENT_CATEGORIES))
    hits: list[FieldHit] = []
    for doc in documents(packet):
        if doc["document_type"] not in allowed:
            continue
        if not include_unreadable and not is_readable(doc):
            continue
        fields = document_fields(doc)
        for alias in aliases:
            if alias not in fields:
                continue
            value = raw_value(fields[alias])
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            hits.append(
                FieldHit(
                    field=field,
                    value=value,
                    document_id=doc["document_id"],
                    document_type=doc["document_type"],
                    source_field=alias,
                    confidence=raw_confidence(fields[alias]),
                )
            )
            break
    order = {doc_type: i for i, doc_type in enumerate(allowed)}
    hits.sort(key=lambda h: order.get(h.document_type, len(order)))
    return hits


def first_field(packet: dict[str, Any], field: str, **kwargs: Any) -> FieldHit | None:
    hits = find_field(packet, field, **kwargs)
    return hits[0] if hits else None


def bank_transactions(packet: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten all transactions found in bank-statement documents."""
    transactions: list[dict[str, Any]] = []
    for doc in documents(packet):
        if doc["document_type"] != "bank_statement":
            continue
        raw = doc.get("transactions") or document_fields(doc).get("transactions") or []
        if isinstance(raw, dict):
            raw = raw.get("value") or []
        for entry in raw:
            if isinstance(entry, dict):
                enriched = dict(entry)
                enriched.setdefault("document_id", doc["document_id"])
                transactions.append(enriched)
    return transactions


def monthly_summaries(packet: dict[str, Any]) -> list[dict[str, Any]]:
    """Pre-aggregated monthly figures, when the extractor provides them."""
    summaries: list[dict[str, Any]] = []
    for doc in documents(packet):
        if doc["document_type"] != "bank_statement":
            continue
        raw = doc.get("monthly_summary") or document_fields(doc).get("monthly_summary") or []
        if isinstance(raw, dict):
            raw = raw.get("value") or []
        for entry in raw:
            if isinstance(entry, dict):
                enriched = dict(entry)
                enriched.setdefault("document_id", doc["document_id"])
                summaries.append(enriched)
    return summaries
