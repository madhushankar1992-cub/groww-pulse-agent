import json
from pathlib import Path

from groww_pulse.scheduling import load_status, notify, run_weekly

WINDOW = {"history_start": "2026-08-03", "report_start": "2026-10-05", "report_end": "2026-10-12"}


class Chain:
    """Fake CLI: records commands and returns canned JSON per command."""

    def __init__(self, fail=None, snapshot_status="READY", preview_status="PREVIEW_READY"):
        self.calls, self.fail = [], fail
        self.snapshot_status, self.preview_status = snapshot_status, preview_status

    def __call__(self, argv, *, config=None):
        name = argv[0] if argv[0] != "config" else "config"
        self.calls.append(name)
        if name == self.fail:
            return 4, {"status": "ERROR", "code": "MCP_UNAVAILABLE"}
        data = {
            "config": {"status": "OK", "window": WINDOW},
            "fetch": {"status": "OK", "export": "e.jsonl", "manifest": "e.manifest.yaml"},
            "import": {"status": "OK", "import_id": "imp-1"},
            "snapshot": {"status": self.snapshot_status, "snapshot_id": "snap-1"},
            "preview": {"status": self.preview_status, "run_id": "run-1", "word_count": 219},
        }[name]
        return 0, data


def go(tmp_path, chain, fetch=True, env=None):
    return run_weekly(runner=chain, as_of="2026-10-12", out=tmp_path / "x", status_path=tmp_path / "s.json",
                      fetch=fetch, env=env or {})


def test_full_chain_never_publishes(tmp_path):
    chain = Chain()
    status = go(tmp_path, chain)
    assert chain.calls == ["config", "fetch", "import", "snapshot", "preview"]
    assert status["outcome"] == "PREVIEW_READY" and status["run_id"] == "run-1"
    assert not {"publish", "publish-direct"} & set(chain.calls)
    saved = load_status(tmp_path / "s.json")
    assert saved["report_start"] == "2026-10-05" and saved["word_count"] == 219


def test_second_run_for_same_week_is_idempotent(tmp_path):
    go(tmp_path, Chain())
    chain = Chain()
    status = go(tmp_path, chain)
    assert status["outcome"] == "ALREADY_DONE" and chain.calls == ["config"]


def test_failed_step_blocks_with_code_only(tmp_path):
    status = go(tmp_path, Chain(fail="fetch"))
    assert status["outcome"] == "BLOCKED" and status["failed_step"] == "fetch"
    assert status["code"] == "MCP_UNAVAILABLE" and "run_id" not in status


def test_stale_data_blocks_instead_of_becoming_this_week(tmp_path):
    chain = Chain(snapshot_status="COVERAGE_MISSING")
    status = go(tmp_path, chain)
    assert status["outcome"] == "BLOCKED" and status["failed_step"] == "snapshot"
    assert "preview" not in chain.calls


def test_without_fetch_reuses_newest_export(tmp_path):
    folder = tmp_path / "x"
    folder.mkdir()
    (folder / "a.manifest.yaml").write_text("x", encoding="utf-8")
    chain = Chain()
    assert go(tmp_path, chain, fetch=False)["outcome"] == "PREVIEW_READY"
    assert "fetch" not in chain.calls
    empty = go(tmp_path / "other", Chain(), fetch=False)
    assert empty["outcome"] == "BLOCKED" and empty["code"] == "NO_EXPORT"


def test_notification_is_https_only_and_content_free(tmp_path):
    sent = []
    assert not notify({"outcome": "X"}, {"PULSE_NOTIFY_URL": "http://insecure.example"}, post=lambda u, s: sent.append(s))
    assert not notify({"outcome": "X"}, {}, post=lambda u, s: sent.append(s))
    assert notify({"outcome": "BLOCKED"}, {"PULSE_NOTIFY_URL": "https://hooks.example/x"},
                  post=lambda u, s: sent.append(s))
    assert sent == [{"outcome": "BLOCKED"}]
    status = go(tmp_path, Chain(), env={"PULSE_NOTIFY_URL": "https://hooks.example/x"})
    text = json.dumps(status)
    assert "content" not in text and Path(tmp_path / "s.json").exists()
