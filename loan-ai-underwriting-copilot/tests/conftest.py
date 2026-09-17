import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from demo_cases import load_cases  # noqa: E402


@pytest.fixture(scope="session")
def demo_cases():
    return {case["case_id"]: case for case in load_cases()}


@pytest.fixture
def packet_factory(demo_cases):
    def factory(case_id: str = "case-1-clean"):
        return copy.deepcopy(demo_cases[case_id]["packet"])

    return factory


@pytest.fixture
def clean_packet(packet_factory):
    return packet_factory("case-1-clean")


class StubNvidiaClient:
    """Deterministic stand-in for NvidiaClient used by the offline tests."""

    def __init__(self, response=None, error=None):
        self.response = response or {
            "recommendation": "READY_FOR_UNDERWRITER",
            "confidence": 0.93,
            "executive_summary": "Packet is complete and internally consistent.",
            "key_findings": [{"finding": "Income verified", "evidence": ["payslip.net_monthly_income"]}],
            "risk_flags": [],
            "missing_information": [],
            "required_actions": ["Underwriter to confirm final decision."],
            "evidence_references": ["payslip.net_monthly_income"],
        }
        self.error = error
        self.calls = []

    def chat_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


@pytest.fixture(scope="session")
def llm_responses():
    return json.loads((FIXTURES / "llm_responses.json").read_text())


@pytest.fixture
def stub_client():
    return StubNvidiaClient


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key-not-a-real-credential")
