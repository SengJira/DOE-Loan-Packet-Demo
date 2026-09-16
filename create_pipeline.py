#!/usr/bin/env python3
"""
create_pipeline.py — build the loan-packet processing pipeline in Dataloop.

    /incoming (dataset node)
        -> classify_document_type            (which of the five doc types)
        -> split by type                     (5 filtered edges)
        -> extract_<doc_type>                (one extraction node per type)
        -> confidence filter on the edge:
             min_confidence >= threshold  -> structured-output dataset
             min_confidence <  threshold  -> review task
        -> completed reviews                 -> ground-truth dataset
        -> retrain_trigger

Usage:
    python create_pipeline.py --project "DDOE demo - Loan Packet Processing"
    python create_pipeline.py --project ... --threshold 0.75 --delete-existing

The pipeline is created but NOT installed/started: starting it provisions a
service per code node on the tenant. Start it from the console when you want
the demo live, or pass --start.

Nothing here is destructive except --delete-existing, which removes a pipeline
of the same name before rebuilding.
"""

from __future__ import annotations

import argparse
import sys

DOC_TYPES = ("loan_application", "pay_stub", "bank_statement", "w2",
             "id_verification")
DEFAULT_THRESHOLD = 0.75


# --------------------------------------------------------------------------
# Node bodies. These run as services inside Dataloop, so each one must be
# self-contained: no imports or globals from this file.
# --------------------------------------------------------------------------

def classify_document_type(item):
    """Tag the item with one of the five loan-packet document types.

    The demo data already carries the true type in item metadata; a real
    deployment replaces this body with a call to a classification model.
    """
    doc_types = ["loan_application", "pay_stub", "bank_statement", "w2",
                 "id_verification"]
    user = item.metadata.get("user", {})
    doc_type = user.get("doc_type")

    if doc_type not in doc_types:
        name = item.name.lower()
        doc_type = next((t for t in doc_types if t in name), "unknown")

    item.metadata.setdefault("user", {})["doc_type"] = doc_type
    item.metadata["user"]["classified_by"] = "pipeline/classify_document_type"
    item.update()
    return item


def extract_fields(item):
    """Extract the structured fields for this document type.

    Replace this body with a call to the extraction model (item.run_model or a
    model adapter). Until then it promotes the prelabels that ship with the
    demo data, so the downstream confidence routing is exercised end to end.
    """
    user = item.metadata.setdefault("user", {})
    prediction = user.get("prediction") or {}
    confidence = user.get("confidence") or {}

    user["extraction"] = prediction
    user["min_confidence"] = min(confidence.values()) if confidence else 0.0
    user["extracted_by"] = "pipeline/extract_fields"
    item.update()
    return item


def retrain_trigger(item):
    """Fire a retrain once enough newly reviewed items have landed.

    Counts the ground-truth dataset and logs the decision; wire the model
    variable below to a real model to make it call `model.train()`.
    """
    import os

    batch = int(os.environ.get("RETRAIN_BATCH_SIZE", "50"))
    dataset = item.dataset
    count = dataset.items.list().items_count

    if count and count % batch == 0:
        print(f"retrain: {count} ground-truth items, batch size {batch} "
              f"-> triggering retraining")
        # model = dl.models.get(model_name=os.environ["RETRAIN_MODEL"])
        # model.train()
    else:
        print(f"retrain: {count} ground-truth items, waiting for the next "
              f"multiple of {batch}")
    return item


# --------------------------------------------------------------------------


def get_or_create_dataset(project, name):
    try:
        return project.datasets.get(dataset_name=name)
    except Exception:
        dataset = project.datasets.create(dataset_name=name)
        print(f"created dataset {name}")
        return dataset


