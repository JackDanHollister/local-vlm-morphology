"""Recorded-image Codex adapter. The observer is never a hardware controller."""

import json
import os
from pathlib import Path
import signal
import subprocess
import time

from .store import canonical, write_new
from .process import communicate


DISABLED = (
    "shell_tool", "unified_exec", "apps", "plugins", "hooks", "memories",
    "multi_agent", "multi_agent_v2", "browser_use", "browser_use_external",
    "computer_use", "image_generation", "view_image", "skill_search",
    "code_mode", "code_mode_host", "goals", "sleep_tool",
)


def inspect_events(lines):
    """Reject tool activity, API failures and incomplete turns, preserving the stream."""
    errors, completed, usage, started = [], False, {}, False
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            errors.append("Malformed event stream")
            continue
        if event.get("type") in {"error", "turn.failed"}:
            errors.append("Observer reported an execution error")
        if event.get("type") == "turn.completed":
            completed, usage = True, event.get("usage", {})
        if event.get("type") == "turn.started":
            started = True
        item = event.get("item", {})
        # CLI 0.154 emits this startup diagnostic as an error item even though
        # inference succeeds. The unavailable tool is deliberate isolation.
        if not started and item.get("type") == "error" and item.get("message") == "Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; enable `features.code_mode_host` and install `codex-code-mode-host`.":
            continue
        if item and item.get("type") not in {"agent_message", "reasoning"}:
            errors.append("Observer used a tool or unexpected item: " + str(item.get("type")))
    if not completed:
        errors.append("No completed observer turn")
    return errors, usage


def observe(store, specimen_id, image_ids, model, *, next_views=(), timeout=300, cancel=None, on_started=None):
    run = store.begin_run(specimen_id, image_ids, model, next_views=next_views)
    if on_started:on_started(run['id'])
    folder = store.root / "runs" / run["id"]
    work = folder / "empty-workdir"
    work.mkdir()
    command = ["codex", "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
               "--skip-git-repo-check", "--sandbox", "read-only", "--model", model,
               "-C", str(work), "--json", "--color", "never",
               "--output-schema", str(folder / "schema.json"),
               "-o", str(folder / "emitted-response.txt"),
               "-c", 'web_search="disabled"', "-c", 'model_reasoning_effort="medium"',
               "-c", 'project_doc_max_bytes=0', "--enable", "skip_host_skill_discovery"]
    command.extend(["-c", "suppress_unstable_features_warning=true"])
    for flag in DISABLED:
        command.extend(["--disable", flag])
    for image_id in image_ids:
        command.extend(["--image", str(store.image_path(image_id, verify=True))])
    command.append("-")
    version = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    write_new(folder / "execution.json", (canonical({"argv": command, "cli_version": version,
              "timeout_seconds": timeout, "reasoning_effort": "medium"})+"\n").encode())
    error, exit_code, usage = None, None, {}
    started = time.monotonic()
    try:
        with (folder / "events.jsonl").open("xb") as stdout, (folder / "stderr.log").open("xb") as stderr:
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, start_new_session=True)
            error=communicate(proc,(folder / "prompt.txt").read_bytes(),timeout,cancel)
            exit_code = proc.returncode
        errors, usage = inspect_events((folder / "events.jsonl").read_text().splitlines())
        if exit_code != 0:
            errors.append(f"Codex exit code {exit_code}")
        if errors:
            error = "; ".join(filter(None, [error, *errors]))
    except OSError as exc:
        error = str(exc)
    emitted = folder / "emitted-response.txt"
    raw = emitted.read_text() if emitted.exists() else ""
    return store.finish_run(run["id"], raw, execution_error=error, telemetry={
        "wall_seconds": round(time.monotonic()-started, 3), "usage": usage,
        "exit_code": exit_code, "cli_version": version,
        "model_requested": model, "model_resolved": None,
        "model_note": "CLI request recorded; backend checkpoint not independently reported"})
