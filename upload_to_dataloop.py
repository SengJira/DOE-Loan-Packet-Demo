#!/usr/bin/env python3
"""
upload_to_dataloop.py — push generated loan packets into a Dataloop project.

  python upload_to_dataloop.py --data ./demo_data --project "Loan-Docs-Demo"
  python upload_to_dataloop.py --data ./demo_data --dry-run

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

--dry-run validates the input directory, prints the dataset/ontology/upload
actions that would be performed, and makes no network calls at all (dtlpy is
not even imported). Exits non-zero if the input directory is unusable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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


def load_dtlpy():
    import dtlpy as dl

    return dl


def ensure_login(dl):
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
    ap.add_argument("--dry-run", action="store_true",
                    help="validate inputs and print the planned actions without "
                         "contacting Dataloop")
    args = ap.parse_args()

    data = Path(args.data)

    if args.dry_run:
        return dry_run(args, data)

    dl = load_dtlpy()
    ensure_login(dl)

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


def dry_run(args, data: Path) -> int:
    """Validate the generated dataset and print the plan. No network, no dtlpy."""
    errors = []
    splits = [s.strip() for s in args.splits.split(",") if s.strip()]

    print("DRY RUN — no network calls will be made")
    print(f"data directory : {data.resolve()}")
    print(f"project        : {args.project}")
    print(f"dataset        : {args.dataset}")
    print(f"splits         : {', '.join(splits)}\n")

    if not data.is_dir():
        print(f"ERROR: data directory {data} does not exist", file=sys.stderr)
        return 1

    def load(name, required=True):
        p = data / name
        if not p.exists():
            if required:
                errors.append(f"missing {name}")
            return None
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError as exc:
            errors.append(f"{name} is not valid JSON: {exc}")
            return None

    meta = load("dataloop_metadata.json")
    ground_truth = load("ground_truth.json")
    prelabels = load("prelabels.json", required=False)
    if not (data / "manifest.csv").exists():
        errors.append("missing manifest.csv")

    print("PLAN")
    print(f"  project.get_or_create   {args.project!r}")
    print(f"  dataset.get_or_create   {args.dataset!r}")
    labels = build_labels()
    print(f"  recipe.add_labels       {len(labels)} labels "
          f"({len(ONTOLOGY)} doc types + document_type)")
    for doc_type, fields in ONTOLOGY.items():
        print(f"      {doc_type:<18} {len(fields)} fields")

    total = 0
    remote_paths = {}
    for split in splits:
        folder = data / split
        if not folder.is_dir():
            errors.append(f"split folder {split} does not exist")
            continue
        pdfs = sorted(folder.glob("*.pdf"))
        total += len(pdfs)
        if not pdfs:
            print(f"  items.upload            {split}: nothing to upload, would skip")
            continue
        for p in pdfs:
            item_meta = (meta or {}).get(p.name)
            if item_meta is None:
                errors.append(f"{p.name} has no entry in dataloop_metadata.json")
                item_meta = {}
            if ground_truth is not None and p.name not in ground_truth:
                errors.append(f"{p.name} has no entry in ground_truth.json")
            rp = f"/{split}/{item_meta.get('user', {}).get('doc_type', 'misc')}"
            remote_paths[rp] = remote_paths.get(rp, 0) + 1
        print(f"  items.upload            {split}: {len(pdfs)} PDFs")
    for rp in sorted(remote_paths):
        print(f"      {rp:<34} {remote_paths[rp]:>4} items")

    if prelabels is not None:
        unlabeled = {
            name for name, entry in (ground_truth or {}).items()
            if entry.get("split") == "unlabeled"
        }
        would_attach = len(unlabeled & set(prelabels)) if unlabeled else 0
        print(f"  item.update             would attach predictions to "
              f"{would_attach} unlabeled items")
        for name, entry in prelabels.items():
            preds = entry.get("predictions")
            if not isinstance(preds, dict) or not preds:
                errors.append(f"prelabels.json entry {name} has no predictions")
                break
            if any("value" not in v or "confidence" not in v for v in preds.values()):
                errors.append(f"prelabels.json entry {name} is missing value/confidence")
                break

    print(f"\n{total} files would be uploaded; incoming/ stays on disk.")

    if errors:
        print(f"\nVALIDATION FAILED ({len(errors)} problem(s)):", file=sys.stderr)
        for e in errors[:20]:
            print(f"  - {e}", file=sys.stderr)
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more", file=sys.stderr)
        return 1

    print("validation OK")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