def build(dl, args):
    project = dl.projects.get(project_name=args.project)
    print(f"project {project.name} ({project.id})")

    source = project.datasets.get(dataset_name=args.source_dataset)
    structured = get_or_create_dataset(project, args.structured_dataset)
    ground_truth = get_or_create_dataset(project, args.ground_truth_dataset)

    if args.delete_existing:
        try:
            project.pipelines.delete(pipeline_name=args.name)
            print(f"deleted the existing pipeline {args.name}")
        except Exception:
            pass

    pipeline = project.pipelines.create(name=args.name, project_id=project.id)

    incoming_filters = dl.Filters()
    incoming_filters.add(field="dir", values=f"/{args.incoming_folder}*")

    incoming = dl.DatasetNode(
        name="incoming",
        project_id=project.id,
        dataset_id=source.id,
        dataset_folder=f"/{args.incoming_folder}",
        load_existing_data=True,
        data_filters=incoming_filters,
        position=(1, 3),
    )
    pipeline.nodes.add(incoming)

    classify = dl.CodeNode(
        name="classify_document_type",
        project_id=project.id,
        project_name=project.name,
        method=classify_document_type,
        position=(2, 3),
    )
    incoming.connect(node=classify)

    review = dl.TaskNode(
        name="low_confidence_review",
        project_id=project.id,
        dataset_id=source.id,
        recipe_title=source.recipes.list()[0].title,
        recipe_id=source.recipes.list()[0].id,
        task_owner=args.task_owner,
        task_type="annotation",
        workload=[dl.WorkloadUnit(assignee_id=args.task_owner, load=100)],
        position=(5, 5),
    )
    pipeline.nodes.add(review)

    structured_node = dl.DatasetNode(
        name="structured_output",
        project_id=project.id,
        dataset_id=structured.id,
        position=(5, 1),
    )
    pipeline.nodes.add(structured_node)

    ground_truth_node = dl.DatasetNode(
        name="ground_truth",
        project_id=project.id,
        dataset_id=ground_truth.id,
        position=(6, 5),
    )
    pipeline.nodes.add(ground_truth_node)

    # One extraction node per document type, reached by a filtered edge —
    # that pair of things is the "split by type".
    for row, doc_type in enumerate(DOC_TYPES):
        extract = dl.CodeNode(
            name=f"extract_{doc_type}",
            project_id=project.id,
            project_name=project.name,
            method=extract_fields,
            position=(4, row + 1),
        )
        pipeline.nodes.add(extract)

        type_filter = dl.Filters()
        type_filter.add(field="metadata.user.doc_type", values=doc_type)
        classify.connect(node=extract, filters=type_filter)

        # Confidence routing happens on the edge, not in a node, so the
        # threshold is visible in the pipeline graph.
        high = dl.Filters()
        high.add(field="metadata.user.min_confidence",
                 values=args.threshold,
                 operator=dl.FiltersOperations.GREATER_THAN_OR_EQUAL)
        extract.connect(node=structured_node, filters=high)

        low = dl.Filters()
        low.add(field="metadata.user.min_confidence",
                values=args.threshold,
                operator=dl.FiltersOperations.LESS_THAN)
        extract.connect(node=review, filters=low)

    # Completed reviews are the new ground truth.
    review.connect(node=ground_truth_node, action="complete")

    retrain = dl.CodeNode(
        name="retrain_trigger",
        project_id=project.id,
        project_name=project.name,
        method=retrain_trigger,
        position=(7, 5),
    )
    ground_truth_node.connect(node=retrain)

    pipeline = pipeline.update()
    print(f"pipeline {pipeline.name} ({pipeline.id}) "
          f"with {len(pipeline.nodes)} nodes")

    if args.start:
        pipeline.install()
        print("pipeline installed and running")
    else:
        print("pipeline created but not started (use --start, or press Start "
              "in the console) — starting it provisions one service per code "
              "node")

    print(f"https://console.dataloop.ai/projects/{project.id}/pipelines/"
          f"{pipeline.id}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default="DDOE demo - Loan Packet Processing")
    ap.add_argument("--name", default="loan-packet-processing")
    ap.add_argument("--source-dataset", default="loan-packets")
    ap.add_argument("--structured-dataset", default="loan-structured-output")
    ap.add_argument("--ground-truth-dataset", default="loan-ground-truth")
    ap.add_argument("--incoming-folder", default="incoming")
    ap.add_argument("--task-owner",
                    help="email of the review task owner; defaults to the "
                         "logged-in user")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help="minimum confidence that skips human review")
    ap.add_argument("--start", action="store_true",
                    help="install the pipeline (provisions services)")
    ap.add_argument("--delete-existing", action="store_true",
                    help="delete a pipeline of the same name first")
    args = ap.parse_args()

    import dtlpy as dl

    if dl.token_expired():
        dl.login()

    if not args.task_owner:
        args.task_owner = dl.info()["user_email"]

    return build(dl, args)


if __name__ == "__main__":
    sys.exit(main())
