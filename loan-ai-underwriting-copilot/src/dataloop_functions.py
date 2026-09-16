"""Dataloop service runner exposing each node as a pipeline function.

Every function takes the ``loan-structured-output`` item, reads the structured
JSON, writes its result into ``item.metadata['user']['aiUnderwriting']`` and
returns the same item, so nodes chain without duplicating payloads.

Idempotency: results are keyed by ``analysis_version`` plus a fingerprint of the
input JSON. Re-running a node on unchanged input reuses the stored result
instead of producing a second output or a second review task.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

try:  # dtlpy exists in the Dataloop runtime; keep imports optional for local tests
    import dtlpy as dl

    _BASE = dl.BaseServiceRunner
except Exception:  # pragma: no cover - local/offline execution
    dl = None  # type: ignore

    class _BASE:  # type: ignore
        pass


from .borrower_context import build_borrower_context
from .completeness import validate_packet_completeness
from .config import load_config
from .cross_document import cross_document_reasoning
from .financial_risk import assess_financial_risk
from .logging_utils import log_event, node_span
from .pipeline_runner import ANALYSIS_VERSION, packet_fingerprint
from .routing import HUMAN_REVIEW, READY_FOR_UNDERWRITER, route_by_confidence_and_risk
from .underwriting_summary import generate_underwriting_summary

METADATA_ROOT = "aiUnderwriting"


def _flat_items(collection: Any):
    """Yield Item entities across dtlpy list/PagedEntities differences."""
    for element in collection:
        if isinstance(element, list):
            yield from element
        else:
            yield element


def _document_entry(it: Any) -> dict[str, Any]:
    user = (it.metadata or {}).get("user") or {}
    return {
        "document_id": getattr(it, "id", None) or it.name,
        "document_type": user.get("doc_type") or "unknown",
        "file_name": it.name,
        "readable": True,
        "fields": user.get("extraction") or user.get("prediction") or {},
        "extraction_confidence": user.get("min_confidence"),
    }


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cycle_id(context: Any) -> str | None:
    for attribute in ("pipeline_execution_id", "execution_id"):
        value = getattr(context, attribute, None)
        if value:
            return str(value)
    return None


class LoanUnderwritingCopilot(_BASE):
    """Service runner for the ``loan-ai-underwriting-copilot-v1`` pipeline."""

    def __init__(self, config_overrides: dict[str, Any] | None = None):
        self.config = load_config(config_overrides)

    # ------------------------------------------------------------------ io
    def load_packet(self, item: Any) -> dict[str, Any]:
        """Read the structured JSON produced by the extraction pipeline.

        Supports two source shapes:
        * a packet-level JSON item (whole loan packet in one file), and
        * document-level items (one file per document) sharing a
          ``metadata.user.loan_id`` — the packet is assembled from all
          sibling items in the same dataset.
        """
        stored = ((item.metadata or {}).get("user") or {}).get("structuredOutput")
        if isinstance(stored, dict):
            return stored
        if str(getattr(item, "mimetype", "")).endswith("json"):
            with tempfile.TemporaryDirectory() as tmp:
                path = item.download(local_path=tmp)
                if isinstance(path, list):
                    path = path[0]
                with open(path, encoding="utf-8") as handle:
                    return json.load(handle)
        return self._assemble_packet(item)

    def _assemble_packet(self, item: Any) -> dict[str, Any]:
        """Build one packet dict from every item sharing the item's loan_id."""
        cached = self._state(item).get("packet_context")
        if isinstance(cached, dict) and cached.get("documents"):
            return cached
        user = (item.metadata or {}).get("user") or {}
        loan_id = user.get("loan_id") or str(item.name).split("__")[0]
        dataset = getattr(item, "dataset", None)
        if dataset is None and dl is not None and getattr(item, "dataset_id", None):
            try:
                dataset = dl.datasets.get(dataset_id=item.dataset_id)
            except Exception:
                dataset = None
        documents = []
        if dataset is not None:
            for sibling in _flat_items(dataset.items.list()):
                suser = (sibling.metadata or {}).get("user") or {}
                if suser.get("loan_id") == loan_id:
                    documents.append(_document_entry(sibling))
        if not documents:
            documents.append(_document_entry(item))
        packet = {
            "packet_id": loan_id or getattr(item, "id", None) or str(item.name),
            "documents": documents,
            "source_dataset": getattr(dataset, "name", None),
            # Every document of the loan triggers the pipeline once, so only the
            # deterministic anchor item runs the analysis; siblings are skipped.
            "_anchor_item_id": min(str(d["document_id"]) for d in documents),
        }
        # Keep the assembled packet inside the (additive) analysis block so the
        # remaining nodes do not re-list the dataset.
        item.metadata = item.metadata or {}
        item.metadata.setdefault("user", {}).setdefault(METADATA_ROOT, {})["packet_context"] = packet
        return packet

    def _state(self, item: Any) -> dict[str, Any]:
        user_metadata = (item.metadata or {}).get("user") or {}
        return user_metadata.get(METADATA_ROOT) or {}

    def _store(self, item: Any, node: str, payload: dict[str, Any], fingerprint: str) -> Any:
        item.metadata = item.metadata or {}
        item.metadata.setdefault("user", {})
        state = item.metadata["user"].setdefault(METADATA_ROOT, {})
        state["analysis_version"] = ANALYSIS_VERSION
        state["input_fingerprint"] = fingerprint
        state["updated_at"] = _now()
        state[node] = payload
        return item.update(system_metadata=False) if hasattr(item, "update") else item

    def _cached(self, item: Any, node: str, fingerprint: str) -> dict[str, Any] | None:
        state = self._state(item)
        if (
            state.get("analysis_version") == ANALYSIS_VERSION
            and state.get("input_fingerprint") == fingerprint
            and isinstance(state.get(node), dict)
        ):
            return state[node]
        return None

    def _run_node(self, item: Any, node: str, compute, context: Any = None) -> Any:
        packet = self.load_packet(item)
        fingerprint = packet_fingerprint(packet)
        pid = packet.get("packet_id")
        anchor = packet.get("_anchor_item_id")
        if anchor and str(getattr(item, "id", "")) != anchor:
            log_event("node.skipped", node=node, packet_id=pid, status="DUPLICATE_PACKET_ITEM")
            return item
        cached = self._cached(item, node, fingerprint)
        if cached is not None:
            log_event("node.cached", node=node, packet_id=pid, status="SKIPPED_IDEMPOTENT")
            return item
        with node_span(node, packet_id=pid, cycle_id=_cycle_id(context)):
            result = compute(packet, self._state(item))
        return self._store(item, node, result, fingerprint)

    # --------------------------------------------------------------- nodes
    def validate_packet_completeness(self, item: Any, context: Any = None) -> Any:
        return self._run_node(
            item, "completeness", lambda packet, _state: validate_packet_completeness(packet), context
        )

    def build_borrower_context(self, item: Any, context: Any = None) -> Any:
        return self._run_node(
            item, "borrower_context", lambda packet, _state: build_borrower_context(packet), context
        )

    def cross_document_reasoning(self, item: Any, context: Any = None) -> Any:
        return self._run_node(
            item,
            "cross_document",
            lambda packet, state: cross_document_reasoning(packet, state.get("borrower_context")),
            context,
        )

    def assess_financial_risk(self, item: Any, context: Any = None) -> Any:
        return self._run_node(
            item,
            "financial_risk",
            lambda packet, state: assess_financial_risk(
                packet,
                state.get("borrower_context") or build_borrower_context(packet),
                state.get("cross_document"),
                state.get("completeness"),
                config=self.config,
            ),
            context,
        )

    def generate_underwriting_summary(self, item: Any, context: Any = None) -> Any:
        cycle = _cycle_id(context)

        def compute(packet: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
            completeness = state.get("completeness") or validate_packet_completeness(packet)
            borrower = state.get("borrower_context") or build_borrower_context(packet)
            cross = state.get("cross_document") or cross_document_reasoning(packet, borrower)
            risk = state.get("financial_risk") or assess_financial_risk(
                packet, borrower, cross, completeness, config=self.config
            )
            return generate_underwriting_summary(
                completeness, borrower, cross, risk, config=self.config, cycle_id=cycle
            )

        return self._run_node(item, "underwriting_summary", compute, context)

    def route_by_confidence_and_risk(self, item: Any, context: Any = None) -> Any:
        packet = self.load_packet(item)
        fingerprint = packet_fingerprint(packet)
        anchor = packet.get("_anchor_item_id")
        if anchor and str(getattr(item, "id", "")) != anchor:
            log_event(
                "node.skipped",
                node="route_by_confidence_and_risk",
                packet_id=packet.get("packet_id"),
                status="DUPLICATE_PACKET_ITEM",
            )
            return item
        state = self._state(item)
        completeness = state.get("completeness") or validate_packet_completeness(packet)
        borrower = state.get("borrower_context") or build_borrower_context(packet)
        cross = state.get("cross_document") or cross_document_reasoning(packet, borrower)
        risk = state.get("financial_risk") or assess_financial_risk(
            packet, borrower, cross, completeness, config=self.config
        )
        summary = state.get("underwriting_summary") or generate_underwriting_summary(
            completeness, borrower, cross, risk, config=self.config, cycle_id=_cycle_id(context)
        )
        with node_span("route_by_confidence_and_risk", packet_id=packet.get("packet_id")):
            routing = route_by_confidence_and_risk(completeness, cross, risk, summary, config=self.config)
        item = self._store(item, "routing", routing, fingerprint)
        # Mirrored at a stable path so pipeline connection filters can branch on it.
        item.metadata["user"][METADATA_ROOT]["route"] = routing["route"]
        item.metadata["user"][METADATA_ROOT]["requires_human_review"] = routing["route"] == HUMAN_REVIEW
        if hasattr(item, "update"):
            item = item.update(system_metadata=False)
        return item

    # ------------------------------------------------- reviewer ground truth
    def capture_reviewer_feedback(self, item: Any, context: Any = None) -> Any:
        """Persist reviewer-approved output as a NEW ground-truth item.

        Existing ground-truth items are never modified: output is written to a
        dedicated folder and a packet that already has a record for this
        analysis version is skipped.
        """
        if dl is None:  # pragma: no cover - offline
            raise RuntimeError("dtlpy is required to write ground truth")
        packet = self.load_packet(item)
        state = self._state(item)
        pid = packet.get("packet_id") or item.name
        review = ((item.metadata or {}).get("user") or {}).get("humanReview") or {}

        record = {
            "packet_id": pid,
            "source_item_id": getattr(item, "id", None),
            "analysis_version": ANALYSIS_VERSION,
            "input_fingerprint": packet_fingerprint(packet),
            "captured_at": _now(),
            "ai": {
                "model": (state.get("underwriting_summary") or {}).get("model"),
                "confidence": (state.get("underwriting_summary") or {}).get("confidence"),
                "recommendation": (state.get("underwriting_summary") or {}).get("recommendation"),
                "route": (state.get("routing") or {}).get("route"),
            },
            "reviewer": {
                "result": review.get("final_review_result"),
                "reason": review.get("decision_reason"),
                "comments": review.get("reviewer_comments"),
                "corrected_fields": review.get("corrected_fields", {}),
                "confirmed_risk_flags": review.get("confirmed_risk_flags", []),
                "dismissed_risk_flags": review.get("dismissed_risk_flags", []),
                "reviewer": review.get("reviewer"),
                "reviewed_at": review.get("reviewed_at") or _now(),
            },
            "analysis": {
                "completeness": state.get("completeness"),
                "cross_document": state.get("cross_document"),
                "financial_risk": state.get("financial_risk"),
                "underwriting_summary": state.get("underwriting_summary"),
            },
            "disclaimer": "Reviewer-confirmed record. Final lending decision belongs to the human underwriter.",
        }

        project = dl.projects.get(project_id=self.config.project_id)
        dataset = project.datasets.get(dataset_name=self.config.ground_truth_dataset)
        folder = self.config.ground_truth_folder.rstrip("/")
        remote_name = f"{pid}__{ANALYSIS_VERSION}.json"
        remote_path = f"{folder}/{remote_name}"

        existing = list(
            dataset.items.list(
                filters=dl.Filters(field="filename", values=remote_path)
            ).all()
        )
        if existing:
            log_event(
                "ground_truth.exists",
                node="capture_reviewer_feedback",
                packet_id=pid,
                status="SKIPPED_IDEMPOTENT",
            )
            return existing[0]

        with tempfile.TemporaryDirectory() as tmp:
            local = os.path.join(tmp, remote_name)
            with open(local, "w", encoding="utf-8") as handle:
                json.dump(record, handle, indent=2, ensure_ascii=False)
            uploaded = dataset.items.upload(
                local_path=local,
                remote_path=folder,
                item_metadata={"user": {"groundTruthSource": "loan-ai-underwriting-copilot-v1"}},
                overwrite=False,
            )
        log_event("ground_truth.written", node="capture_reviewer_feedback", packet_id=pid, status="OK")
        return uploaded


# Convenience aliases used by the pipeline node definitions.
FUNCTION_NAMES = [
    "validate_packet_completeness",
    "build_borrower_context",
    "cross_document_reasoning",
    "assess_financial_risk",
    "generate_underwriting_summary",
    "route_by_confidence_and_risk",
    "capture_reviewer_feedback",
]

ROUTE_VALUES = (READY_FOR_UNDERWRITER, HUMAN_REVIEW)
