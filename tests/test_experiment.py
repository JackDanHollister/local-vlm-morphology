from __future__ import annotations

import copy
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

from PIL import Image
import pytest

from traittrail_experiment.cli import main
from traittrail_experiment.contract import FEATURES
from traittrail_experiment.records import load_catalog, load_run
from traittrail_experiment.report import build_graph, unique_records, write_report
from traittrail_experiment.runner import analyse, normalize_boxes

ROOT = Path(__file__).resolve().parents[1]


def payload():
    return dict(summary="Synthetic test response", next_view_id=None, next_view_reason="",
                observations=[dict(feature_id=key, status="unknown", value=None,
                                   visibility="insufficient_detail", confidence=None,
                                   evidence=[], note="Not resolved in this test fixture") for key in FEATURES])


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "input.png"
    Image.new("RGB", (40, 30), "grey").save(path)
    return path


@pytest.fixture
def endpoint():
    state = dict(response=payload(), finish="stop", status=200)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["path"] = self.path
            state["request"] = self.rfile.read(int(self.headers["Content-Length"]))
            state["authorization"] = self.headers.get("Authorization")
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            content = state.get("text", json.dumps(state["response"]))
            self.wfile.write(json.dumps(dict(model="synthetic-test-model", choices=[dict(
                finish_reason=state["finish"], message=dict(content=content))])).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1", state
    server.shutdown()
    server.server_close()
    thread.join()


def test_all_historical_examples_have_full_inputs_and_valid_contract():
    records = load_catalog(ROOT / "examples/catalog.json")
    assert len(records) == 12
    assert len({r["specimen_id"] for r in records}) == 5
    assert len({i["sha256"] for r in records for i in r["images"]}) == 14
    assert all(not r["validation_errors"] for r in records)
    assert all(len(r["observation"]["observations"]) == 16 for r in records)


def test_catalog_rejects_changed_source_file(tmp_path):
    catalog = json.loads((ROOT / "examples/catalog.json").read_text())
    catalog["runs"] = catalog["runs"][:1]
    catalog["runs"][0]["result"] = "changed.json"
    (tmp_path / "changed.json").write_text("{}")
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog))
    with pytest.raises(ValueError, match="Changed source"):
        load_catalog(path)


def test_recorded_run_keeps_request_prompt_images_and_raw_reply(tmp_path, image, endpoint, monkeypatch):
    url, state = endpoint
    monkeypatch.setenv("TRAITTRAIL_API_KEY", "synthetic-test-key")
    output = tmp_path / "run"
    record = analyse([image], output, model="synthetic-model", base_url=url, specimen_id="test:one")
    assert record["state"] == "recorded"
    assert state["path"] == "/v1/chat/completions"
    assert state["authorization"] == "Bearer synthetic-test-key"
    assert (output / "request.json").read_bytes() == state["request"]
    assert (output / "images/01.png").read_bytes() == image.read_bytes()
    assert (output / "prompt.txt").read_text() == json.loads(state["request"])["messages"][0]["content"][0]["text"]
    assert json.loads((output / "raw-response.txt").read_text()) == payload()
    assert all("synthetic-test-key" not in p.read_text(errors="replace") for p in output.glob("*.json"))
    assert load_run(output / "record.json")["run_id"] == record["run_id"]


def test_multiple_views_and_native_qwen_boxes_retain_instance_identity(tmp_path, image, endpoint):
    url, state = endpoint
    second = tmp_path / "second.png"
    Image.new("RGB", (80, 60), "blue").save(second)
    answer = payload()
    legs = next(o for o in answer["observations"] if o["feature_id"] == "legs.visible_count")
    legs.update(status="observed", value=2, visibility="clear", confidence=0.8, evidence=[
        dict(image_id="image_1", bbox=[100, 200, 300, 400], instance_id="leg-a", description="First visible leg"),
        dict(image_id="image_2", bbox=[200, 300, 400, 500], instance_id="leg-a", description="Same leg in second view"),
        dict(image_id="image_2", bbox=[600, 300, 800, 500], instance_id="leg-b", description="Second visible leg")])
    state["response"] = answer
    output = tmp_path / "multiview"
    record = analyse([image, second], output, model="synthetic-qwen", base_url=url,
                     specimen_id="test:one", bbox_format="qwen-1000-xyxy", response_format="json-object")
    assert record["state"] == "recorded"
    assert len(record["images"]) == 2
    result = next(o for o in record["observation"]["observations"] if o["feature_id"] == "legs.visible_count")
    assert result["evidence"][0]["bbox"] == [0.1, 0.2, 0.2, 0.2]
    assert {e["instance_id"] for e in result["evidence"]} == {"leg-a", "leg-b"}
    assert json.loads((output / "raw-response.txt").read_text()) == answer


@pytest.mark.parametrize("text", ["{broken", '{"confidence":NaN}'])
def test_malformed_reply_is_retained_as_failure(tmp_path, image, endpoint, text):
    url, state = endpoint
    state["text"] = text
    output = tmp_path / "failed"
    record = analyse([image], output, model="synthetic-model", base_url=url, specimen_id="test:one")
    assert record["state"] == "failed"
    assert (output / "raw-response.txt").read_text() == text
    assert (output / "record.json").is_file()
    assert record["errors"]


