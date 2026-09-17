import json

import pytest
import requests

from borrower_context import build_borrower_context
from completeness import validate_packet_completeness
from config import load_config
from cross_document import cross_document_reasoning
from financial_risk import assess_financial_risk
from logging_utils import mask_identifier, redact
from nvidia_client import (
    ModelResponseError,
    NvidiaApiError,
    NvidiaClient,
    parse_json_response,
    strip_code_fences,
)
from schemas import LLM_SUMMARY_SCHEMA, UNDERWRITING_SUMMARY_SCHEMA, is_valid
from underwriting_summary import (
    generate_underwriting_summary,
    minimize_context,
)

VALID_LLM_JSON = {
    "recommendation": "READY_FOR_UNDERWRITER",
    "confidence": 0.91,
    "executive_summary": "Complete and consistent packet.",
    "key_findings": [],
    "risk_flags": [],
    "missing_information": [],
    "required_actions": [],
    "evidence_references": ["payslip.net_monthly_income"],
}


def _analysis(packet):
    completeness = validate_packet_completeness(packet)
    borrower = build_borrower_context(packet)
    cross = cross_document_reasoning(packet, borrower)
    risk = assess_financial_risk(packet, borrower, cross, completeness)
    return completeness, borrower, cross, risk


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = 0

    def post(self, *args, **kwargs):
        self.posts += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, *args, **kwargs):
        return self.responses.pop(0)


def _completion(text):
    return FakeResponse(200, {"choices": [{"message": {"content": text}}]})


def test_strip_code_fences_variants():
    assert strip_code_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_code_fences('```\n{"a": 1}```') == '{"a": 1}'
    assert strip_code_fences('Here you go: {"a": 1} thanks') == '{"a": 1}'


def test_parse_json_response_rejects_garbage():
    with pytest.raises(ModelResponseError):
        parse_json_response("not json at all")
    with pytest.raises(ModelResponseError):
        parse_json_response("")


def test_chat_json_validates_against_schema():
    session = FakeSession([_completion("```json\n" + json.dumps(VALID_LLM_JSON) + "\n```")])
    client = NvidiaClient(load_config(), session=session)
    result = client.chat_json("sys", "user", schema=LLM_SUMMARY_SCHEMA)
    assert result["recommendation"] == "READY_FOR_UNDERWRITER"


def test_chat_json_raises_on_schema_violation():
    bad = dict(VALID_LLM_JSON, confidence=5.0)
    session = FakeSession([_completion(json.dumps(bad))])
    client = NvidiaClient(load_config(), session=session)
    with pytest.raises(ModelResponseError):
        client.chat_json("sys", "user", schema=LLM_SUMMARY_SCHEMA)


def test_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr("src.nvidia_client.time.sleep", lambda *_: None)
    session = FakeSession([FakeResponse(429), FakeResponse(503), _completion(json.dumps(VALID_LLM_JSON))])
    client = NvidiaClient(load_config(), session=session)
    assert client.chat_json("sys", "user", schema=LLM_SUMMARY_SCHEMA)["confidence"] == 0.91
    assert session.posts == 3


def test_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr("src.nvidia_client.time.sleep", lambda *_: None)
    session = FakeSession([FakeResponse(500), FakeResponse(500), FakeResponse(500)])
    client = NvidiaClient(load_config(), session=session)
    with pytest.raises(NvidiaApiError):
        client.chat_json("sys", "user")


def test_non_retryable_status_fails_fast():
    session = FakeSession([FakeResponse(401)])
    client = NvidiaClient(load_config(), session=session)
    with pytest.raises(NvidiaApiError):
        client.chat_json("sys", "user")
    assert session.posts == 1


def test_resolve_model_prefers_available_model():
    session = FakeSession(
        [
            FakeResponse(200, {"data": [{"id": "nvidia/llama-3.1-nemotron-70b-instruct"}]}),
            FakeResponse(200),  # probe succeeds
        ]
    )
    client = NvidiaClient(load_config(), session=session)
    assert client.resolve_model(["meta/llama-3.3-70b-instruct", "nvidia/llama-3.1-nemotron-70b-instruct"]) == (
        "nvidia/llama-3.1-nemotron-70b-instruct"
    )


