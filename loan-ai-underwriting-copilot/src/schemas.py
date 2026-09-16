"""JSON schemas for every node output plus a dependency-light validator.

``jsonschema`` is used when it is installed (it is listed in requirements.txt and
is present in the Dataloop runtime image); otherwise a small built-in validator
covering the subset of JSON Schema used here takes over, so validation never
silently degrades to "accept anything".
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - exercised implicitly by whichever branch is installed
    import jsonschema  # type: ignore

    _HAS_JSONSCHEMA = True
except ImportError:  # pragma: no cover
    jsonschema = None  # type: ignore
    _HAS_JSONSCHEMA = False


DOCUMENT_CATEGORIES: tuple[str, ...] = (
    "loan_application",
    "identification",
    "payslip",
    "bank_statement",
    "proof_of_address",
)

REQUIRED_DOCUMENTS: tuple[str, ...] = (
    "loan_application",
    "identification",
    "payslip",
    "bank_statement",
)

CONDITIONALLY_REQUIRED_DOCUMENTS: tuple[str, ...] = ("proof_of_address",)

REQUIRED_FIELDS: tuple[str, ...] = (
    "applicant_name",
    "identification_number",
    "date_of_birth",
    "current_address",
    "employer",
    "declared_monthly_income",
    "verified_monthly_income",
    "requested_loan_amount",
    "employment_status",
)

SEVERITIES = ("LOW", "MEDIUM", "HIGH")
CHECK_STATUSES = ("MATCH", "MISMATCH", "INSUFFICIENT_DATA")
RISK_LEVELS = ("LOW", "MEDIUM", "HIGH", "UNKNOWN")
RECOMMENDATIONS = ("READY_FOR_UNDERWRITER", "MANUAL_REVIEW", "INSUFFICIENT_DATA")
ROUTES = ("READY_FOR_UNDERWRITER", "HUMAN_REVIEW")

DISCLAIMER = "AI-generated analysis for human review; not a final lending decision."


COMPLETENESS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "packet_id",
        "complete",
        "present_documents",
        "missing_documents",
        "missing_fields",
        "unreadable_documents",
        "completeness_score",
    ],
    "properties": {
        "packet_id": {"type": "string"},
        "complete": {"type": "boolean"},
        "present_documents": {"type": "array", "items": {"type": "string"}},
        "missing_documents": {"type": "array", "items": {"type": "string"}},
        "missing_fields": {"type": "array", "items": {"type": "string"}},
        "unreadable_documents": {"type": "array", "items": {"type": "string"}},
        "completeness_score": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

BORROWER_CONTEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["packet_id", "borrower", "banking_summary", "source_references"],
    "properties": {
        "packet_id": {"type": "string"},
        "borrower": {
            "type": "object",
            "required": [
                "full_name",
                "date_of_birth",
                "identification_number",
                "declared_address",
                "document_address",
                "employer",
                "employment_status",
                "declared_monthly_income",
                "verified_monthly_income",
                "requested_loan_amount",
            ],
        },
        "banking_summary": {
            "type": "object",
            "required": [
                "average_monthly_salary_credit",
                "salary_credit_variability",
                "average_monthly_outflow",
                "negative_balance_events",
                "returned_payment_events",
            ],
        },
        "source_references": {"type": "array"},
    },
}

CROSS_DOCUMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["packet_id", "consistency_score", "checks"],
    "properties": {
        "packet_id": {"type": "string"},
        "consistency_score": {"type": "number", "minimum": 0, "maximum": 1},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "check_id",
                    "field",
                    "status",
                    "severity",
                    "observed_values",
                    "evidence",
                    "explanation",
                ],
                "properties": {
                    "check_id": {"type": "string"},
                    "field": {"type": "string"},
                    "status": {"type": "string", "enum": list(CHECK_STATUSES)},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "observed_values": {"type": "array"},
                    "evidence": {"type": "array"},
                    "explanation": {"type": "string"},
                },
            },
        },
    },
}

FINANCIAL_RISK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "packet_id",
        "risk_level",
        "risk_score",
        "risk_flags",
        "positive_indicators",
        "insufficient_data",
        "calculation_details",
    ],
    "properties": {
        "packet_id": {"type": "string"},
        "risk_level": {"type": "string", "enum": list(RISK_LEVELS)},
        "risk_score": {"type": "number", "minimum": 0, "maximum": 100},
        "risk_flags": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["code", "severity", "description", "evidence"],
                "properties": {
                    "code": {"type": "string"},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "description": {"type": "string"},
                    "evidence": {"type": "array"},
                },
            },
        },
        "positive_indicators": {"type": "array"},
        "insufficient_data": {"type": "array"},
        "calculation_details": {"type": "object"},
    },
}

UNDERWRITING_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "packet_id",
        "recommendation",
        "confidence",
        "executive_summary",
        "key_findings",
        "risk_flags",
        "missing_information",
        "required_actions",
        "evidence_references",
        "model",
        "generated_at",
        "disclaimer",
    ],
    "properties": {
        "packet_id": {"type": "string"},
        "recommendation": {"type": "string", "enum": list(RECOMMENDATIONS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "executive_summary": {"type": "string"},
        "key_findings": {"type": "array"},
        "risk_flags": {"type": "array"},
        "missing_information": {"type": "array"},
        "required_actions": {"type": "array"},
        "evidence_references": {"type": "array"},
        "model": {"type": "string"},
        "generated_at": {"type": "string"},
        "disclaimer": {"type": "string"},
    },
}

# What the LLM itself must return. Bookkeeping fields (model, generated_at,
# disclaimer) are attached by our code, never trusted from the model.
LLM_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "recommendation",
        "confidence",
        "executive_summary",
        "key_findings",
        "risk_flags",
        "missing_information",
        "required_actions",
        "evidence_references",
    ],
    "properties": {
        "packet_id": {"type": "string"},
        "recommendation": {"type": "string", "enum": list(RECOMMENDATIONS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "executive_summary": {"type": "string"},
        "key_findings": {"type": "array"},
        "risk_flags": {"type": "array"},
        "missing_information": {"type": "array"},
        "required_actions": {"type": "array"},
        "evidence_references": {"type": "array"},
    },
}

ROUTING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["packet_id", "route", "reasons", "thresholds", "decided_at"],
    "properties": {
        "packet_id": {"type": "string"},
        "route": {"type": "string", "enum": list(ROUTES)},
        "reasons": {"type": "array", "items": {"type": "object"}},
        "thresholds": {"type": "object"},
        "decided_at": {"type": "string"},
    },
}


class SchemaValidationError(ValueError):
    """Raised when a payload does not satisfy its schema."""


_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "number": (int, float),
    "integer": int,
}


def _validate_builtin(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errors: list[str] = []
    expected = schema.get("type")
    if expected:
        python_type = _TYPE_MAP[expected]
        if expected in {"number", "integer"} and isinstance(instance, bool):
            return [f"{path}: expected {expected}, got boolean"]
        if not isinstance(instance, python_type):
            return [f"{path}: expected {expected}, got {type(instance).__name__}"]
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} < minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: {instance} > maximum {schema['maximum']}")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required property '{key}'")
        for key, subschema in schema.get("properties", {}).items():
            if key in instance:
                errors.extend(_validate_builtin(instance[key], subschema, f"{path}.{key}"))
    if isinstance(instance, list) and "items" in schema:
        for index, item in enumerate(instance):
            errors.extend(_validate_builtin(item, schema["items"], f"{path}[{index}]"))
    return errors


def validate(instance: Any, schema: dict[str, Any], name: str = "payload") -> Any:
    """Validate ``instance`` against ``schema``; raise :class:`SchemaValidationError`."""
    if _HAS_JSONSCHEMA:
        validator = jsonschema.Draft7Validator(schema)  # type: ignore[union-attr]
        errors = [
            f"${''.join(f'[{p!r}]' for p in e.absolute_path)}: {e.message}"
            for e in sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
        ]
    else:  # pragma: no cover - only when jsonschema is absent
        errors = _validate_builtin(instance, schema)
    if errors:
        raise SchemaValidationError(f"{name} failed schema validation: " + "; ".join(errors[:10]))
    return instance


def is_valid(instance: Any, schema: dict[str, Any]) -> bool:
    try:
        validate(instance, schema)
    except SchemaValidationError:
        return False
    return True
