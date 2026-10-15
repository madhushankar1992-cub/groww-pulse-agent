from pathlib import Path

import pytest

from groww_pulse.cli import main
from groww_pulse.errors import PulseError
from groww_pulse.integrations.contract import check_capabilities, load_contract, load_tools_dump
from tests.conftest import dump_tools

CONTRACT = Path("config/mcp_contract.yaml")


def full_tools(contract):
    return {n: list(f) for n, f in {**contract.required, **contract.optional}.items()}


def test_complete_server_passes(tmp_path):
    contract = load_contract(CONTRACT)
    tools = {**full_tools(contract), "gmail_send_draft": ["draft_id"], "docs_share": ["email"], "ping": []}
    report = check_capabilities(contract, load_tools_dump(dump_tools(tmp_path / "t.json", tools)))
    assert report.ok
    assert report.ignored_forbidden == ("docs_share", "gmail_send_draft")
    assert report.ignored_other == 1
    assert report.available_optional == ("docs_get_page_count", "gmail_update_draft")


def test_missing_tool_and_input_block(tmp_path):
    contract = load_contract(CONTRACT)
    tools = full_tools(contract)
    del tools["gmail_get_draft"]
    tools["gmail_create_draft"] = ["to", "subject", "body_text"]  # no marker
    report = check_capabilities(contract, load_tools_dump(dump_tools(tmp_path / "t.json", tools)))
    assert not report.ok
    assert report.missing_required == ("gmail_get_draft",)
    assert report.missing_inputs == {"gmail_create_draft": ("marker",)}


def test_contract_never_allows_send():
    contract = load_contract(CONTRACT)
    assert not any("send" in name for name in contract.allowlist)


def test_contract_with_send_tool_is_rejected(tmp_path):
    bad = tmp_path / "c.yaml"
    bad.write_text("version: 1\nrequired:\n  gmail_send_draft: [draft_id]\nforbidden_patterns: [send]\n",
                   encoding="utf-8")
    with pytest.raises(PulseError):
        load_contract(bad)


def test_cli_exit_codes(tmp_path, capsys):
    contract = load_contract(CONTRACT)
    good = dump_tools(tmp_path / "good.json", full_tools(contract))
    assert main(["contract", "check", "--tools", str(good)]) == 0
    bad = dump_tools(tmp_path / "bad.json", {"docs_get_document": ["document_id"]})
    assert main(["contract", "check", "--tools", str(bad)]) == 4
    assert "CAPABILITY_UNAVAILABLE" in capsys.readouterr().out