def test_resolve_model_skips_listed_but_deprecated_model():
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "data": [
                        {"id": "nvidia/llama-3.1-nemotron-70b-instruct"},
                        {"id": "nvidia/nemotron-3-ultra-550b-a55b"},
                    ]
                },
            ),
            FakeResponse(404),  # 70b probe fails (listed but deprecated)
            FakeResponse(200),  # ultra probe succeeds
        ]
    )
    client = NvidiaClient(load_config(), session=session)
    assert client.resolve_model(
        ["nvidia/llama-3.1-nemotron-70b-instruct", "nvidia/nemotron-3-ultra-550b-a55b"]
    ) == "nvidia/nemotron-3-ultra-550b-a55b"


@pytest.mark.parametrize("key", ["valid_fenced", "valid_with_prose"])
def test_recorded_valid_responses_parse(llm_responses, key):
    session = FakeSession([_completion(llm_responses[key])])
    client = NvidiaClient(load_config(), session=session)
    assert client.chat_json("sys", "user", schema=LLM_SUMMARY_SCHEMA)["confidence"] <= 1.0


@pytest.mark.parametrize(
    "key",
    ["invalid_not_json", "invalid_truncated", "invalid_schema_confidence", "invalid_schema_enum"],
)
def test_recorded_invalid_responses_are_rejected(llm_responses, key):
    session = FakeSession([_completion(llm_responses[key])])
    client = NvidiaClient(load_config(), session=session)
    with pytest.raises(ModelResponseError):
        client.chat_json("sys", "user", schema=LLM_SUMMARY_SCHEMA)


def test_summary_is_schema_valid(clean_packet, stub_client):
    summary = generate_underwriting_summary(*_analysis(clean_packet), client=stub_client())
    assert is_valid(summary, UNDERWRITING_SUMMARY_SCHEMA)
    assert summary["model_output_valid"] is True
    assert summary["disclaimer"].startswith("AI-generated analysis")


def test_api_failure_fails_safe(clean_packet, stub_client):
    summary = generate_underwriting_summary(
        *_analysis(clean_packet), client=stub_client(error=NvidiaApiError("boom"))
    )
    assert summary["recommendation"] == "INSUFFICIENT_DATA"
    assert summary["confidence"] == 0.0
    assert summary["model_output_valid"] is False


def test_invalid_model_output_fails_safe(clean_packet, stub_client):
    summary = generate_underwriting_summary(
        *_analysis(clean_packet), client=stub_client(error=ModelResponseError("bad json"))
    )
    assert summary["recommendation"] == "INSUFFICIENT_DATA"
    assert summary["model_output_valid"] is False


def test_missing_api_key_fails_safe(clean_packet, monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    summary = generate_underwriting_summary(*_analysis(clean_packet), config=load_config())
    assert summary["recommendation"] == "INSUFFICIENT_DATA"


def test_payload_is_minimized_and_identifiers_masked(clean_packet):
    minimized = minimize_context(*_analysis(clean_packet))
    serialized = json.dumps(minimized)
    assert "1102345678901" not in serialized
    assert "1-1023-45678-90-1" not in serialized
    assert minimized["borrower"]["borrower"]["identification_number"].startswith("*")
    assert "RENT TRANSFER" not in serialized
    assert "CARD PAYMENT" not in serialized
    assert "documents" not in minimized


def test_logging_redacts_secrets_and_identifiers():
    logged = redact({"api_key": "secret", "messages": [{"content": "x"}], "note": "ID 1102345678901"})
    assert logged["api_key"] == "<redacted>"
    assert logged["messages"] == "<redacted>"
    assert "1102345678901" not in logged["note"]
    assert mask_identifier("1102345678901") == "*********8901"
