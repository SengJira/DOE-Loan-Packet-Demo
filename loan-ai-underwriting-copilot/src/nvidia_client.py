"""Thin OpenAI-compatible client for the NVIDIA hosted inference API.

Responsibilities: model verification, retries with exponential backoff, fenced
JSON extraction and schema validation. The API key is read from the environment
on every call and never logged, stored or returned.
"""

from __future__ import annotations

import json
import random
import re
import time
from typing import Any

import requests

from config import Config, load_config
from logging_utils import log_event
from schemas import SchemaValidationError, validate

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", flags=re.IGNORECASE)
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class NvidiaApiError(RuntimeError):
    """Transport/HTTP level failure that survived all retries."""


class ModelResponseError(ValueError):
    """The model replied, but the reply was not usable JSON for our schema."""


def strip_code_fences(text: str) -> str:
    """Remove Markdown fences and any prose surrounding the JSON object."""
    cleaned = _FENCE.sub("", (text or "").strip())
    if cleaned.startswith("{") and cleaned.endswith("}"):
        return cleaned
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        return cleaned[start : end + 1]
    return cleaned


def parse_json_response(text: str) -> dict[str, Any]:
    cleaned = strip_code_fences(text)
    if not cleaned:
        raise ModelResponseError("model returned an empty response")
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ModelResponseError(f"model response is not valid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ModelResponseError("model response is not a JSON object")
    return parsed


class NvidiaClient:
    def __init__(self, config: Config | None = None, session: requests.Session | None = None):
        self.config = config or load_config()
        self.session = session or requests.Session()

    # -- model discovery ----------------------------------------------------
    def list_models(self) -> list[str]:
        response = self.session.get(
            f"{self.config.base_url}/models",
            headers=self._headers(auth=self.config.has_api_key()),
            timeout=self.config.request_timeout_seconds,
        )
        response.raise_for_status()
        return [m["id"] for m in response.json().get("data", []) if isinstance(m, dict) and "id" in m]

    def resolve_model(self, preferred: list[str] | None = None) -> str:
        """Return the first configured model that is listed AND serves inference.

        The /models catalog lists deprecated models that return 404/410 on
        /chat/completions, so candidates are verified with a one-token probe.
        """
        candidates = list(preferred or [self.config.model, *self.config.model_preference_order])
        try:
            available = set(self.list_models())
        except requests.RequestException as exc:
            log_event("model.list_failed", error_type=type(exc).__name__)
            return candidates[0]
        for candidate in candidates:
            if candidate in available and self._serves_inference(candidate):
                return candidate
        raise NvidiaApiError(
            "none of the configured models are available on the NVIDIA endpoint: " + ", ".join(candidates)
        )

    def _serves_inference(self, model: str) -> bool:
        """True when a minimal chat completion succeeds for ``model``."""
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "OK"}],
            "max_tokens": 1,
            "temperature": 0.0,
            "stream": False,
        }
        try:
            response = self.session.post(
                f"{self.config.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=self.config.request_timeout_seconds,
            )
        except requests.RequestException:
            return False
        if response.status_code == 200:
            return True
        log_event("model.probe_failed", model=model, http_status=response.status_code, status="SKIP")
        return False

    # -- chat ---------------------------------------------------------------
    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any] | None = None,
        model: str | None = None,
        packet_id: str | None = None,
        cycle_id: str | None = None,
    ) -> dict[str, Any]:
        """Run a chat completion and return validated JSON."""
        model_id = model or self.config.model
        payload = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "max_tokens": self.config.max_output_tokens,
            "stream": False,
        }
        content = self._post_with_retries(payload, packet_id=packet_id, cycle_id=cycle_id)
        parsed = parse_json_response(content)
        if schema is not None:
            try:
                validate(parsed, schema, "llm_response")
            except SchemaValidationError as exc:
                raise ModelResponseError(str(exc)) from exc
        return parsed

    # -- internals ----------------------------------------------------------
    def _headers(self, auth: bool = True) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _post_with_retries(
        self,
        payload: dict[str, Any],
        packet_id: str | None = None,
        cycle_id: str | None = None,
    ) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            started = time.time()
            try:
                response = self.session.post(
                    f"{self.config.base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                    timeout=self.config.request_timeout_seconds,
                )
            except requests.RequestException as exc:
                last_error = exc
                log_event(
                    "model.request_exception",
                    node="generate_underwriting_summary",
                    packet_id=packet_id,
                    pipeline_cycle_id=cycle_id,
                    model=payload["model"],
                    attempt=attempt,
                    error_type=type(exc).__name__,
                    status="RETRY" if attempt < self.config.max_retries else "ERROR",
                )
            else:
                duration_ms = int((time.time() - started) * 1000)
                if response.status_code == 200:
                    log_event(
                        "model.response",
                        node="generate_underwriting_summary",
                        packet_id=packet_id,
                        pipeline_cycle_id=cycle_id,
                        model=payload["model"],
                        duration_ms=duration_ms,
                        status="OK",
                        attempt=attempt,
                    )
                    return self._extract_content(response.json())
                last_error = NvidiaApiError(f"HTTP {response.status_code}")
                log_event(
                    "model.http_error",
                    node="generate_underwriting_summary",
                    packet_id=packet_id,
                    pipeline_cycle_id=cycle_id,
                    model=payload["model"],
                    duration_ms=duration_ms,
                    http_status=response.status_code,
                    attempt=attempt,
                    status="RETRY" if response.status_code in RETRYABLE_STATUS else "ERROR",
                )
                if response.status_code not in RETRYABLE_STATUS:
                    raise NvidiaApiError(f"NVIDIA API returned HTTP {response.status_code}")
            if attempt < self.config.max_retries:
                delay = self.config.retry_backoff_seconds * (2 ** (attempt - 1))
                time.sleep(delay + random.uniform(0, 0.25))
        raise NvidiaApiError(f"NVIDIA API request failed after {self.config.max_retries} attempts: {last_error}")

    @staticmethod
    def _extract_content(body: dict[str, Any]) -> str:
        try:
            return body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelResponseError("unexpected chat completion payload shape") from exc