def test_truncated_answer_cannot_be_recorded_as_success(tmp_path, image, endpoint):
    url, state = endpoint
    state["finish"] = "length"
    record = analyse([image], tmp_path / "failed", model="synthetic-model", base_url=url, specimen_id="test:one")
    assert record["state"] == "failed"
    assert "incomplete" in record["errors"][0]


def test_invalid_structure_stays_failed_and_can_be_reviewed(tmp_path, image, endpoint):
    url, state = endpoint
    state["response"] = {"observations": ["not an observation"]}
    output = tmp_path / "invalid"
    record = analyse([image], output, model="synthetic-model", base_url=url, specimen_id="test:one")
    assert record["state"] == "failed"
    assert record["observation"] is None
    assert (output / "invalid-observation.json").is_file()
    loaded = load_run(output / "record.json")
    report = write_report([loaded], tmp_path / "failed-review")
    assert report.is_file()
    assert not json.loads((report.parent / "graph.json").read_text())["feature_groups"]


def test_http_failure_keeps_error_and_record(tmp_path, image, endpoint):
    url, state = endpoint
    state["status"] = 401
    output = tmp_path / "failed"
    record = analyse([image], output, model="synthetic-model", base_url=url, specimen_id="test:one")
    assert record["state"] == "failed"
    assert (output / "http-error.txt").is_file()
    assert (output / "record.json").is_file()


def test_new_run_cannot_overwrite_saved_directory(tmp_path, image):
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "original.txt"
    marker.write_text("keep me")
    with pytest.raises(FileExistsError):
        analyse([image], output, model="test", base_url="http://127.0.0.1:1/v1", specimen_id="test:one")
    assert marker.read_text() == "keep me"


def test_qwen_coordinates_are_converted_once_without_mutating_raw_reply():
    raw = payload()
    raw["observations"][0]["evidence"] = [dict(bbox=[100, 200, 400, 600])]
    result = normalize_boxes(raw, "qwen-1000-xyxy")
    assert result["observations"][0]["evidence"][0]["bbox"] == [0.1, 0.2, 0.3, 0.4]
    assert raw["observations"][0]["evidence"][0]["bbox"] == [100, 200, 400, 600]
    raw["observations"][0]["evidence"][0]["bbox"] = [400, 200, 100, 600]
    with pytest.raises(ValueError, match="ordered"):
        normalize_boxes(raw, "qwen-1000-xyxy")


def test_graph_counts_specimens_once_despite_multiple_runs():
    records = load_catalog(ROOT / "examples/catalog.json")
    first = next(r for r in records if r["run_id"] == "run_517a20489a604ff5")
    repeat = copy.deepcopy(first)
    repeat["run_id"] = "synthetic-repeat"
    graph = build_graph([first, repeat, first])
    group = next(g for g in graph["feature_groups"] if g["feature_id"] == "legs.visible_count")
    assert group["specimen_count"] == 1
    assert len(group["specimens"][0]["runs"]) == 2
    assert len([n for n in graph["nodes"] if n["kind"] == "run"]) == 2
    repeat["run_id"] = first["run_id"]
    repeat["model"] = "conflicting-model"
    with pytest.raises(ValueError, match="Conflicting"):
        unique_records([first, repeat])


def test_portable_report_has_all_answers_and_escapes_model_text(tmp_path):
    records = load_catalog(ROOT / "examples/catalog.json")
    records[0]["observation"]["observations"][0]["note"] = "</script><script>untrusted()</script>"
    report = write_report(records, tmp_path / "report")
    assert "</script><script>untrusted()" not in report.read_text()
    with (report.parent / "observations.csv").open() as stream:
        assert len(list(csv.DictReader(stream))) == 192
    exported = json.loads((report.parent / "records.json").read_text())
    assert all(not Path(i["path"]).is_absolute() and (report.parent / i["path"]).exists() for r in exported for i in r["images"])
    assert len(list((report.parent / "images").iterdir())) == 14


def test_colour_group_order_does_not_change_shared_pattern():
    first = load_catalog(ROOT / "examples/catalog.json")[0]
    other = copy.deepcopy(first)
    other.update(run_id="synthetic-colours", specimen_id="test:another")
    for record, colours in [(first, ["black", "yellow"]), (other, ["yellow", "black"])]:
        next(o for o in record["observation"]["observations"] if o["feature_id"] == "body.colours")["value"] = colours
    groups = [g for g in build_graph([first, other])["feature_groups"] if g["feature_id"] == "body.colours"]
    assert len(groups) == 1 and groups[0]["specimen_count"] == 2
    assert next(o for o in other["observation"]["observations"] if o["feature_id"] == "body.colours")["value"] == ["yellow", "black"]


def test_cli_offline_demo_and_check_work_without_a_model(tmp_path):
    catalog = str(ROOT / "examples/catalog.json")
    assert main(["check", "--catalog", catalog]) == 0
    assert main(["demo", "--catalog", catalog, "--output", str(tmp_path / "demo")]) == 0
    assert (tmp_path / "demo/index.html").exists()
