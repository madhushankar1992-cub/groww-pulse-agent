"""Phase 7: unattended weekly run that stops before anything irreversible.

Sequence: config check -> (opt-in) fetch -> import -> snapshot -> preview. The job never sends
email or edits a Doc. It writes a content-free status file (codes, ids, counts only) and can
POST the same JSON to ``PULSE_NOTIFY_URL``. A human reads the preview and runs
``pulse publish-direct <run_id> --send --approve``.
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

STATUS_KEYS = ("outcome", "failed_step", "code", "report_start", "report_end", "run_id", "snapshot_id",
               "word_count")


def cli_runner(argv, *, config=None):
    """Run one CLI command in a child process; return (exit_code, parsed_json_or_{})."""
    cmd = [sys.executable, "-m", "groww_pulse"] + (["--config", str(config)] if config else []) + list(argv)
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    try:
        return done.returncode, json.loads(done.stdout)
    except ValueError:
        return done.returncode, {}


def load_status(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_status(path: Path, status: dict):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(status, indent=2), encoding="utf-8")
    temp.replace(path)


def notify(status: dict, env=os.environ, post=None):
    """Best-effort content-free webhook. Never raises, never includes review text."""
    url = env.get("PULSE_NOTIFY_URL", "").strip()
    if not url.startswith("https://"):
        return False
    try:
        if post is None:
            import urllib.request
            request = urllib.request.Request(url, data=json.dumps(status).encode(), method="POST",
                                             headers={"Content-Type": "application/json"})
            urllib.request.urlopen(request, timeout=15).close()  # noqa: S310 (https enforced above)
        else:
            post(url, status)
        return True
    except Exception:
        return False


def run_weekly(*, runner=cli_runner, as_of: str, out: Path, status_path: Path, fetch: bool,
               config=None, env=os.environ, now=lambda: datetime.now(UTC)):
    """Run the weekly chain once; return the status dict (also written to ``status_path``)."""
    status = {"started_at": now().isoformat(timespec="seconds"), "as_of": as_of, "fetch": fetch,
              "outcome": "STARTED"}

    def finish(outcome, step=None, code=None):
        status.update(outcome=outcome, failed_step=step, code=code,
                      finished_at=now().isoformat(timespec="seconds"))
        write_status(status_path, {k: v for k, v in status.items() if v is not None})
        notify(status, env)
        return status

    def step(name, argv):
        code, data = runner(argv, config=config)
        if code != 0 or data.get("status") == "ERROR":
            raise _Stop(name, data.get("code") or data.get("reason_code") or f"EXIT_{code}")
        return data

    try:
        window = step("config", ["config", "check", "--as-of", as_of])["window"]
        status.update(report_start=window["report_start"], report_end=window["report_end"])
        previous = load_status(status_path)
        if (previous.get("outcome") == "PREVIEW_READY" and previous.get("report_start") == window["report_start"]
                and previous.get("run_id")):
            status.update(run_id=previous["run_id"], snapshot_id=previous.get("snapshot_id"),
                          word_count=previous.get("word_count"))
            return finish("ALREADY_DONE")  # idempotent: one preview per reporting week
        if fetch:
            fetched = step("fetch", ["fetch", "--confirm-public-source", "--out", str(out)])
            export, manifest = Path(out) / fetched["export"], Path(out) / fetched["manifest"]
        else:
            manifests = sorted(Path(out).glob("*.manifest.yaml"), key=lambda p: p.stat().st_mtime)
            if not manifests:
                raise _Stop("fetch", "NO_EXPORT")
            manifest = manifests[-1]
            export = manifest.with_name(manifest.name.replace(".manifest.yaml", ".jsonl"))
        imported = step("import", ["import", "--manifest", str(manifest), "--file", str(export),
                                   "--as-of", as_of])
        snap = step("snapshot", ["snapshot", "--import-id", imported["import_id"], "--as-of", as_of])
        if snap.get("status") != "READY":
            raise _Stop("snapshot", snap.get("status", "SNAPSHOT_NOT_READY"))  # stale data never = this week
        status["snapshot_id"] = snap["snapshot_id"]
        run = step("preview", ["preview", "--snapshot-id", snap["snapshot_id"]])
        status.update(run_id=run.get("run_id"), word_count=run.get("word_count"))
        if run.get("status") != "PREVIEW_READY":
            raise _Stop("preview", run.get("reason_code") or run.get("status", "PREVIEW_FAILED"))
        return finish("PREVIEW_READY")
    except _Stop as stop:
        return finish("BLOCKED", stop.step, stop.code)
    except Exception:  # Never leak exception text: it could contain review content.
        return finish("BLOCKED", "internal", "INTERNAL_ERROR")


class _Stop(Exception):
    def __init__(self, step, code):
        super().__init__(step, code)
        self.step, self.code = step, code
