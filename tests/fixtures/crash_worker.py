"""Exit without cleanup after a persisted node, using fictional data only."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from groww_pulse.config import RunConfig  # noqa: E402
from groww_pulse.repository.db import connect  # noqa: E402
from groww_pulse.workflow.graph import execute  # noqa: E402
from tests.unit.test_publication import publishing  # noqa: E402

folder = Path(sys.argv[1])
config = RunConfig.model_validate({"storage": {"database": str(folder / "pulse.sqlite3")}})
conn = connect(config.storage.database)
snap, nodes, server = publishing(conn, config, folder)
state = execute(nodes, snap.snapshot_id, mode="publish", interrupt_after=[sys.argv[2]])
(folder / "restart.json").write_text(json.dumps({"state": state, "calls": nodes.llm.calls,
    "documents": server.documents, "drafts": server.drafts, "tool_calls": server.calls}), encoding="utf-8")
os._exit(0)
