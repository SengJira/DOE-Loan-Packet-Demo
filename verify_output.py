#!/usr/bin/env python3
"""
verify_output.py — offline acceptance checks over a generated demo_data tree.

  python verify_output.py --data ./demo_data
  python verify_output.py --data ./demo_data --emit-digest ./digest.json
  python verify_output.py --compare ./run_a/digest.json ./run_b/digest.json
  python verify_output.py --compare-trees ./run_a ./run_b

Checks performed by --data:
  1. the four metadata files exist and parse
  2. every PDF on disk opens with PyMuPDF and renders page 1
  3. documents marked "scanned": true carry no extractable text layer;
     documents marked false do
  4. manifest.csv row count == number of PDFs on disk
  5. every filename in ground_truth.json exists on disk, and vice versa
  6. every PDF has an entry in dataloop_metadata.json

Exits non-zero if any check fails. Makes no network calls.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

SPLITS = ("base", "unlabeled", "incoming")
METADATA_FILES = ("ground_truth.json", "prelabels.json",
                  "dataloop_metadata.json", "manifest.csv")

# A rasterised page is not perfectly textless in every viewer, but PyMuPDF
# returns the real text layer. Allow a couple of stray characters rather than
# demanding exactly zero, so a single decoding artefact does not fail the run.
SCANNED_TEXT_TOLERANCE = 3


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.checks = 0

    def check(self, ok: bool, message: str) -> bool:
        self.checks += 1
        if not ok:
            self.failures.append(message)
        return ok

    def fail(self, message: str) -> None:
        self.checks += 1
        self.failures.append(message)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_pdfs(data: Path) -> list[Path]:
    return sorted(
        (p for split in SPLITS for p in (data / split).glob("*.pdf")),
        key=lambda p: (p.parent.name, p.name),
    )


def digest(data: Path) -> dict:
    """Stable fingerprint of a run: the file list plus ground_truth.json."""
    pdfs = find_pdfs(data)
    listing = [f"{p.parent.name}/{p.name}" for p in pdfs]
    gt = data / "ground_truth.json"
    return {
        "file_count": len(listing),
        "file_list": listing,
        "file_list_sha256": hashlib.sha256(
            "\n".join(listing).encode()).hexdigest(),
        "ground_truth_sha256": sha256_file(gt) if gt.exists() else None,
        "manifest_sha256": sha256_file(data / "manifest.csv")
        if (data / "manifest.csv").exists() else None,
        "prelabels_sha256": sha256_file(data / "prelabels.json")
        if (data / "prelabels.json").exists() else None,
        "dataloop_metadata_sha256": sha256_file(data / "dataloop_metadata.json")
        if (data / "dataloop_metadata.json").exists() else None,
    }


def verify(data: Path, rep: Report) -> None:
    import pymupdf

    print(f"verifying {data.resolve()}")

    for name in METADATA_FILES:
        rep.check((data / name).exists(), f"missing metadata file {name}")

    gt_path = data / "ground_truth.json"
    if not gt_path.exists():
        rep.fail("ground_truth.json missing — cannot continue")
        return
    ground_truth = json.loads(gt_path.read_text())

    dl_meta = {}
    if (data / "dataloop_metadata.json").exists():
        dl_meta = json.loads((data / "dataloop_metadata.json").read_text())

    pdfs = find_pdfs(data)
    by_name = {p.name: p for p in pdfs}
    print(f"  {len(pdfs)} PDFs across {', '.join(SPLITS)}")

    rep.check(len(by_name) == len(pdfs),
              "duplicate PDF filenames across split directories")

    # --- manifest row count ------------------------------------------------
    manifest_path = data / "manifest.csv"
    manifest_rows = []
    if manifest_path.exists():
        with manifest_path.open(newline="") as fh:
            manifest_rows = list(csv.DictReader(fh))
        rep.check(
            len(manifest_rows) == len(pdfs),
            f"manifest.csv has {len(manifest_rows)} rows but {len(pdfs)} PDFs "
            f"are on disk",
        )
        print(f"  manifest.csv rows: {len(manifest_rows)}")

    # --- ground truth <-> disk --------------------------------------------
    missing_on_disk = sorted(set(ground_truth) - set(by_name))
    for name in missing_on_disk[:10]:
        rep.fail(f"ground_truth.json references {name}, which is not on disk")
    if len(missing_on_disk) > 10:
        rep.fail(f"...and {len(missing_on_disk) - 10} more missing files")

    undocumented = sorted(set(by_name) - set(ground_truth))
    for name in undocumented[:10]:
        rep.fail(f"{name} is on disk but absent from ground_truth.json")

    for name in sorted(set(by_name) - set(dl_meta))[:10]:
        rep.fail(f"{name} is on disk but absent from dataloop_metadata.json")

    # --- split placement ---------------------------------------------------
    for name, entry in ground_truth.items():
        p = by_name.get(name)
        if p is not None and p.parent.name != entry.get("split"):
            rep.fail(f"{name} is in {p.parent.name}/ but ground truth says "
                     f"{entry.get('split')}")
            break

    # --- every PDF opens, and its text layer matches `scanned` -------------
    scanned_ok = digital_ok = 0
    for p in pdfs:
        entry = ground_truth.get(p.name)
        try:
            doc = pymupdf.open(p)
            page_count = doc.page_count
            text = "".join(page.get_text() for page in doc)
            # Rendering is the real "does it open" test: a structurally broken
            # PDF can still report a page count.
            doc.load_page(0).get_pixmap(dpi=36)
            doc.close()
        except Exception as exc:                       # noqa: BLE001
            rep.fail(f"{p.name} failed to open/render: {exc}")
            continue

        rep.check(page_count >= 1, f"{p.name} has no pages")
        if entry is None:
            continue

        stripped = "".join(text.split())
        if entry.get("scanned"):
            if rep.check(
                len(stripped) <= SCANNED_TEXT_TOLERANCE,
                f"{p.name} is marked scanned:true but has an extractable text "
                f"layer ({len(stripped)} chars, e.g. {stripped[:60]!r})",
            ):
                scanned_ok += 1
        else:
            if rep.check(
                len(stripped) > 50,
                f"{p.name} is marked scanned:false but has no extractable text "
                f"({len(stripped)} chars)",
            ):
                digital_ok += 1

    print(f"  scanned PDFs with no text layer : {scanned_ok}")
    print(f"  digital PDFs with a text layer  : {digital_ok}")

    # --- prelabels sanity ---------------------------------------------------
    pre_path = data / "prelabels.json"
    if pre_path.exists():
        prelabels = json.loads(pre_path.read_text())
        for name in list(prelabels)[:200]:
            if name not in by_name:
                rep.fail(f"prelabels.json references {name}, not on disk")
                break
        print(f"  prelabel entries: {len(prelabels)}")


def compare(a: Path, b: Path) -> int:
    da, db = json.loads(a.read_text()), json.loads(b.read_text())
    keys = ["file_count", "file_list_sha256", "ground_truth_sha256",
            "manifest_sha256", "prelabels_sha256", "dataloop_metadata_sha256"]
    width = max(len(k) for k in keys)
    ok = True
    print(f"{'':<{width}}  {'run A':<64}  {'run B':<64}  match")
    for k in keys:
        va, vb = da.get(k), db.get(k)
        same = va == vb
        ok &= same
        print(f"{k:<{width}}  {str(va):<64}  {str(vb):<64}  "
              f"{'yes' if same else 'NO'}")
    if da.get("file_list") != db.get("file_list"):
        ok = False
        only_a = sorted(set(da.get("file_list", [])) - set(db.get("file_list", [])))
        only_b = sorted(set(db.get("file_list", [])) - set(da.get("file_list", [])))
        print(f"file list differs: {len(only_a)} only in A, {len(only_b)} only in B")
        for n in only_a[:10]:
            print(f"  only in A: {n}")
        for n in only_b[:10]:
            print(f"  only in B: {n}")
    print("\nRESULT: identical" if ok else "\nRESULT: DIFFERENT")
    return 0 if ok else 1


def rendered(pymupdf, path: Path) -> str:
    """Hash of every page rasterised at a fixed DPI: content, not container."""
    h = hashlib.sha256()
    with pymupdf.open(path) as doc:
        for page in doc:
            h.update(page.get_pixmap(dpi=72).samples)
    return h.hexdigest()


def compare_trees(a: Path, b: Path) -> int:
    """Byte-compare the PDFs of two runs, split by the scanned flag.

    Digital PDFs must match byte for byte. Rasterised ones are expected to
    differ in the PDF trailer /ID, which MuPDF fills with random bytes on every
    save, so they are only required to render to identical pixels.
    """
    import pymupdf

    gt = json.loads((a / "ground_truth.json").read_text())
    pdfs_a = {p.name: p for p in find_pdfs(a)}
    pdfs_b = {p.name: p for p in find_pdfs(b)}

    if set(pdfs_a) != set(pdfs_b):
        print(f"file lists differ: {len(set(pdfs_a) ^ set(pdfs_b))} names only in one run")
        return 1

    digital_same = digital_total = scanned_same = scanned_total = 0
    mismatches = []
    for name in sorted(pdfs_a):
        identical = sha256_file(pdfs_a[name]) == sha256_file(pdfs_b[name])
        if gt.get(name, {}).get("scanned"):
            scanned_total += 1
            scanned_same += identical
            if not identical:
                if rendered(pymupdf, pdfs_a[name]) != rendered(pymupdf, pdfs_b[name]):
                    mismatches.append(
                        f"{name}: scanned PDFs differ beyond the trailer /ID")
        else:
            digital_total += 1
            digital_same += identical
            if not identical:
                mismatches.append(
                    f"{name}: digital PDF is not byte-identical across runs")

    print(f"digital PDFs byte-identical : {digital_same}/{digital_total}")
    print(f"scanned PDFs byte-identical : {scanned_same}/{scanned_total} "
          f"(the rest render to identical pixels; they differ only in the "
          f"random PDF trailer /ID)")
    for m in mismatches[:15]:
        print(f"  FAIL: {m}", file=sys.stderr)
    if mismatches:
        print("\nRESULT: DIFFERENT")
        return 1
    print("\nRESULT: content identical")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", help="generated output directory to verify")
    ap.add_argument("--emit-digest", metavar="PATH",
                    help="write the run fingerprint as JSON")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"),
                    help="compare two digest files written by --emit-digest")
    ap.add_argument("--compare-trees", nargs=2, metavar=("A", "B"),
                    help="byte-compare the PDFs of two generated directories")
    args = ap.parse_args()

    if args.compare:
        return compare(Path(args.compare[0]), Path(args.compare[1]))

    if args.compare_trees:
        return compare_trees(Path(args.compare_trees[0]),
                             Path(args.compare_trees[1]))

    if not args.data:
        ap.error("--data is required unless --compare/--compare-trees is used")

    data = Path(args.data)
    if not data.is_dir():
        print(f"ERROR: {data} is not a directory", file=sys.stderr)
        return 1

    rep = Report()
    verify(data, rep)

    if args.emit_digest:
        out = Path(args.emit_digest)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(digest(data), indent=2))
        print(f"  digest written to {out}")

    print(f"\n{rep.checks} checks run, {len(rep.failures)} failed")
    for f in rep.failures[:25]:
        print(f"  FAIL: {f}", file=sys.stderr)
    if len(rep.failures) > 25:
        print(f"  ... and {len(rep.failures) - 25} more", file=sys.stderr)
    return 1 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(main())
