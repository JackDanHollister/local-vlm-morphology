"""Portable image review, CSV observations and a graph with explicit evidence links."""
from __future__ import annotations

import copy
import csv
from importlib.resources import files
import json
from pathlib import Path
import shutil

from PIL import Image

from .contract import canonical
from .records import sha256


def unique_records(records: list[dict]) -> list[dict]:
    selected = {}
    for record in records:
        key = record["run_id"]
        fingerprint = canonical({k: record.get(k) for k in ("model", "specimen_id", "observation")} | {
            "inputs": [{k: image[k] for k in ("id", "sha256")} for image in record["images"]]})
        if key in selected and selected[key][0] != fingerprint:
            raise ValueError("Conflicting records share run ID: " + key)
        selected[key] = (fingerprint, record)
    return [item[1] for item in selected.values()]


def build_graph(records: list[dict]) -> dict:
    nodes, links, patterns = {}, set(), {}
    for record in unique_records(records):
        sid = "specimen:" + record["specimen_id"]
        rid = "run:" + record["run_id"]
        nodes[sid] = dict(id=sid, kind="specimen", label=record["specimen_label"])
        nodes[rid] = dict(id=rid, kind="run", label=record["model"],
                          validation_errors=record.get("validation_errors", []), human_reviewed=False)
        links.add((sid, rid, "has_run"))
        image_ids = {}
        for image in record["images"]:
            iid = "image:" + record["specimen_id"] + ":" + image["sha256"]
            nodes[iid] = dict(id=iid, kind="image", label=image["id"], sha256=image["sha256"])
            links.update({(sid, iid, "has_image"), (rid, iid, "used_image")})
            image_ids[image["id"]] = iid
        observations = [] if record.get("validation_errors") else (record.get("observation") or {}).get("observations", [])
        for o in observations:
            oid = rid + ":" + o["feature_id"]
            fid = "feature:" + o["feature_id"]
            nodes[fid] = dict(id=fid, kind="feature", label=o["feature_id"])
            nodes[oid] = dict(id=oid, kind="observation", label=o["feature_id"], status=o["status"],
                              value=o["value"], confidence=o["confidence"], note=o["note"],
                              evidence=o["evidence"], human_reviewed=False)
            links.update({(rid, oid, "reports"), (oid, fid, "about_feature")})
            for e in o["evidence"]:
                if e["image_id"] in image_ids:
                    links.add((oid, image_ids[e["image_id"]], "supported_by"))
            if o["status"] == "observed" and not record.get("validation_errors"):
                # Colour answers are sets; preserve the source order in observations,
                # but do not split graph groups just because two models list them differently.
                value = sorted(o["value"]) if o["feature_id"] == "body.colours" else o["value"]
                key = (o["feature_id"], canonical(value))
                if key not in patterns:
                    patterns[key] = dict(feature_id=o["feature_id"], value=value, specimens={})
                patterns[key]["specimens"].setdefault(sid, set()).add(rid)
    groups = []
    for item in patterns.values():
        groups.append(item | {"specimens": [dict(id=sid, runs=sorted(runs)) for sid, runs in item["specimens"].items()],
                              "specimen_count": len(item["specimens"])})
    return dict(schema="traittrail-evidence-graph-v1", nodes=list(nodes.values()),
                links=[dict(source=a, target=b, relation=c) for a, b, c in sorted(links)],
                feature_groups=sorted(groups, key=lambda g: (g["feature_id"], canonical(g["value"]))),
                note="Groups count distinct catalogued specimens, not views or repeated model runs. Unreviewed predictions; no consensus or accuracy score is inferred.")


def write_report(records: list[dict], output: Path) -> Path:
    records = unique_records(records)
    if not records:
        raise ValueError("No runs to review")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    pictures = output / "images"
    pictures.mkdir(exist_ok=True)
    exported = copy.deepcopy(records)
    for record in exported:
        record.pop("source_result", None)
        if record.get("validation_errors"):
            # Preserve malformed responses in the run folder; only valid answers enter the viewer.
            record["observation"] = None
        for image in record["images"]:
            source = Path(image["path"])
            if sha256(source) != image["sha256"]:
                raise ValueError("Image bytes changed before report creation")
            with Image.open(source) as photo:
                if photo.format not in ("JPEG", "PNG", "WEBP"):
                    raise ValueError("Unsupported review image format")
                suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}[photo.format]
            target = pictures / (image["sha256"] + suffix)
            if target.exists() and sha256(target) != image["sha256"]:
                raise ValueError("Report image destination contains different bytes")
            if not target.exists():
                shutil.copyfile(source, target)
            image["path"] = str(target.relative_to(output))
    graph = build_graph(exported)
    (output / "graph.json").write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="")
    (output / "records.json").write_text(json.dumps(exported, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="")
    with (output / "observations.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["specimen_id", "run_id", "model", "feature_id", "status", "value_json", "confidence", "evidence_json", "note"])
        for r in exported:
            for o in (r.get("observation") or {}).get("observations", []):
                # Prefix spreadsheet control characters in free text only; the
                # JSON records remain the unmodified source of exported answers.
                safe = lambda text: "'" + text if str(text).startswith(("=", "+", "-", "@")) else text
                writer.writerow([safe(r["specimen_id"]), r["run_id"], safe(r["model"]), o["feature_id"], o["status"],
                                 json.dumps(o["value"], ensure_ascii=False), o["confidence"],
                                 json.dumps(o["evidence"], ensure_ascii=False), safe(o["note"])])
    template = files("traittrail_experiment").joinpath("viewer.html").read_text(encoding="utf-8")
    data = json.dumps(dict(records=exported, graph=graph), ensure_ascii=False).replace("<", "\\u003c")
    (output / "index.html").write_text(template.replace("__TRAITTRAIL_DATA__", data), encoding="utf-8", newline="")
    return output / "index.html"
