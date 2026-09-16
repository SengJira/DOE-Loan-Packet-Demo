"""Configure the human-review experience for the copilot pipeline.

Creates a dedicated recipe/ontology for reviewer input and writes the reviewer
instruction payload. It never edits recipes used by the existing pipelines: a
new recipe is cloned for the copilot review task only.

What the reviewer sees (all of it is already on the item, written by the
service under `metadata.user.aiUnderwriting`):
  - the original documents and the structured extraction (item + parent item)
  - AI executive summary, key findings, confidence and model id
  - risk flags with evidence, cross-document mismatches, missing information
  - the deterministic routing reasons and required reviewer actions

What the reviewer can do:
  - correct extracted values           -> classification/attribute inputs below
  - confirm or dismiss each risk flag  -> `risk_flag_decision` attributes
  - add comments                       -> `reviewer_comments` free text
  - set the final review result        -> `final_review_result`
  - record the decision reason         -> `decision_reason`

Usage:
    python deployment/configure_human_review.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json

from _common import find_task, get_project

from config import load_config

REVIEW_FORM = {
    "final_review_result": {
        "type": "options",
        "required": True,
        "values": [
            "APPROVED_FOR_UNDERWRITING",
            "RETURNED_FOR_MORE_INFORMATION",
            "DECLINED_BY_UNDERWRITER",
            "ESCALATED",
        ],
    },
    "decision_reason": {"type": "freeText", "required": True},
    "reviewer_comments": {"type": "freeText", "required": False},
    "risk_flag_decision": {
        "type": "options",
        "required": False,
        "description": "Confirm or dismiss each AI risk flag (code:CONFIRMED / code:DISMISSED).",
        "values": ["CONFIRMED", "DISMISSED", "NEEDS_MORE_EVIDENCE"],
    },
    "corrected_fields": {
        "type": "freeText",
        "required": False,
        "description": "JSON object of corrected extracted values, e.g. {\"declared_monthly_income\": 65000}.",
    },
}

REVIEWER_INSTRUCTIONS = """
Loan AI Underwriting Copilot - reviewer instructions

1. Read the AI executive summary, then verify every claim against the cited
   evidence. The AI only cites document/field references; if a claim has no
   evidence, treat it as unverified.
2. Work through the cross-document checks. Confirm or dismiss each MISMATCH.
3. Work through the risk flags. Confirm or dismiss each one.
4. Correct any extracted value that is wrong (corrected_fields).
5. Record final_review_result and decision_reason.

The AI never approves or declines a loan. The final decision is yours.
""".strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config()

    if args.dry_run:
        print(json.dumps({"form": REVIEW_FORM, "instructions": REVIEWER_INSTRUCTIONS}, indent=2))
        return

    import dtlpy as dl

    project = get_project(dl)
    copilot_tasks = [
        t
        for t in project.tasks.list()
        if t and getattr(t, "name", "") and config.pipeline_name in t.name
    ]
    task = next(
        (t for t in copilot_tasks if t.name == config.human_review_task_name),
        None,
    ) or find_task(project, config.human_review_task_name)
    if task is None:
        raise SystemExit(
            f"task {config.human_review_task_name} not found; run deployment/create_pipeline.py first"
        )

    # Clone the source recipe so the review form never edits the recipe shared
    # with the existing pipelines, then repoint the copilot tasks at the clone.
    review_recipe_id = getattr(task, "recipe_id", None)
    if getattr(task, "copilot_review_recipe", None):
        review_recipe_id = task.copilot_review_recipe
    recipe = dl.recipes.get(recipe_id=review_recipe_id)
    if not getattr(recipe, "title", "").endswith("(underwriting-copilot)"):
        recipe = recipe.clone()
        recipe.title = f"{recipe.title} (underwriting-copilot)"
        recipe.update(system_metadata=True)
        for t in copilot_tasks:
            t.recipe_id = recipe.id
            t.update()
        print(f"cloned recipe for copilot review: {recipe.id}")

    ontology = recipe.ontologies.list()[0]

    existing_labels = {label.tag for label in ontology.labels}
    for name, spec in REVIEW_FORM.items():
        if name not in existing_labels:
            ontology.add_label(label_name=name)
        ontology.update_attributes(
            key=name,
            title=name.replace("_", " ").title(),
            attribute_type=spec["type"],
            values=spec.get("values"),
            optional=not spec["required"],
            attribute_range=None,
        )

    recipe.metadata = recipe.metadata or {}
    recipe.metadata.setdefault("system", {})
    recipe.metadata["aiUnderwritingReview"] = {
        "instructions": REVIEWER_INSTRUCTIONS,
        "displayPanels": [
            "metadata.user.aiUnderwriting.summary",
            "metadata.user.aiUnderwriting.cross_document",
            "metadata.user.aiUnderwriting.financial_risk",
            "metadata.user.aiUnderwriting.completeness",
            "metadata.user.aiUnderwriting.routing",
        ],
    }
    recipe.update(system_metadata=True)

    print(f"review recipe configured: {recipe.id}")
    print(
        "reviewer output is written by capture_reviewer_feedback to "
        f"{config.ground_truth_dataset}{config.ground_truth_folder} as a new item per packet/version; "
        "existing ground-truth items are never overwritten."
    )


if __name__ == "__main__":
    main()
