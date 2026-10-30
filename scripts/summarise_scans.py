#!/usr/bin/env python3
"""Summarise preserved scan records without importing model or hardware code."""

import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def summarise(root=ROOT):
    demo = root / "examples" / "scans" / "demo"
    catalog = json.loads((demo / "catalog.json").read_text())
    rows = []
    for selector, item in enumerate(catalog["scans"], 1):
        records = json.loads((demo / "scans" / item["id"] / "records.json").read_text())
        images, runs = records["images"], records["runs"]
        originals = sum(image["role"] == "original" for image in images)
        crops = sum(image["role"] == "crop" for image in images)
        usable = sum(run["state"] == "usable" for run in runs)
        if any(run["state"] == "running" for run in runs):
            raise ValueError("Scan has an unterminated run: " + item["id"])
        row = dict(scan_selector=selector, scan_id=item["id"],
                   completed=item["display_time"], originals=originals, crops=crops,
                   full_frame_fallbacks=originals-crops, model_batches=len(runs),
                   schema_accepted_batches=usable, failed_batches=len(runs)-usable,
                   model_ids=";".join(sorted({run["model"] for run in runs})),
                   engine_ids=";".join(sorted({run["engine"] for run in runs})),
                   recorded_acquisition_seconds=item["acquisition_seconds"],
                   recorded_analysis_seconds=item["analysis_seconds"])
        if (originals, crops, len(runs), usable, len(runs)-usable) != (
                item["original_count"], item["crop_count"], item["run_count"],
                item["valid_batches"], item["failed_batches"]):
            raise ValueError("Catalog/record count mismatch: " + item["id"])
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "runs/scan-summary.csv")
    args = parser.parse_args()
    rows = summarise()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"scans": len(rows), "originals": sum(r["originals"] for r in rows),
                      "crops": sum(r["crops"] for r in rows),
                      "schema_accepted_batches": sum(r["schema_accepted_batches"] for r in rows),
                      "failed_batches": sum(r["failed_batches"] for r in rows),
                      "summary_csv": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
