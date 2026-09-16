"""Node 1 - validate_packet_completeness.

Purely deterministic: it reports what is present, what is missing and how
complete the packet is. It never invents a value that the extractor did not
produce.
"""

from __future__ import annotations

from typing import Any

from . import extraction
from .schemas import (
    COMPLETENESS_SCHEMA,
    CONDITIONALLY_REQUIRED_DOCUMENTS,
    REQUIRED_DOCUMENTS,
    REQUIRED_FIELDS,
    validate,
)

DOCUMENT_WEIGHT = 0.5
FIELD_WEIGHT = 0.5


def proof_of_address_required(packet: dict[str, Any]) -> bool:
    """Conditional requirement; opt out explicitly per packet when not needed."""
    requirements = packet.get("requirements")
    if isinstance(requirements, dict) and "proof_of_address_required" in requirements:
        return bool(requirements["proof_of_address_required"])
    return True


def required_documents_for(packet: dict[str, Any]) -> list[str]:
    required = list(REQUIRED_DOCUMENTS)
    if proof_of_address_required(packet):
        required.extend(CONDITIONALLY_REQUIRED_DOCUMENTS)
    return required


def validate_packet_completeness(packet: dict[str, Any]) -> dict[str, Any]:
    pid = extraction.packet_id(packet)
    docs = extraction.documents(packet)

    readable_types: list[str] = []
    unreadable_documents: list[str] = []
    for doc in docs:
        if extraction.is_readable(doc):
            if doc["document_type"] not in readable_types:
                readable_types.append(doc["document_type"])
        else:
            unreadable_documents.append(doc["document_id"])

    required = required_documents_for(packet)
    present_documents = [t for t in readable_types if t != "unknown"]
    missing_documents = [t for t in required if t not in readable_types]

    missing_fields = [f for f in REQUIRED_FIELDS if not extraction.find_field(packet, f)]

    document_ratio = (len(required) - len(missing_documents)) / len(required) if required else 1.0
    field_ratio = (len(REQUIRED_FIELDS) - len(missing_fields)) / len(REQUIRED_FIELDS)
    score = DOCUMENT_WEIGHT * document_ratio + FIELD_WEIGHT * field_ratio

    result = {
        "packet_id": pid,
        "complete": not missing_documents and not missing_fields and not unreadable_documents,
        "present_documents": present_documents,
        "missing_documents": missing_documents,
        "missing_fields": missing_fields,
        "unreadable_documents": unreadable_documents,
        "completeness_score": round(max(0.0, min(1.0, score)), 4),
    }
    return validate(result, COMPLETENESS_SCHEMA, "completeness")
