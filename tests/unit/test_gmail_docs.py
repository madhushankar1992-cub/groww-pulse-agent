import json

import pytest

from groww_pulse.errors import PulseError
from groww_pulse.integrations.gmail_docs import publish, settings

ENV = {"MCP_AUTH_TOKEN": "t" * 12, "PULSE_DRAFT_RECIPIENT": "me@example.com",
       "PULSE_GOOGLE_DOC_ID": "1" + "a" * 30}


@pytest.fixture
def run_dir(tmp_path):
    folder = tmp_path / "artifacts" / "run-1"
    folder.mkdir(parents=True)
    (folder / "pulse.md").write_text("# Groww Weekly Review Pulse | 05 Oct\n\n1. Theme\n", encoding="utf-8")
    (folder / "validation.json").write_text(json.dumps({"valid": True, "errors": []}), encoding="utf-8")
    return folder


class Fake:
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def __call__(self, cfg, name, arguments, timeout=60):
        self.calls.append((name, bool(arguments.get("dry_run"))))
        if name == self.fail_on:
            raise PulseError(__import__("groww_pulse.errors", fromlist=["ErrorCode"]).ErrorCode.MCP_UNAVAILABLE)
        return {"message_id": "m1", "status": "ok"}


def test_settings_require_all_values():
    with pytest.raises(PulseError, match="CONFIG_INVALID"):
        settings({**ENV, "MCP_AUTH_TOKEN": ""})
    with pytest.raises(PulseError, match="CONFIG_INVALID"):
        settings({**ENV, "PULSE_DRAFT_RECIPIENT": "not-an-email"})
    assert settings(ENV)["document_id"] == "1" + "a" * 30
    url = {**ENV, "PULSE_GOOGLE_DOC_ID": "https://docs.google.com/document/d/" + "b" * 25 + "/edit"}
    assert settings(url)["document_id"] == "b" * 25


def test_dry_run_only_validates(run_dir, tmp_path):
    fake = Fake()
    report = publish(run_dir, env=ENV, ledger_path=tmp_path / "l.json", caller=fake)
    assert fake.calls == [] and report["email"] == "SKIPPED"


def test_send_requires_approval(run_dir, tmp_path):
    fake = Fake()
    with pytest.raises(PulseError, match="APPROVAL_REQUIRED"):
        publish(run_dir, dry_run=False, env=ENV, ledger_path=tmp_path / "l.json", caller=fake)
    assert ("append_to_google_doc", False) not in fake.calls


def test_publish_once_then_idempotent(run_dir, tmp_path):
    ledger = tmp_path / "l.json"
    first = Fake()
    report = publish(run_dir, dry_run=False, approve=True, env=ENV, ledger_path=ledger, caller=first)
    assert report["doc_append"] == "DONE" and report["email"] == "SENT"
    assert [c for c in first.calls if not c[1]] == [("append_to_google_doc", False), ("send_email", False)]
    again = Fake()
    report = publish(run_dir, dry_run=False, approve=True, env=ENV, ledger_path=ledger, caller=again)
    assert report["doc_append"] == "ALREADY_DONE" and report["email"] == "ALREADY_DONE"
    assert [c for c in again.calls if not c[1]] == []


def test_unknown_outcome_is_never_repeated(run_dir, tmp_path):
    ledger = tmp_path / "l.json"
    with pytest.raises(PulseError):
        publish(run_dir, dry_run=False, approve=True, env=ENV, ledger_path=ledger,
                caller=Fake(fail_on="append_to_google_doc"))
    retry = Fake()
    with pytest.raises(PulseError, match="RECONCILIATION_REQUIRED"):
        publish(run_dir, dry_run=False, approve=True, env=ENV, ledger_path=ledger, caller=retry)
    assert [c for c in retry.calls if not c[1]] == []


def test_invalid_pulse_blocks_publication(run_dir, tmp_path):
    (run_dir / "validation.json").write_text(json.dumps({"valid": False, "errors": ["WORD_LIMIT"]}))
    with pytest.raises(PulseError, match="VALIDATION_FAILED"):
        publish(run_dir, env=ENV, ledger_path=tmp_path / "l.json", caller=Fake())
