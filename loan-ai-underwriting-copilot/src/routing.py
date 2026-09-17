"""Node 6 - route_by_confidence_and_risk.

Deterministic. The LLM never decides the route; it only supplies a confidence
value that these rules consume. Both routes remain recommendations - the final
lending decision always belongs to a human underwriter.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from config import Config, load_config
from cross_document import has_high_severity_mismatch, identity_uncertain
from schemas import ROUTING_SCHEMA, validate

HUMAN_REVIEW = "HUMAN_REVIEW"
READY_FOR_UNDERWRITER = "READY_FOR_UNDERWRITER"


def _reason(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


def route_by_confidence_and_risk(
    completeness: dict[str, Any],
    cross_document: dict[str, Any],
    risk: dict[str, Any],
    summary: dict[str, Any],
    config: Config | None = None,
) -> dict[str, Any]:
    cfg = config or load_config()
    reasons: list[dict[str, str]] = []

    confidence = float(summary.get("confidence") or 0.0)
    completeness_score = float(completeness.get("completeness_score") or 0.0)

    if not summary.get("model_output_valid", True):
        reasons.append(
            _reason("MODEL_OUTPUT_INVALID", summary.get("error", "model output was not usable"))
        )
    if summary.get("recommendation") == "INSUFFICIENT_DATA":
        reasons.append(_reason("AI_INSUFFICIENT_DATA", "AI reported insufficient data"))
    if confidence < cfg.ai_confidence_threshold:
        reasons.append(
            _reason(
                "LOW_AI_CONFIDENCE",
                f"confidence {confidence:.2f} is below threshold {cfg.ai_confidence_threshold:.2f}",
            )
        )
    if completeness_score < cfg.packet_completeness_threshold:
        reasons.append(
            _reason(
                "LOW_COMPLETENESS",
                f"completeness {completeness_score:.2f} is below threshold "
                f"{cfg.packet_completeness_threshold:.2f}",
            )
        )
    if risk.get("risk_level") in {"HIGH", "UNKNOWN"}:
        reasons.append(_reason("RISK_LEVEL", f"risk level is {risk.get('risk_level')}"))
    if has_high_severity_mismatch(cross_document):
        reasons.append(_reason("HIGH_SEVERITY_MISMATCH", "a high-severity cross-document mismatch exists"))
    for missing in completeness.get("missing_documents", []):
        reasons.append(_reason("MISSING_DOCUMENT", f"required document '{missing}' is missing"))
    for missing in completeness.get("missing_fields", []):
        reasons.append(_reason("MISSING_FIELD", f"required field '{missing}' is missing"))
    for unreadable in completeness.get("unreadable_documents", []):
        reasons.append(_reason("UNREADABLE_DOCUMENT", f"document '{unreadable}' is unreadable"))
    if identity_uncertain(cross_document):
        reasons.append(_reason("IDENTITY_UNCERTAIN", "identity fields could not be confirmed across documents"))
    if risk.get("insufficient_data"):
        reasons.append(
            _reason(
                "INSUFFICIENT_EVIDENCE",
                "missing evidence for: " + ", ".join(risk["insufficient_data"][:5]),
            )
        )

    route = HUMAN_REVIEW if reasons else READY_FOR_UNDERWRITER
    result = {
        "packet_id": summary.get("packet_id") or completeness.get("packet_id"),
        "route": route,
        "reasons": reasons,
        "thresholds": {
            "AI_CONFIDENCE_THRESHOLD": cfg.ai_confidence_threshold,
            "PACKET_COMPLETENESS_THRESHOLD": cfg.packet_completeness_threshold,
        },
        "decided_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "final_decision_owner": "human_underwriter",
        "ai_confidence": confidence,
        "completeness_score": completeness_score,
        "risk_level": risk.get("risk_level"),
    }
    return validate(result, ROUTING_SCHEMA, "routing")
