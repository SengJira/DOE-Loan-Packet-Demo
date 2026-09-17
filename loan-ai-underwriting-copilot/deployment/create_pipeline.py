"""Create the `loan-ai-underwriting-copilot-v1` pipeline.

The pipeline is new and separate: `loan-packet-processing` and
`loan-nemotron-classifier-v7` are never read for modification, and the source
dataset node is created with `load_existing_data=False` so nothing is
bulk-processed on install. Items are fed in deliberately (see --seed-items and
README "Controlled test run").

Flow:
    loan-structured-output
      -> validate_packet_completeness
      -> build_borrower_context
      -> cross_document_reasoning
      -> assess_financial_risk
      -> generate_underwriting_summary
      -> route_by_confidence_and_risk
           |-- READY_FOR_UNDERWRITER -> underwriter review task
           `-- HUMAN_REVIEW          -> human review task
                                        -> capture_reviewer_feedback
                                        -> loan-ground-truth/ai-underwriting-copilot-v1

Usage:
    python deployment/create_pipeline.py [--dry-run] [--install]
"""

from __future__ import annotations

import argparse
import json

from _common import SERVICE_NAME, find_pipeline, find_service, get_project

from config import load_config

NODE_SEQUENCE = [
    "validate_packet_completeness",
    "build_borrower_context",
    "cross_document_reasoning",
    "assess_financial_risk",
    "generate_underwriting_summary",
    "route_by_confidence_and_risk",
]


def _route_filter(dl, route: str) -> dict:
    filters = dl.Filters(resource=dl.FiltersResource.ITEM)
    filters.add(field="metadata.user.aiUnderwriting.route", values=route)
    return filters.prepare(query_only=True).get("filter", {})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--install", action="store_true", help="install (activate) the pipeline after creation")
    parser.add_argument("--task-owner", default=None, help="email of the review task owner")
    args = parser.parse_args()

    config = load_config()

    if args.dry_run:
        print(
            json.dumps(
                {
                    "pipeline": config.pipeline_name,
                    "source_dataset": config.source_dataset,
                    "nodes": NODE_SEQUENCE,
                    "branches": {
                        "READY_FOR_UNDERWRITER": "underwriter-review task",
                        "HUMAN_REVIEW": [
                            config.human_review_task_name,
                            "capture_reviewer_feedback",
                            f"{config.ground_truth_dataset}{config.ground_truth_folder}",
                        ],
                    },
                    "install": args.install,
                },
                indent=2,
            )
        )
        return

    import dtlpy as dl

    project = get_project(dl)
    if find_pipeline(project, config.pipeline_name) is not None:
        raise SystemExit(
            f"pipeline {config.pipeline_name} already exists - refusing to overwrite. "
            "Delete it manually if you intend to recreate it."
        )

    service = find_service(project, SERVICE_NAME)
    if service is None:
        raise SystemExit("service not found; run deployment/deploy_service.py first")

    source = project.datasets.get(dataset_name=config.source_dataset)
    ground_truth = project.datasets.get(dataset_name=config.ground_truth_dataset)
    humans = [c.email for c in project.contributors if getattr(c, "type", None) != "bot"]
    task_owner = args.task_owner or (humans[0] if humans else dl.info()["user_email"])

    recipe_id = source.metadata["system"]["recipes"][0]
    recipe_title = project.recipes.get(recipe_id=recipe_id).title or "loan-underwriting-review"

    pipeline = project.pipelines.create(name=config.pipeline_name, project_id=project.id)

    source_node = dl.DatasetNode(
        name=config.source_dataset,
        project_id=project.id,
        dataset_id=source.id,
        load_existing_data=False,  # never bulk-process the existing dataset
        data_filters=dl.Filters(
            custom_filter={"$and": [{"hidden": False}, {"type": "file"}]}
        ),
        position=(1, 1),
    )
    pipeline.nodes.add(node=source_node)

    previous = source_node
    for index, function_name in enumerate(NODE_SEQUENCE, start=2):
        node = dl.FunctionNode(
            name=function_name,
            service=service,
            function_name=function_name,
            project_id=project.id,
            position=(index, 1),
        )
        previous.connect(node=node)
        previous = node
    route_node = previous

    review_task = dl.TaskNode(
        name=config.human_review_task_name,
        project_id=project.id,
        dataset_id=source.id,
        recipe_title=recipe_title,
        recipe_id=recipe_id,
        task_owner=task_owner,
        task_type="annotation",
        position=(8, 2),
        actions=["approve", "reject"],
        workload=[dl.WorkloadUnit(assignee_id=task_owner, load=100)],
    )
    underwriter_task = dl.TaskNode(
        name="loan-ai-underwriting-copilot-v1-underwriter-queue",
        project_id=project.id,
        dataset_id=source.id,
        recipe_title=recipe_title,
        recipe_id=recipe_id,
        task_owner=task_owner,
        task_type="annotation",
        position=(8, 1),
        actions=["approve", "reject"],
        workload=[dl.WorkloadUnit(assignee_id=task_owner, load=100)],
    )
    capture_node = dl.FunctionNode(
        name="capture_reviewer_feedback",
        service=service,
        function_name="capture_reviewer_feedback",
        project_id=project.id,
        position=(9, 2),
    )
    ground_truth_node = dl.DatasetNode(
        name=config.ground_truth_dataset,
        project_id=project.id,
        dataset_id=ground_truth.id,
        dataset_folder=config.ground_truth_folder,
        load_existing_data=False,
        position=(10, 2),
    )

    # Even the "ready" branch ends in a human queue: the copilot never closes a
    # loan on its own.
    route_node.connect(node=underwriter_task, filters=_route_filter(dl, "READY_FOR_UNDERWRITER"))
    route_node.connect(node=review_task, filters=_route_filter(dl, "HUMAN_REVIEW"))
    review_task.connect(node=capture_node, action="approve")
    underwriter_task.connect(node=capture_node, action="approve")
    capture_node.connect(node=ground_truth_node)

    pipeline.update()
    print(f"pipeline created: {pipeline.name} ({pipeline.id}) - status paused until installed")
    if args.install:
        pipeline.install()
        print("pipeline installed (active). Feed items deliberately; nothing is back-filled.")
    else:
        print("not installed. Run with --install, or install from the console, when you are ready.")


if __name__ == "__main__":
    main()
