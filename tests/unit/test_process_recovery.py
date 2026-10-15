import json
import subprocess
import sys
from pathlib import Path

import pytest

from groww_pulse.integrations.mcp_client import SafeClient
from groww_pulse.integrations.publication import Publisher
from groww_pulse.llm.base import Budget
from groww_pulse.workflow.graph import execute
from groww_pulse.workflow.nodes import Nodes
from tests.unit.test_publication import FakeServer
from tests.unit.test_workflow import ComposerLLM


@pytest.mark.parametrize("stop", ["load_snapshot", "discover_taxonomy", "classify_reviews", "rank_themes",
    "select_quotes", "compose_pulse", "validate_pulse", "check_capabilities", "publish_doc",
    "verify_doc", "create_draft", "verify_draft"])
def test_process_exit_and_recovery(conn, config, tmp_path, stop):
    script = Path(__file__).parents[1] / "fixtures" / "crash_worker.py"
    process = subprocess.run([sys.executable, str(script), str(tmp_path), stop], capture_output=True,
                             timeout=90, check=False)
    assert process.returncode == 0, process.stderr.decode(errors="replace")
    saved = json.loads((tmp_path / "restart.json").read_text(encoding="utf-8"))
    server = FakeServer()
    server.documents, server.drafts, server.calls = saved["documents"], saved["drafts"], saved["tool_calls"]
    llm = ComposerLLM()
    publisher = Publisher(conn, SafeClient(server), "operator@example.com", sleep=lambda _: None)
    nodes = Nodes(conn, config, llm, Budget(config.model), None, publisher, tmp_path / "artifacts")
    state = execute(nodes, run_id=saved["state"]["run_id"])
    assert state["status"] == "COMPLETE", state
    for call in ("themes", "assignments", "summaries"):
        if call in saved["calls"]:
            assert call not in llm.calls
    assert server.calls.count("docs_create_document") == server.calls.count("gmail_create_draft") == 1
