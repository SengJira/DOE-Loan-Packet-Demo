"""Loader for the synthetic demo packets.

The fixtures store dates as relative tokens such as ``${T-30}`` so the demo stays
valid whenever it is run (document-recency checks would otherwise start failing
as the fixtures age). Tokens are resolved against ``today``.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

DEFAULT_CASES_PATH = Path(__file__).resolve().parent.parent / "demo" / "synthetic_cases.json"
_TOKEN = re.compile(r"\$\{T([+-]\d+)\}")


def resolve_tokens(value: Any, today: date | None = None) -> Any:
    reference = today or date.today()
    if isinstance(value, str):
        return _TOKEN.sub(lambda m: (reference + timedelta(days=int(m.group(1)))).isoformat(), value)
    if isinstance(value, list):
        return [resolve_tokens(v, reference) for v in value]
    if isinstance(value, dict):
        return {k: resolve_tokens(v, reference) for k, v in value.items()}
    return value


def load_cases(path: Path | None = None, today: date | None = None) -> list[dict[str, Any]]:
    source = Path(path or DEFAULT_CASES_PATH)
    data = json.loads(source.read_text(encoding="utf-8"))
    return [resolve_tokens(case, today) for case in data["cases"]]


def load_case(case_id: str, path: Path | None = None, today: date | None = None) -> dict[str, Any]:
    for case in load_cases(path, today):
        if case["case_id"] == case_id:
            return case
    raise KeyError(f"unknown demo case: {case_id}")
