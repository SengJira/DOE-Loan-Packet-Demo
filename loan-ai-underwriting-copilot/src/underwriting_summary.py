"""Node 5 - generate_underwriting_summary.

Builds a minimised, identifier-masked payload, asks the selected NVIDIA-hosted
model for a strictly-JSON brief, validates it and fails safe: any API or schema
failure produces an INSUFFICIENT_DATA summary with confidence 0.0, which the
router always sends to a human.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .logging_utils import log_event, mask_identifier, node_span
from .nvidia_client import ModelResponseError, NvidiaApiError, NvidiaClient
from .schemas import (
    DISCLAIMER,
    LLM_SUMMARY_SCHEMA,
    UNDERWRITING_SUMMARY_SCHEMA,
    validate,
)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
NODE_NAME = "generate_underwriting_summary"


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def minimize_context(
    completeness: dict[str, Any],
    borrower_context: dict[str, Any],
    cross_document: dict[str, Any],
    risk: dict[str, Any],
    mask_ids: bool = True,
) -> dict[str, dict[str, Any]]:
    """Strip raw documents and mask identifiers before anything leaves the VPC."""
    borrower = dict(borrower_context.get("borrower", {}))
    if mask_ids:
        borrower["identification_number"] = mask_identifier(borrower.get("identification_number"))
    minimal_borrower = {
        "borrower": borrower,
        "banking_summary": borrower_context.get("banking_summary", {}),
        "source_references": borrower_context.get("source_references", [])[:40],
    }
    minimal_checks = [
        {
            "check_id": check["check_id"],
            "field": check["field"],
            "status": check["status"],
            "severity": check["severity"],
            "explanation": check["explanation"],
            "evidence": [
                {k: v for k, v in item.items() if k in {"document_id", "document_type", "field"}}
                for item in check.get("evidence", [])
            ],
        }
        for check in cross_document.get("checks", [])
        if check["status"] != "MATCH" or check["severity"] == "HIGH"
    ]
    return {
        "completeness": {
            k: v for k, v in completeness.items() if k != "packet_id"
        },
        "borrower": minimal_borrower,
        "cross_document": {
            "consistency_score": cross_document.get("consistency_score"),
            "checks": minimal_checks,
        },
        "risk": {
            "risk_level": risk.get("risk_level"),
            "risk_score": risk.get("risk_score"),
            "risk_flags": risk.get("risk_flags", []),
            "positive_indicators": risk.get("positive_indicators", []),
            "insufficient_data": risk.get("insufficient_data", []),
        },
    }


def build_user_prompt(packet_id: str, minimized: dict[str, Any]) -> str:
    template = load_prompt("underwriting_user_template.txt")
    dumps = lambda value: json.dumps(value, indent=2, ensure_ascii=False, default=str)  # noqa: E731
    return template.format(
        packet_id=packet_id,
        completeness_json=dumps(minimized["completeness"]),
        borrower_json=dumps(minimized["borrower"]),
        cross_document_json=dumps(minimized["cross_document"]),
        risk_json=dumps(minimized["risk"]),
    )


def fallback_summary(packet_id: str, model: str, reason: str, missing: list[str] | None = None) -> dict[str, Any]:
    """Safe output used whenever the model cannot be trusted or reached."""
    summary = {
        "packet_id": packet_id,
        "recommendation": "INSUFFICIENT_DATA",
        "confidence": 0.0,
        "executive_summary": (
            "The AI summary could not be produced, so no AI assessment is available for this packet. "
            "A human underwriter must review the extracted data and documents directly."
        ),
        "key_findings": [],
        "risk_flags": [],
        "missing_information": missing or ["ai_summary_unavailable"],
        "required_actions": ["Review the packet manually; the AI stage did not return a usable result."],
        "evidence_references": [],
        "model": model,
        "generated_at": _utc_now(),
        "disclaimer": DISCLAIMER,
        "error": reason,
        "model_output_valid": False,
    }
    return validate(summary, UNDERWRITING_SUMMARY_SCHEMA, "underwriting_summary")


def generate_underwriting_summary(
    completeness: dict[str, Any],
    borrower_context: dict[str, Any],
    cross_document: dict[str, Any],
    risk: dict[str, Any],
    config: Config | None = None,
    client: NvidiaClient | None = None,
    cycle_id: str | None = None,
) -> dict[str, Any]:
    cfg = config or load_config()
    packet_id = completeness.get("packet_id") or borrower_context.get("packet_id") or "unknown-packet"
    model = cfg.model

    with node_span(NODE_NAME, packet_id=packet_id, cycle_id=cycle_id, model=model) as span:
        if client is None and not cfg.has_api_key():
            span["status"] = "FAILED_SAFE"
            return fallback_summary(packet_id, model, "NVIDIA_API_KEY is not configured")

        minimized = minimize_context(
            completeness, borrower_context, cross_document, risk, mask_ids=cfg.mask_identification_numbers
        )
        llm = client or NvidiaClient(cfg)
        try:
            raw = llm.chat_json(
                system_prompt=load_prompt("underwriting_system.txt"),
                user_prompt=build_user_prompt(packet_id, minimized),
                schema=LLM_SUMMARY_SCHEMA,
                model=model,
                packet_id=packet_id,
                cycle_id=cycle_id,
            )
        except (NvidiaApiError, ModelResponseError) as exc:
            span["status"] = "FAILED_SAFE"
            log_event(
                "summary.failed_safe",
                node=NODE_NAME,
                packet_id=packet_id,
                pipeline_cycle_id=cycle_id,
                model=model,
                error_type=type(exc).__name__,
                status="FAILED_SAFE",
            )
            return fallback_summary(packet_id, model, f"{type(exc).__name__}: {exc}")

        summary = {
            "packet_id": packet_id,
            "recommendation": raw["recommendation"],
            "confidence": float(raw["confidence"]),
            "executive_summary": str(raw["executive_summary"]),
            "key_findings": list(raw.get("key_findings", [])),
            "risk_flags": list(raw.get("risk_flags", [])),
            "missing_information": list(raw.get("missing_information", [])),
            "required_actions": list(raw.get("required_actions", [])),
            "evidence_references": list(raw.get("evidence_references", [])),
            "model": model,
            "generated_at": _utc_now(),
            "disclaimer": DISCLAIMER,
            "model_output_valid": True,
        }
        span["status"] = "OK"
        return validate(summary, UNDERWRITING_SUMMARY_SCHEMA, "underwriting_summary")
