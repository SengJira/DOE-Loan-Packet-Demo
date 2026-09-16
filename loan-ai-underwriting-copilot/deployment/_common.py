"""Shared helpers for the deployment scripts.

Every script here is additive by design: it creates the new copilot package,
service, pipeline and review task only. Nothing existing is deleted, renamed,
paused or overwritten.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.config import load_config  # noqa: E402

PACKAGE_NAME = "loan-ai-underwriting-copilot"
SERVICE_NAME = "loan-ai-underwriting-copilot-v1"
MODULE_NAME = "loan_underwriting_copilot"


def manifest() -> dict[str, Any]:
    import json

    return json.loads((Path(__file__).with_name("dataloop_package.json")).read_text())


def login(dl) -> None:
    """Authenticate with whatever credentials are present in the environment."""
    if dl.token_expired():
        api_key = os.environ.get("DATALOOP_API_KEY", "").strip()
        email = os.environ.get("DATALOOP_M2M_EMAIL", "").strip()
        password = os.environ.get("DATALOOP_M2M_PASSWORD", "").strip()
        client_id = os.environ.get("DATALOOP_CLIENT_ID", "").strip()
        if api_key:
            dl.login_api_key(api_key)
        elif email and password:
            dl.login_m2m(
                email=email,
                password=password,
                client_id=client_id or None,
                client_secret=os.environ.get("DATALOOP_CLIENT_SECRET") or None,
            )
        else:
            raise RuntimeError(
                "No Dataloop credentials found. Set DATALOOP_API_KEY, or "
                "DATALOOP_M2M_EMAIL/DATALOOP_M2M_PASSWORD, before running deployment."
            )


def get_project(dl):
    config = load_config()
    login(dl)
    return dl.projects.get(project_id=config.dataloop_project_id)


def find_pipeline(project, name: str) -> Any | None:
    for pipeline in project.pipelines.list().all():
        if pipeline.name == name:
            return pipeline
    return None


def find_service(project, name: str) -> Any | None:
    for service in project.services.list().all():
        if service.name == name:
            return service
    return None


def find_task(project, name: str) -> Any | None:
    for task in project.tasks.list():
        if task.name == name:
            return task
    return None
