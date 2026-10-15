import sys
from pathlib import Path

from groww_pulse.integrations.mcp_client import MCPConfig, connect_mcp
from groww_pulse.integrations.publication import Publisher
from groww_pulse.workflow.graph import execute
from tests.unit.test_workflow import setup_nodes


def test_real_sdk_stdio_transport_with_fictional_server(conn, config, tmp_path):
    server = Path(__file__).parents[1] / "fixtures" / "mcp_server.py"
    mcp_config = MCPConfig(command=sys.executable, args=(str(server),), timeout_seconds=20)
    with connect_mcp(mcp_config) as client:
        publisher = Publisher(conn, client, "operator@example.com", sleep=lambda _: None)
        snap, nodes = setup_nodes(conn, config, tmp_path, publisher=publisher)
        state = execute(nodes, snap.snapshot_id, mode="publish")
        assert state["status"] == "COMPLETE", state
        assert state["document_url"].startswith("https://docs.google.com/document/d/")
        assert state["draft_id"] == "draft_0"
