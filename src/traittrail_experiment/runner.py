"""Send supplied images to a configured vision endpoint and retain the evidence."""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import shutil
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
import uuid

from PIL import Image

from .contract import FEATURES, output_schema
from .records import sha256, validate_record


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("The vision endpoint redirected; configure its final URL directly")


def strict_json(text):
    def reject(value):
        raise ValueError("Non-finite JSON number: " + value)
    return json.loads(text, parse_constant=reject)


def normalize_boxes(payload: dict, mode: str) -> dict:
    if mode == "normalized-xywh":
        return payload
    if mode != "qwen-1000-xyxy":
        raise ValueError("Unknown coordinate format")
    result = copy.deepcopy(payload)
    if not isinstance(result, dict) or not isinstance(result.get("observations"), list):
        raise ValueError("Expected an observations array before coordinate conversion")
    for o in result.get("observations", []):
        if not isinstance(o, dict) or not isinstance(o.get("evidence", []), list):
            raise ValueError("Expected observation objects with evidence arrays")
        for e in o.get("evidence", []):
            if not isinstance(e, dict):
                raise ValueError("Expected evidence objects")
            box = e.get("bbox")
            if not isinstance(box, list) or len(box) != 4 or any(type(v) is not int for v in box):
                raise ValueError("Expected integer Qwen [x1,y1,x2,y2] boxes")
            x1, y1, x2, y2 = box
            if not 0 <= x1 < x2 <= 1000 or not 0 <= y1 < y2 <= 1000:
                raise ValueError("Qwen box coordinates must be ordered and within 0–1000")
            e["bbox"] = [x1 / 1000, y1 / 1000, (x2 - x1) / 1000, (y2 - y1) / 1000]
    return result


def analyse(paths: list[Path], output: Path, *, model: str, base_url: str,
            specimen_id: str, bbox_format: str = "normalized-xywh", timeout: float = 300,
            max_tokens: int = 6000, response_format: str = "json-schema",
            reasoning_effort: str | None = None, hardware: str | None = None,
            prompt_path: Path | None = None, api_key_env: str = "TRAITTRAIL_API_KEY") -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use an HTTP(S) API base URL without credentials, query or fragment")
    if not paths or not model.strip() or not specimen_id.strip() or not 0 < timeout <= 3600 or max_tokens <= 0:
        raise ValueError("Images, model, specimen ID and positive limits are required")
    if bbox_format not in ("normalized-xywh", "qwen-1000-xyxy"):
        raise ValueError("Unknown coordinate format")
    if response_format not in ("json-schema", "json-object", "none"):
        raise ValueError("Unknown response format")
    prepared = []
    for path in paths:
        path = path.resolve()
        with Image.open(path) as image:
            if image.format not in ("JPEG", "PNG", "WEBP"):
                raise ValueError("Supply JPEG, PNG or WebP photographs")
            mime = Image.MIME[image.format]
            dimensions = image.size
            image.verify()
        prepared.append((path, mime, dimensions))
    # A new run never overwrites an earlier run or its original evidence.
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "images").mkdir()
    images, content = [], []
    for index, (path, mime, dimensions) in enumerate(prepared, 1):
        target = output / "images" / f"{index:02d}{path.suffix.lower()}"
        shutil.copyfile(path, target)
        image_id = f"image_{index}"
        images.append(dict(id=image_id, path=str(target.relative_to(output)), sha256=sha256(target),
                           width=dimensions[0], height=dimensions[1], credit=None, licence=None))
        content.append(dict(type="image_url", image_url=dict(url="data:" + mime + ";base64," + base64.b64encode(target.read_bytes()).decode())))
    schema = output_schema([i["id"] for i in images])
    base_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path else files("traittrail_experiment").joinpath("prompt.txt").read_text(encoding="utf-8")
    coordinates = "normalized [x,y,width,height] values between 0 and 1"
    if bbox_format == "qwen-1000-xyxy":
        coordinates = "integer [x1,y1,x2,y2] corner coordinates between 0 and 1000"
        box = schema["properties"]["observations"]["items"]["properties"]["evidence"]["items"]["properties"]["bbox"]
        box["items"].update(type="integer", maximum=1000)
    prompt = (base_prompt + "\nImage catalogue: " + json.dumps([{k: i[k] for k in ("id", "width", "height")} for i in images])
              + "\nVocabulary: " + json.dumps(FEATURES, ensure_ascii=False)
              + "\nEvidence boxes use " + coordinates + "."
              + "\nRequired JSON schema: " + json.dumps(schema, ensure_ascii=False))
    (output / "prompt.txt").write_text(prompt, encoding="utf-8", newline="")
    (output / "schema.json").write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8", newline="")
    payload = dict(model=model, messages=[dict(role="user", content=[dict(type="text", text=prompt), *content])],
                   temperature=0, max_tokens=max_tokens, stream=False)
    if response_format == "json-schema":
        payload["response_format"] = dict(type="json_schema", json_schema=dict(name="specimen_observation", strict=True, schema=schema))
    elif response_format == "json-object":
        payload["response_format"] = dict(type="json_object")
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort
    body = json.dumps(payload, ensure_ascii=False).encode()
    (output / "request.json").write_bytes(body)
    record = dict(record_version=1, run_id="run_" + uuid.uuid4().hex, specimen_id=specimen_id,
                  specimen_label=specimen_id, model=model, images=images, observation=None,
                  created=datetime.now(timezone.utc).isoformat(), historical=False, human_reviewed=False,
                  endpoint=base_url, bbox_format=bbox_format,
                  prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                  hardware_declared=hardware, hardware_verified=False,
                  settings=dict(temperature=0, max_tokens=max_tokens, response_format=response_format,
                                reasoning_effort=reasoning_effort), state="failed", errors=[])
    headers = {"Content-Type": "application/json"}
    key = os.environ.get(api_key_env)
    if key:
        headers["Authorization"] = "Bearer " + key
    started = time.monotonic()
    try:
        with build_opener(NoRedirect()).open(Request(url, body, headers), timeout=timeout) as response:
            raw = response.read(16_000_001)
        (output / "http-response.json").write_bytes(raw)
        if len(raw) > 16_000_000:
            raise ValueError("Response exceeded the 16 MB limit")
        data = strict_json(raw)
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object from the vision endpoint")
        record["model_reported"] = data.get("model")
        record["usage"] = data.get("usage")
        choices = data.get("choices", [])
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError("Expected one model response")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError("Expected a model message object")
        text = message.get("content")
        if not isinstance(text, str):
            raise ValueError("The model did not return text")
        (output / "raw-response.txt").write_text(text, encoding="utf-8", newline="")
        if choice.get("finish_reason") != "stop" or message.get("tool_calls"):
            raise ValueError("Model response was incomplete or requested tools")
        record["observation"] = normalize_boxes(strict_json(text), bbox_format)
        errors, warnings = validate_record(record)
        record.update(validation_errors=errors, validation_warnings=warnings)
        if errors:
            record["errors"].extend(errors)
            (output / "invalid-observation.json").write_text(json.dumps(record["observation"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="")
            record["observation"] = None
        else:
            record["state"] = "recorded"
    except HTTPError as error:
        (output / "http-error.txt").write_bytes(error.read(16_000_000))
        record["errors"].append("Vision endpoint returned HTTP " + str(error.code))
    except (OSError, ValueError, KeyError, TypeError) as error:
        record["errors"].append(str(error))
    finally:
        record["wall_seconds"] = round(time.monotonic() - started, 3)
        (output / "record.json").write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="")
    return record
