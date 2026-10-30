"""Command-line entry points for replay, local inference and evidence export."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

from .records import load_catalog, load_run
from .report import write_report
from .runner import analyse


def project_root() -> Path:
    for path in [Path.cwd(), *Path.cwd().parents, Path(__file__).resolve().parents[2]]:
        if (path / "examples/catalog.json").is_file():
            return path
    raise ValueError("Run from the project checkout, or supply --catalog with its full path")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="TraitTrail specimen-image experiment")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ["demo", "check"]:
        p = sub.add_parser(command, help="Review saved examples offline" if command == "demo" else "Check saved input hashes and observation structure")
        p.add_argument("--catalog", type=Path)
        if command == "demo":
            p.add_argument("--output", type=Path, default=Path("runs/demo"))
    p = sub.add_parser("analyse", help="Analyse supplied images using a configured vision endpoint")
    p.add_argument("images", nargs="+", type=Path)
    p.add_argument("--model", required=True)
    p.add_argument("--base-url", required=True, help="API base URL, for example http://127.0.0.1:8000/v1")
    p.add_argument("--specimen-id", required=True, help="Stable ID for the specimen shared by these views")
    p.add_argument("--output", type=Path, required=True, help="A new run directory")
    p.add_argument("--bbox-format", choices=["normalized-xywh", "qwen-1000-xyxy"], default="normalized-xywh")
    p.add_argument("--response-format", choices=["json-schema", "json-object", "none"], default="json-schema")
    p.add_argument("--timeout", type=float, default=300)
    p.add_argument("--max-tokens", type=int, default=6000)
    p.add_argument("--reasoning-effort", choices=["none", "low", "medium", "high"])
    p.add_argument("--hardware", help="User-supplied server hardware description; not independently verified")
    p.add_argument("--prompt", type=Path, help="Optional prompt file; exact final prompt is retained")
    p.add_argument("--api-key-env", default="TRAITTRAIL_API_KEY", help="Environment-variable name, never the key itself")
    for command in ["view", "graph"]:
        p = sub.add_parser(command, help="Review current runs and export a linked feature graph")
        p.add_argument("records", nargs="+", type=Path, help="record.json files from current runs")
        p.add_argument("--output", required=True, type=Path)
    p = sub.add_parser("figures", help="Rebuild the approved figures into a separate output directory")
    p.add_argument("--output", type=Path, default=Path("runs/figures"))
    p.add_argument("--font", type=Path)
    p.add_argument("--bold-font", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command in ["demo", "check"]:
            catalog = args.catalog or project_root() / "examples/catalog.json"
            records = load_catalog(catalog)
            errors = sum(len(r["validation_errors"]) for r in records)
            warnings = sum(len(r["validation_warnings"]) for r in records)
            print(f'{len(records)} saved runs; {len({r["specimen_id"] for r in records})} specimens; {errors} structural errors; {warnings} warnings. Predictions remain unreviewed.')
            if args.command == "demo":
                print("Open " + str(write_report(records, args.output)))
            return 1 if errors else 0
        if args.command == "analyse":
            record = analyse(args.images, args.output, model=args.model, base_url=args.base_url,
                             specimen_id=args.specimen_id, bbox_format=args.bbox_format, timeout=args.timeout,
                             max_tokens=args.max_tokens, response_format=args.response_format,
                             reasoning_effort=args.reasoning_effort, hardware=args.hardware,
                             prompt_path=args.prompt, api_key_env=args.api_key_env)
            print(record["state"] + ": " + str(args.output.resolve() / "record.json"))
            for error in record["errors"]:
                print(error, file=sys.stderr)
            return 0 if record["state"] == "recorded" else 1
        if args.command in ["view", "graph"]:
            records = [load_run(path / "record.json" if path.is_dir() else path) for path in args.records]
            print("Open " + str(write_report(records, args.output)))
            return 0
        if args.command == "figures":
            root = project_root()
            output = args.output.resolve()
            if output == root or output in root.parents:
                raise ValueError("Choose a separate generated-figure output directory")
            options = ["--output", str(output)]
            if args.font:
                options += ["--font", str(args.font)]
            if args.bold_font:
                options += ["--bold-font", str(args.bold_font)]
            for script in ["build_qwen_figures.py", "build_other_model_figures.py"]:
                subprocess.run([sys.executable, str(root / "scripts" / script), *options], check=True)
            print("Rebuilt figures in " + str(output))
            return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        print("Error: " + str(error), file=sys.stderr)
        return 1
    return 0
