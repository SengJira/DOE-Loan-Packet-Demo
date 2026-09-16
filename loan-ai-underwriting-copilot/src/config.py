"""Central configuration for the AI Loan Underwriting Copilot.

Every value can be overridden through environment variables or through the
``config`` block of a Dataloop service / pipeline node, so no threshold or model
name is baked into the code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"

# Verified against GET https://integrate.api.nvidia.com/v1/models (2026-09-16).
# ``meta/llama-3.3-70b-instruct`` was NOT listed by the endpoint, so the highest
# preference that is actually available is used as the default.
PRIMARY_MODEL = "meta/llama-3.3-70b-instruct"
DEFAULT_MODEL = "nvidia/llama-3.1-nemotron-70b-instruct"
MODEL_PREFERENCE_ORDER: list[str] = [
    PRIMARY_MODEL,
    "nvidia/llama-3.1-nemotron-70b-instruct",
    "nvidia/llama-3.1-nemotron-51b-instruct",
    "meta/llama-3.1-70b-instruct",
]
DEFAULT_VISION_MODEL = "meta/llama-3.2-90b-vision-instruct"


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    """Runtime configuration. Immutable; use :meth:`merged` to override."""

    # --- model / API -----------------------------------------------------
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    model_preference_order: list[str] = field(default_factory=lambda: list(MODEL_PREFERENCE_ORDER))
    temperature: float = 0.1
    top_p: float = 0.9
    max_output_tokens: int = 3000
    request_timeout_seconds: int = 120
    max_retries: int = 3
    retry_backoff_seconds: float = 2.0

    # --- optional vision fallback (disabled by default) ------------------
    enable_vision_fallback: bool = False
    vision_model: str = DEFAULT_VISION_MODEL

    # --- routing thresholds ----------------------------------------------
    ai_confidence_threshold: float = 0.85
    packet_completeness_threshold: float = 0.90

    # --- risk tuning -------------------------------------------------------
    income_variance_warning_pct: float = 10.0
    income_variance_high_pct: float = 20.0
    salary_variability_warning_pct: float = 15.0
    max_loan_to_annual_income_ratio: float = 5.0

    # --- Dataloop ----------------------------------------------------------
    project_id: str = "37b46e9c-3d18-475d-b624-254d6e5eefd3"
    source_dataset: str = "loan-structured-output"
    ground_truth_dataset: str = "loan-ground-truth"
    ground_truth_folder: str = "/ai-underwriting-copilot-v1"
    pipeline_name: str = "loan-ai-underwriting-copilot-v1"
    human_review_task_name: str = "loan-ai-underwriting-copilot-v1-human-review"

    # --- misc ---------------------------------------------------------------
    mask_identification_numbers: bool = True

    @property
    def api_key(self) -> str:
        """Read the API key lazily so it is never stored or serialised."""
        key = os.environ.get("NVIDIA_API_KEY", "").strip()
        if not key:
            raise RuntimeError("NVIDIA_API_KEY is not set in the environment")
        return key

    def has_api_key(self) -> bool:
        return bool(os.environ.get("NVIDIA_API_KEY", "").strip())

    def merged(self, overrides: dict[str, Any] | None) -> Config:
        """Return a copy with any recognised keys from ``overrides`` applied."""
        if not overrides:
            return self
        known = {f.name for f in self.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        clean = {k: v for k, v in overrides.items() if k in known and v is not None}
        return replace(self, **clean) if clean else self


def load_config(overrides: dict[str, Any] | None = None) -> Config:
    """Build a :class:`Config` from environment variables plus node overrides."""
    cfg = Config(
        base_url=_env_str("NVIDIA_BASE_URL", DEFAULT_BASE_URL),
        model=_env_str("NVIDIA_MODEL", DEFAULT_MODEL),
        temperature=_env_float("NVIDIA_TEMPERATURE", 0.1),
        top_p=_env_float("NVIDIA_TOP_P", 0.9),
        max_output_tokens=_env_int("NVIDIA_MAX_OUTPUT_TOKENS", 3000),
        request_timeout_seconds=_env_int("NVIDIA_REQUEST_TIMEOUT", 120),
        max_retries=_env_int("NVIDIA_MAX_RETRIES", 3),
        retry_backoff_seconds=_env_float("NVIDIA_RETRY_BACKOFF_SECONDS", 2.0),
        enable_vision_fallback=_env_bool("ENABLE_VISION_FALLBACK", False),
        vision_model=_env_str("NVIDIA_VISION_MODEL", DEFAULT_VISION_MODEL),
        ai_confidence_threshold=_env_float("AI_CONFIDENCE_THRESHOLD", 0.85),
        packet_completeness_threshold=_env_float("PACKET_COMPLETENESS_THRESHOLD", 0.90),
        project_id=_env_str("DATALOOP_PROJECT_ID", Config.project_id),
        source_dataset=_env_str("SOURCE_DATASET", Config.source_dataset),
        ground_truth_dataset=_env_str("GROUND_TRUTH_DATASET", Config.ground_truth_dataset),
        ground_truth_folder=_env_str("GROUND_TRUTH_FOLDER", Config.ground_truth_folder),
        pipeline_name=_env_str("PIPELINE_NAME", Config.pipeline_name),
        human_review_task_name=_env_str("HUMAN_REVIEW_TASK_NAME", Config.human_review_task_name),
    )
    return cfg.merged(overrides)
