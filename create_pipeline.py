#!/usr/bin/env python3
"""
create_pipeline.py — build the loan-packet processing pipeline in Dataloop.

Two templates, selected with --template:

extract (default):
    /incoming (dataset node)
        -> classify_document_type            (which of the five doc types)
        -> split by type                     (5 filtered edges)
        -> extract_<doc_type>                (one extraction node per type)
        -> confidence filter on the edge:
             min_confidence >= threshold  -> structured-output dataset
             min_confidence <  threshold  -> review task
        -> completed reviews                 -> ground-truth dataset
        -> retrain_trigger

annotate:
    /unlabeled (dataset node)
        -> auto_label                        (writes annotations)
        -> confidence filter on the edge:
             min_confidence >= threshold  -> auto-annotated dataset
             min_confidence <  threshold  -> annotation task
        -> completed annotations             -> ground-truth dataset
        -> retrain_trigger

ai-classify (real AI: CLIP embedding + nearest-centroid):
    /incoming of the images dataset (dataset node)
        -> extract_item                      FunctionNode on the clip-extraction
                                             service (embeds the item with CLIP)
        -> clip_classify                     code node: cosine similarity against
                                             per-type centroids of the labeled
                                             /base images
        -> split by type                     (5 filtered edges)
        -> extract_<doc_type>                (one extraction node per type)
        -> confidence filter on the edge:
             min_confidence >= threshold  -> structured-output dataset
             min_confidence <  threshold  -> review task
        -> completed reviews                 -> ground-truth dataset
        -> retrain_trigger

    Requires a rasterized copy of the PDF dataset (see README-pipeline.md)
    and the deployed CLIP model service id via --clip-service.

Usage:
    python create_pipeline.py --project "DDOE demo - Loan Packet Processing"
    python create_pipeline.py --template annotate --name loan-auto-annotation \
        --delete-existing --start

The pipeline is created but NOT installed/started: starting it provisions a
service per code node on the tenant. Start it from the console when you want
the demo live, or pass --start.

Nothing here is destructive except --delete-existing, which removes a pipeline
of the same name before rebuilding.
"""

from __future__ import annotations

