"""Load immutable examples and current runs without conflating specimen IDs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from .contract import validate


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def duplicate_box_warnings(payload: dict) -> list[str]:
    warnings = []
    for o in payload.get("observations", []):
        if o.get("status") != "observed" or not o.get("feature_id", "").endswith("count"):
            continue
        evidence = o.get("evidence", [])
        boxes = {tuple(e["bbox"]) for e in evidence}
        if type(o.get("value")) is int and o["value"] > 1 and len(boxes) < o["value"]:
            warnings.append(o["feature_id"] + ": claimed instances share a box")
    return warnings


def validate_record(record: dict) -> tuple[list[str], list[str]]:
    images = record["images"]
    errors = validate(record["observation"], [i["id"] for i in images])
    warnings = duplicate_box_warnings(record["observation"]) if not errors else []
    return errors, warnings


def load_catalog(path: Path) -> list[dict]:
    path = path.resolve()
    catalog = read_json(path)
    root = path.parent
    records = []
    for entry in catalog["runs"]:
        source = (root / entry["result"]).resolve()
        if not source.is_relative_to(root):
            raise ValueError("Catalog result path leaves its directory")
        if sha256(source) != entry["result_sha256"]:
            raise ValueError("Changed source result: " + entry["result"])
        raw = read_json(source)
        style = entry["format"]
        if style == "api":
            observation = raw["observation"]
            model, run_id = raw["model"], raw["run_id"]
        elif style == "scanner":
            observation = raw["result"]
            model, run_id = raw["model"], raw["id"]
        elif style == "annotations":
            values = {}
            for annotation in raw["data"]:
                for text in annotation["attributes"]["oa:hasBody"]["oa:value"]:
                    o = json.loads(text)
                    previous = values.get(o["feature_id"])
                    if previous and any(previous[k] != o[k] for k in ("value", "status", "evidence")):
                        raise ValueError("Conflicting repeated annotation: " + o["feature_id"])
                    values[o["feature_id"]] = o
            models = {o["model"] for o in values.values()}
            runs = {o["run_id"] for o in values.values()}
            if len(models) != 1 or len(runs) != 1:
                raise ValueError("Annotations mix model runs")
            model, run_id = models.pop(), runs.pop()
            observation = dict(summary="Saved annotation export", observations=[
                {k: o[k] for k in ("feature_id", "status", "value", "visibility", "confidence", "evidence", "note")}
                for o in values.values()], next_view_id=None, next_view_reason="")
        else:
            raise ValueError("Unknown catalog format: " + style)
        if model != entry["model"] or run_id != entry["run_id"]:
            raise ValueError("Catalog model/run metadata does not match source")
        images = []
        for metadata in entry["images"]:
            image = (root / metadata["path"]).resolve()
            if not image.is_relative_to(root) or sha256(image) != metadata["sha256"]:
                raise ValueError("Changed or invalid source image: " + metadata["path"])
            with Image.open(image) as im:
                width, height = im.size
                im.verify()
            images.append(metadata | {"path": str(image), "width": width, "height": height})
        record = dict(record_version=1, specimen_id=entry["specimen_id"],
                      specimen_label=entry["specimen_label"], model=model, run_id=run_id,
                      images=images, observation=observation, source_result=str(source),
                      historical=True, human_reviewed=False)
        errors, warnings = validate_record(record)
        record.update(validation_errors=errors, validation_warnings=warnings)
        records.append(record)
    return records


def load_run(path: Path) -> dict:
    path = path.resolve()
    data = read_json(path)
    if data.get("record_version") != 1:
        raise ValueError("Expected a current experiment record.json, not an archived API export")
    data["images"] = [i | {"path": str((path.parent / i["path"]).resolve())} for i in data["images"]]
    for image in data["images"]:
        if sha256(Path(image["path"])) != image["sha256"]:
            raise ValueError("Input image checksum changed")
    if data.get("observation") is None:
        data.setdefault("validation_errors", ["Run has no parsed observation"])
        return data
    errors, warnings = validate_record(data)
    data.update(validation_errors=errors, validation_warnings=warnings)
    return data
