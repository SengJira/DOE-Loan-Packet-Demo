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
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from config import load_config  # noqa: E402

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
    return dl.projects.get(project_id=config.project_id)


def _entities(repo_list):
    """Yield entities across the dtlpy list/PagedEntities API differences."""
    iterable = repo_list.all() if hasattr(repo_list, "all") else repo_list
    items = getattr(iterable, "items", iterable)
    for element in items:
        if isinstance(element, list):
            yield from element
        else:
            yield element


def find_pipeline(project, name: str) -> Any | None:
    for pipeline in _entities(project.pipelines.list()):
        if getattr(pipeline, "name", None) == name:
            return pipeline
    return None


def find_service(project, name: str) -> Any | None:
    for service in _entities(project.services.list()):
        if getattr(service, "name", None) == name:
            return service
    return None


def find_task(project, name: str) -> Any | None:
    for task in _entities(project.tasks.list()):
        if getattr(task, "name", None) == name:
            return task
    return None