import argparse
import inspect
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

    The generator only writes prelabels for the `unlabeled` split, so items
    arriving from `/incoming` have none. Rather than scoring those 0.0, which
    sends every one of them down the low-confidence edge and leaves the
    structured-output branch empty, a confidence is derived from the item id:
    deterministic per item and spread either side of the threshold.

    Keep every code-node body pure ASCII. Dataloop truncates the uploaded
    source by the number of extra bytes any non-ASCII character costs, which
    silently lops characters off the end of the function.
    """
    import hashlib
    import dtlpy as dl

    user = item.metadata.setdefault("user", {})
    prediction = user.get("prediction") or {}
    confidence = user.get("confidence") or {}

    if not confidence:
        fields = list(prediction) or ["doc_type"]
        for field in fields:
            seed = hashlib.sha256(f"{item.id}:{field}".encode()).digest()
            # 0.55 .. 0.99, so a little over half clear a 0.75 threshold
            confidence[field] = round(0.55 + (seed[0] / 255) * 0.44, 3)
        user["confidence"] = confidence
        user["confidence_source"] = "synthetic"

    user["extraction"] = prediction
    user["min_confidence"] = min(confidence.values())
    user["extracted_by"] = "pipeline/extract_fields"
    item.update()
    return item


def auto_label(item):
    """Annotate the item with its predicted document type and confidence.

    Replace this body with a real pre-labeling model. Until then it uses the
    generator's prelabels where they exist (the `unlabeled` split) and a
    deterministic synthetic confidence where they do not, so the confidence
    edge still splits items between the dataset and the annotation task.

    Keep every code-node body pure ASCII. Dataloop truncates the uploaded
    source by the number of extra bytes any non-ASCII character costs.
    """
    import hashlib
    import dtlpy as dl

    user = item.metadata.setdefault("user", {})
    prediction = user.get("prediction") or {}
    confidence = user.get("confidence") or {}
    doc_types = ["loan_application", "pay_stub", "bank_statement", "w2",
                 "id_verification"]

    doc_type = user.get("doc_type")
    if doc_type not in doc_types:
        name = item.name.lower()
        doc_type = next((t for t in doc_types if t in name), "unknown")
        user["doc_type"] = doc_type

    if not confidence:
        fields = list(prediction) or ["doc_type"]
        for field in fields:
            seed = hashlib.sha256(f"{item.id}:{field}".encode()).digest()
            # 0.55 .. 0.99, so a little over half clear a 0.75 threshold
            confidence[field] = round(0.55 + (seed[0] / 255) * 0.44, 3)
        user["confidence"] = confidence
        user["confidence_source"] = "synthetic"

    builder = item.annotations.builder()
    builder.add(annotation_definition=dl.Classification(label=doc_type))
    item.annotations.upload(builder)

    user["min_confidence"] = min(confidence.values())
    user["labeled_by"] = "pipeline/auto_label"
    item.update()
    return item


def clip_classify(item):
    """Classify an image item by CLIP-embedding nearest-centroid.

    Reads the item's CLIP embedding from the model's feature set, compares it
    against per-document-type centroids built from the labeled /base images,
    and writes doc_type plus min_confidence to item metadata. The confidence
    is a margin score: how clearly the best centroid beats the runner-up.

    Keep every code-node body pure ASCII. Dataloop truncates the uploaded
    source by the number of extra bytes any non-ASCII character costs.
    """
    import math
    import dtlpy as dl

    doc_types = ["loan_application", "pay_stub", "bank_statement", "w2",
                 "id_verification"]
    user = item.metadata.setdefault("user", {})
    dataset = item.dataset
    project = dataset.project

    # Labeled reference items live in /base of the same dataset.
    base_filters = dl.Filters()
    base_filters.add(field="dir", values="/base*")
    labeled = {}
    for ref in dataset.items.list(filters=base_filters).all():
        t = ref.metadata.get("user", {}).get("doc_type")
        if t in doc_types:
            labeled[ref.id] = t

    fs = project.feature_sets.get(
        feature_set_name="CLIP model for semantic search")
    vectors = {}
    item_vec = None
    for feat in fs.features.list().all():
        v = list(feat.value)
        if feat.entity_id == item.id:
            item_vec = v
        elif feat.entity_id in labeled:
            vectors[feat.entity_id] = v

    if item_vec is None:
        user["doc_type"] = "unknown"
        user["min_confidence"] = 0.0
        user["classified_by"] = "pipeline/clip_classify:no-embedding"
        item.update()
        return item

    def norm(v):
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def dot(a, b):
        return sum(x * y for x, y in zip(a, b))

    iv = norm(item_vec)
    sims = {}
    for t in doc_types:
        vecs = [vectors[i] for i, lt in labeled.items() if lt == t
                and i in vectors]
        if not vecs:
            sims[t] = 0.0
            continue
        centroid = norm([sum(vec[d] for vec in vecs) / len(vecs)
                         for d in range(len(item_vec))])
        sims[t] = dot(iv, centroid)

    ranked = sorted(sims.values(), reverse=True)
    best_type = max(sims, key=sims.get)
    margin = ranked[0] - (ranked[1] if len(ranked) > 1 else 0.0)

    user["doc_type"] = best_type
    user["min_confidence"] = round(min(1.0, max(0.0, margin * 12 + 0.5)), 3)
    user["confidence_source"] = "clip-margin"
    user["classified_by"] = "pipeline/clip_classify"
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


def assert_ascii(func):
    """Dataloop truncates a code node's source once it contains non-ASCII."""
    try:
        inspect.getsource(func).encode("ascii")
    except UnicodeEncodeError as exc:
        raise SystemExit(f"{func.__name__} contains a non-ASCII character at "
                         f"offset {exc.start}; code node sources must be "
                         f"pure ASCII or Dataloop uploads a truncated body")


def get_or_create_dataset(project, name):
    try:
        return project.datasets.get(dataset_name=name)
    except Exception:
        dataset = project.datasets.create(dataset_name=name)
        print(f"created dataset {name}")
        return dataset


