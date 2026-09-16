#!/usr/bin/env python3
"""
upload_to_dataloop.py — push generated loan packets into a Dataloop project.

  python upload_to_dataloop.py --data ./demo_data --project "Loan-Docs-Demo"

What it does:
  1. creates (or gets) the project and a `loan-packets` dataset
  2. adds the extraction ontology to the dataset's default recipe
  3. uploads base/ and unlabeled/ under remote paths, with item metadata
     (loan_id, doc_type, scanned, split) so your DQL filters work immediately
  4. attaches model-style predictions from prelabels.json to the unlabeled
     items as item metadata, so a pipeline can route on confidence
  5. leaves incoming/ on disk — that is your live trigger folder for the demo

Note on step 4: predictions land in `metadata.user.prediction` rather than as
studio annotations. Field-level text annotations depend on how you configure
the PDF studio in your tenant, so create those interactively once and mirror
the resulting annotation JSON here if you want them pre-populated.

Verify every call against your tenant before the demo — dtlpy surfaces change.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import dtlpy as dl

# Fields the demo extracts, grouped by document type. These become the labels
# in the recipe ontology.
ONTOLOGY = {
    "loan_application": [
        "applicant_name", "loan_id", "application_date", "loan_amount",
        "loan_purpose", "property_address", "annual_income", "employer_name",
        "years_employed", "credit_score", "monthly_debt", "term_months",
    ],
    "pay_stub": [
        "employee_name", "employer_name", "pay_period_start", "pay_period_end",
        "gross_pay", "net_pay", "federal_tax", "ytd_gross", "employee_id",
    ],
    "bank_statement": [
        "account_holder", "bank_name", "account_number", "statement_period",
        "opening_balance", "closing_balance", "total_deposits", "total_withdrawals",
    ],
    "w2": [
        "employee_name", "employer_name", "tax_year", "wages_tips_other",
        "federal_income_tax_withheld", "social_security_wages", "employer_ein",
        "control_number",
    ],
    "id_verification": [
        "full_name", "date_of_birth", "document_number", "issuing_state",
        "issue_date", "expiry_date", "address", "verification_result",
    ],
}


def ensure_login():
    if dl.token_expired():
        dl.login()


def build_labels():
    """Hierarchical labels: doc_type.field — keeps the studio label picker sane."""
    labels = []
    for doc_type, fields in ONTOLOGY.items():
        for f in fields:
            labels.append(f"{doc_type}.{f}")
    labels.append("document_type")  # for whole-document classification
    return labels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="./demo_data")
    ap.add_argument("--project", default="Loan-Docs-Demo")
    ap.add_argument("--dataset", default="loan-packets")
    ap.add_argument("--splits", default="base,unlabeled",
                    help="which split folders to upload (incoming stays local)")
    args = ap.parse_args()

    data = Path(args.data)
    ensure_login()

    try:
        project = dl.projects.get(project_name=args.project)
        print(f"using existing project {args.project}")
    except Exception:
        project = dl.projects.create(project_name=args.project)
        print(f"created project {args.project}")

    try:
        dataset = project.datasets.get(dataset_name=args.dataset)
    except Exception:
        dataset = project.datasets.create(dataset_name=args.dataset)
        print(f"created dataset {args.dataset}")

    # --- ontology -------------------------------------------------------
    existing = {lbl.tag for lbl in dataset.labels}
    new_labels = [l for l in build_labels() if l not in existing]
    if new_labels:
        dataset.add_labels(label_list=new_labels)
        print(f"added {len(new_labels)} labels to the default recipe")

    # --- upload ---------------------------------------------------------
    meta = json.loads((data / "dataloop_metadata.json").read_text())

    for split in args.splits.split(","):
        folder = data / split.strip()
        pdfs = sorted(folder.glob("*.pdf"))
        if not pdfs:
            print(f"nothing in {folder}, skipping")
            continue
        print(f"uploading {len(pdfs)} files from {split}...")
        for p in pdfs:
            item_meta = meta.get(p.name, {})
            dataset.items.upload(
                local_path=str(p),
                remote_path=f"/{split}/{item_meta.get('user', {}).get('doc_type', 'misc')}",
                item_metadata=item_meta,
            )
        print(f"  {split} done")

    # --- predictions ----------------------------------------------------
    pre_path = data / "prelabels.json"
    if pre_path.exists():
        prelabels = json.loads(pre_path.read_text())
        filters = dl.Filters()
        filters.add(field="metadata.user.split", values="unlabeled")
        attached = 0
        for item in dataset.items.list(filters=filters).all():
            entry = prelabels.get(item.name)
            if not entry:
                continue
            preds = entry["predictions"]
            item.metadata.setdefault("user", {})["prediction"] = {
                k: v["value"] for k, v in preds.items()
            }
            item.metadata["user"]["confidence"] = {
                k: v["confidence"] for k, v in preds.items()
            }
            item.metadata["user"]["min_confidence"] = min(
                v["confidence"] for v in preds.values()
            )
            item.update()
            attached += 1
        print(f"attached predictions to {attached} items")

    print("\nReady. Useful DQL filters for the demo:")
    print('  low confidence  ->  {"metadata.user.min_confidence": {"$lt": 0.75}}')
    print('  scans only      ->  {"metadata.user.scanned": true}')
    print('  one packet      ->  {"metadata.user.loan_id": "LN-123456"}')


if __name__ == "__main__":
    main()
