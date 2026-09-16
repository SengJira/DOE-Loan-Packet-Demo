"""Pure-Python orchestration of the six nodes.

Used by the unit tests, the local demo and by the Dataloop service wrapper in
``dataloop_functions.py``. It has no Dataloop dependency so the whole analysis
can be exercised offline.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .borrower_context import build_borrower_context
from .completeness import validate_packet_completeness
from .config import Config, load_config
from .cross_document import cross_document_reasoning
from .financial_risk import assess_financial_risk
from .logging_utils import node_span
from .nvidia_client import NvidiaClient
from .routing import route_by_confidence_and_risk
from .underwriting_summary import generate_underwriting_summary

ANALYSIS_VERSION = "1.0.0"


def packet_fingerprint(packet: dict[str, Any]) -> str:
    """Stable hash of the input, used to make reruns idempotent."""
    payload = json.dumps(packet, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def analyze_packet(
    packet: dict[str, Any],
    config: Config | None = None,
    client: NvidiaClient | None = None,
    cycle_id: str | None = None,
) -> dict[str, Any]:
    """Run nodes 1-6 and return every intermediate result."""
    cfg = config or load_config()
    pid = packet.get("packet_id") or "unknown-packet"

    with node_span("validate_packet_completeness", packet_id=pid, cycle_id=cycle_id):
        completeness = validate_packet_completeness(packet)
    with node_span("build_borrower_context", packet_id=pid, cycle_id=cycle_id):
        borrower = build_borrower_context(packet)
    with node_span("cross_document_reasoning", packet_id=pid, cycle_id=cycle_id):
        cross = cross_document_reasoning(packet, borrower)
    with node_span("assess_financial_risk", packet_id=pid, cycle_id=cycle_id):
        risk = assess_financial_risk(packet, borrower, cross, completeness, config=cfg)

    summary = generate_underwriting_summary(
        completeness, borrower, cross, risk, config=cfg, client=client, cycle_id=cycle_id
    )

    with node_span("route_by_confidence_and_risk", packet_id=pid, cycle_id=cycle_id):
        routing = route_by_confidence_and_risk(completeness, cross, risk, summary, config=cfg)

    return {
        "packet_id": completeness["packet_id"],
        "analysis_version": ANALYSIS_VERSION,
        "input_fingerprint": packet_fingerprint(packet),
        "model": summary.get("model"),
        "completeness": completeness,
        "borrower_context": borrower,
        "cross_document": cross,
        "financial_risk": risk,
        "underwriting_summary": summary,
        "routing": routing,
    }