def build_extract_pipeline(dl, args, project, pipeline, source):
    structured = get_or_create_dataset(project, args.structured_dataset)
    ground_truth = get_or_create_dataset(project, args.ground_truth_dataset)

    incoming_filters = dl.Filters()
    incoming_filters.add(field="dir", values=f"/{args.source_folder}*")

    incoming = dl.DatasetNode(
        name="incoming",
        project_id=project.id,
        dataset_id=source.id,
        dataset_folder=f"/{args.source_folder}",
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

    # One extraction node per document type, reached by a filtered edge -
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
    return ground_truth_node


def build_annotate_pipeline(dl, args, project, pipeline, source):
    annotated = get_or_create_dataset(project, args.annotated_dataset)
    ground_truth = get_or_create_dataset(project, args.ground_truth_dataset)

    folder_filters = dl.Filters()
    folder_filters.add(field="dir", values=f"/{args.source_folder}*")

    unlabeled = dl.DatasetNode(
        name="unlabeled",
        project_id=project.id,
        dataset_id=source.id,
        dataset_folder=f"/{args.source_folder}",
        load_existing_data=True,
        data_filters=folder_filters,
        position=(1, 3),
    )
    pipeline.nodes.add(unlabeled)

    labeler = dl.CodeNode(
        name="auto_label",
        project_id=project.id,
        project_name=project.name,
        method=auto_label,
        position=(2, 3),
    )
    unlabeled.connect(node=labeler)

    task = dl.TaskNode(
        name="low_confidence_annotation",
        project_id=project.id,
        dataset_id=source.id,
        recipe_title=source.recipes.list()[0].title,
        recipe_id=source.recipes.list()[0].id,
        task_owner=args.task_owner,
        task_type="annotation",
        workload=[dl.WorkloadUnit(assignee_id=args.task_owner, load=100)],
        position=(3, 5),
    )
    pipeline.nodes.add(task)

    annotated_node = dl.DatasetNode(
        name="auto_annotated",
        project_id=project.id,
        dataset_id=annotated.id,
        position=(3, 1),
    )
    pipeline.nodes.add(annotated_node)

    ground_truth_node = dl.DatasetNode(
        name="ground_truth",
        project_id=project.id,
        dataset_id=ground_truth.id,
        position=(4, 5),
    )
    pipeline.nodes.add(ground_truth_node)

    high = dl.Filters()
    high.add(field="metadata.user.min_confidence",
             values=args.threshold,
             operator=dl.FiltersOperations.GREATER_THAN_OR_EQUAL)
    labeler.connect(node=annotated_node, filters=high)

    low = dl.Filters()
    low.add(field="metadata.user.min_confidence",
            values=args.threshold,
            operator=dl.FiltersOperations.LESS_THAN)
    labeler.connect(node=task, filters=low)

    task.connect(node=ground_truth_node, action="complete")
    return ground_truth_node


def build_ai_classify_pipeline(dl, args, project, pipeline, source):
    structured = get_or_create_dataset(project, args.structured_dataset)
    ground_truth = get_or_create_dataset(project, args.ground_truth_dataset)

    try:
        service = project.services.get(service_id=args.clip_service)
    except Exception:
        service = next(s for s in project.services.list().items
                       if s.name == args.clip_service)

    folder_filters = dl.Filters()
    folder_filters.add(field="dir", values=f"/{args.source_folder}*")

    incoming = dl.DatasetNode(
        name="incoming",
        project_id=project.id,
        dataset_id=source.id,
        dataset_folder=f"/{args.source_folder}",
        load_existing_data=True,
        data_filters=folder_filters,
        position=(1, 3),
    )
    pipeline.nodes.add(incoming)

    embed = dl.FunctionNode(
        name="clip_embed",
        service=service,
        function_name="extract_item",
        project_id=project.id,
        project_name=project.name,
        position=(2, 3),
    )
    incoming.connect(node=embed)

    classify = dl.CodeNode(
        name="clip_classify",
        project_id=project.id,
        project_name=project.name,
        method=clip_classify,
        position=(3, 3),
    )
    embed.connect(node=classify)

    review = dl.TaskNode(
        name="low_confidence_review",
        project_id=project.id,
        dataset_id=source.id,
        recipe_title=source.recipes.list()[0].title,
        recipe_id=source.recipes.list()[0].id,
        task_owner=args.task_owner,
        task_type="annotation",
        workload=[dl.WorkloadUnit(assignee_id=args.task_owner, load=100)],
        position=(6, 5),
    )
    pipeline.nodes.add(review)

    structured_node = dl.DatasetNode(
        name="structured_output",
        project_id=project.id,
        dataset_id=structured.id,
        position=(6, 1),
    )
    pipeline.nodes.add(structured_node)

    ground_truth_node = dl.DatasetNode(
        name="ground_truth",
        project_id=project.id,
        dataset_id=ground_truth.id,
        position=(7, 5),
    )
    pipeline.nodes.add(ground_truth_node)

    for row, doc_type in enumerate(DOC_TYPES):
        extract = dl.CodeNode(
            name=f"extract_{doc_type}",
            project_id=project.id,
            project_name=project.name,
            method=extract_fields,
            position=(5, row + 1),
        )
        pipeline.nodes.add(extract)

        type_filter = dl.Filters()
        type_filter.add(field="metadata.user.doc_type", values=doc_type)
        classify.connect(node=extract, filters=type_filter)

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

    review.connect(node=ground_truth_node, action="complete")
    return ground_truth_node


def build(dl, args):
    for func in (classify_document_type, extract_fields, auto_label,
                 clip_classify, retrain_trigger):
        assert_ascii(func)

    project = dl.projects.get(project_name=args.project)
    print(f"project {project.name} ({project.id})")

    source = project.datasets.get(dataset_name=args.source_dataset)

    if args.delete_existing:
        try:
            project.pipelines.delete(pipeline_name=args.name)
            print(f"deleted the existing pipeline {args.name}")
        except Exception:
            pass

    pipeline = project.pipelines.create(name=args.name, project_id=project.id)

    if args.template == "annotate":
        ground_truth_node = build_annotate_pipeline(dl, args, project,
                                                    pipeline, source)
    elif args.template == "ai-classify":
        ground_truth_node = build_ai_classify_pipeline(dl, args, project,
                                                       pipeline, source)
    else:
        ground_truth_node = build_extract_pipeline(dl, args, project,
                                                   pipeline, source)

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
    ap.add_argument("--annotated-dataset", default="loan-auto-annotated",
                    help="dataset receiving high-confidence auto-labels "
                         "(annotate template)")
    ap.add_argument("--template", choices=("extract", "annotate",
                                           "ai-classify"),
                    default="extract",
                    help="extract: /incoming classify/extract/review flow; "
                         "annotate: /unlabeled auto-label flow")
    ap.add_argument("--source-folder", "--incoming-folder",
                    dest="source_folder", default=None,
                    help="dataset folder the pipeline watches (default: "
                         "incoming for extract, unlabeled for annotate)")
    ap.add_argument("--task-owner",
                    help="email of the review task owner; defaults to the "
                         "logged-in user")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help="minimum confidence that skips human review")
    ap.add_argument("--start", action="store_true",
                    help="install the pipeline (provisions services)")
    ap.add_argument("--clip-service",
                    help="service id of the deployed CLIP extract_item "
                         "function (ai-classify template); defaults to the "
                         "service named clip-extraction")
    ap.add_argument("--delete-existing", action="store_true",
                    help="delete a pipeline of the same name first")
    args = ap.parse_args()
    if args.clip_service is None:
        args.clip_service = "clip-extraction"
    if args.source_folder is None:
        args.source_folder = ("unlabeled" if args.template == "annotate"
                              else "incoming")

    import dtlpy as dl

    if dl.token_expired():
        dl.login()

    if not args.task_owner:
        args.task_owner = dl.info()["user_email"]

    return build(dl, args)


if __name__ == "__main__":
    sys.exit(main())
