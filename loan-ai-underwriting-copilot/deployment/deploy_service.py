"""Push the copilot package and deploy its FaaS service.

Creates/updates only `loan-ai-underwriting-copilot` (package) and
`loan-ai-underwriting-copilot-v1` (service). Existing packages, services,
pipelines and datasets are untouched.

Usage:
    python deployment/deploy_service.py [--dry-run]

The NVIDIA key is never passed as a plain environment variable: store it as a
Dataloop secret named NVIDIA_API_KEY (organization -> Secrets) and pass its
name with --secret-name (default: NVIDIA_API_KEY).
"""

from __future__ import annotations

import argparse
import json

from _common import (
    MODULE_NAME,
    PACKAGE_NAME,
    REPO_ROOT,
    SERVICE_NAME,
    find_service,
    get_project,
    manifest,
)


def build_modules(dl):
    spec = manifest()["modules"][0]
    functions = [
        dl.PackageFunction(
            name=function["name"],
            display_name=function.get("displayName"),
            description=function.get("description"),
            inputs=[dl.FunctionIO(type=i["type"], name=i["name"]) for i in function["input"]],
            outputs=[dl.FunctionIO(type=o["type"], name=o["name"]) for o in function["output"]],
        )
        for function in spec["functions"]
    ]
    return [
        dl.PackageModule(
            name=MODULE_NAME,
            entry_point=spec["entryPoint"],
            class_name=spec["className"],
            init_inputs=[dl.FunctionIO(type=dl.PackageInputType.JSON, name="config_overrides")],
            functions=functions,
        )
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the plan without calling Dataloop")
    parser.add_argument("--secret-name", default="NVIDIA_API_KEY", help="Dataloop secret holding the NVIDIA key")
    parser.add_argument("--model", default=None, help="override NVIDIA_MODEL for the service")
    args = parser.parse_args()

    spec = manifest()
    service_spec = spec["services"][0]
    env = dict(spec["environmentVariables"])
    if args.model:
        env["NVIDIA_MODEL"] = args.model

    if args.dry_run:
        print(json.dumps({"package": PACKAGE_NAME, "service": service_spec, "env": env}, indent=2))
        return

    import dtlpy as dl

    project = get_project(dl)
    print(f"project: {project.name} ({project.id})")

    package = project.packages.push(
        package_name=PACKAGE_NAME,
        src_path=str(REPO_ROOT),
        modules=build_modules(dl),
        requirements=[
            dl.PackageRequirement(name="requests", version="2.31.0", operator=">="),
            dl.PackageRequirement(name="jsonschema", version="4.21.0", operator=">="),
        ],
        ignore_sanity_check=True,
    )
    print(f"package pushed: {package.name} v{package.version}")

    secrets = []
    try:
        integration = project.integrations.get(integrations_id=args.secret_name)
        secrets = [integration.id]
    except Exception:
        print(
            f"note: no Dataloop secret/integration named {args.secret_name} was found. "
            "Create it and re-run, or the service will start without an NVIDIA key "
            "and every packet will fail safe to HUMAN_REVIEW."
        )

    existing = find_service(project, SERVICE_NAME)
    if existing is not None:
        print(f"service {SERVICE_NAME} already exists ({existing.id}); updating to the new package revision")
        existing.package_revision = package.version
        existing.init_input = {"config_overrides": env}
        existing.update(force=True)
        service = existing
    else:
        service = package.services.deploy(
            service_name=SERVICE_NAME,
            module_name=MODULE_NAME,
            runtime=dl.KubernetesRuntime(**service_spec["runtime"]),
            execution_timeout=service_spec["executionTimeout"],
            max_attempts=service_spec["maxAttempts"],
            init_input={"config_overrides": env},
            secrets=secrets or None,
            project_id=project.id,
            is_global=False,
        )
    print(f"service ready: {service.name} ({service.id})")
    print("next: python deployment/create_pipeline.py")


if __name__ == "__main__":
    main()
