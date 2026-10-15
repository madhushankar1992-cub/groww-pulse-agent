import pytest

from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.integrations.contract import load_contract
from groww_pulse.integrations.mcp_client import SafeClient
from groww_pulse.integrations.publication import Publisher, confirm_layout
from groww_pulse.workflow.graph import execute
from tests.unit.test_workflow import setup_nodes


class FakeServer:
    def __init__(self, page_tool=True, fail_after=None, missing=None):
        self.page_tool, self.fail_after, self.missing = page_tool, fail_after, missing
        self.documents, self.drafts, self.calls = {}, {}, []
        self.fail_reads = 0
        self.gmail_down = False
        self.duplicate = False
        self.pages = 1

    def list_tools(self):
        contract = load_contract()
        tools = {k: frozenset(v) for k, v in {**contract.required, **contract.optional}.items()}
        tools["gmail_send_draft"] = frozenset({"draft_id"})
        if not self.page_tool:
            tools.pop("docs_get_page_count")
        if self.missing:
            tools.pop(self.missing)
        return tools

    def call(self, name, args):
        self.calls.append(name)
        if name.startswith("gmail") and self.gmail_down:
            raise TimeoutError()
        if name == "docs_create_document":
            rid = f"doc_{len(self.documents)}"
            self.documents[rid] = {"document_id": rid, "title": args["title"], "text": "",
                                   "url": f"https://docs.google.com/document/d/{rid}/edit",
                                   "revision_id": "r1", "marker": args["marker"]}
            result = self.documents[rid]
        elif name == "docs_write_content":
            self.documents[args["document_id"]]["text"] = args["content_markdown"]
            result = {"document_id": args["document_id"], "revision_id": "r2"}
        elif name == "docs_get_document":
            if self.fail_reads:
                self.fail_reads -= 1
                raise TimeoutError()
            result = self.documents[args["document_id"]]
        elif name == "docs_find_by_marker":
            docs = [d for d in self.documents.values() if d["marker"] == args["marker"]]
            result = {"documents": docs * (2 if self.duplicate else 1)}
        elif name == "docs_get_page_count":
            result = {"page_count": self.pages}
        elif name == "gmail_create_draft":
            rid = f"draft_{len(self.drafts)}"
            self.drafts[rid] = {**args, "draft_id": rid, "message_id": "message_0", "is_draft": True}
            result = self.drafts[rid]
        elif name == "gmail_get_draft":
            result = self.drafts[args["draft_id"]]
        elif name == "gmail_find_drafts":
            result = {"drafts": [d for d in self.drafts.values() if d["marker"] == args["marker"]]}
        else:
            raise AssertionError(name)
        if self.fail_after == name:
            self.fail_after = None
            raise TimeoutError()
        return {**result, "private_account": "never-persist@example.com"}


def publishing(conn, config, tmp_path, server=None):
    server = server or FakeServer()
    pub = Publisher(conn, SafeClient(server), "operator@example.com", sleep=lambda _: None)
    snap, nodes = setup_nodes(conn, config, tmp_path, publisher=pub)
    return snap, nodes, server


def test_happy_path_and_no_forbidden_calls(conn, config, tmp_path):
    snap, nodes, server = publishing(conn, config, tmp_path)
    result = execute(nodes, snap.snapshot_id, mode="publish")
    assert result["status"] == "COMPLETE", result
    assert len(server.documents) == len(server.drafts) == 1
    assert all(d["is_draft"] for d in server.drafts.values())
    assert not any("send" in name or "share" in name for name in server.calls)
    assert conn.execute("SELECT verified FROM publication_receipts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM external_operations WHERE status='VERIFIED'").fetchone()[0] == 3
    for path in tmp_path.glob("*.sqlite3*"):
        assert b"never-persist@example.com" not in path.read_bytes()
    for path in [tmp_path / "checkpoints.sqlite3", tmp_path / "logs/events.jsonl"]:
        assert b"operator@example.com" not in path.read_bytes()


@pytest.mark.parametrize("tool", ["docs_create_document", "docs_write_content", "gmail_create_draft"])
def test_applied_timeout_reconciles_without_second_write(conn, config, tmp_path, tool):
    snap, nodes, server = publishing(conn, config, tmp_path, FakeServer(fail_after=tool))
    result = execute(nodes, snap.snapshot_id, mode="publish")
    assert result["status"] == "COMPLETE", result
    assert server.calls.count(tool) == 1


def test_unknown_create_with_no_matches_never_retries(conn, config, tmp_path):
    class BeforeApply(FakeServer):
        def call(self, name, args):
            if name == "docs_create_document":
                self.calls.append(name)
                raise TimeoutError()
            return super().call(name, args)
    snap, nodes, server = publishing(conn, config, tmp_path, BeforeApply())
    result = execute(nodes, snap.snapshot_id, mode="publish")
    assert result["reason_code"] == "RECONCILIATION_REQUIRED"
    result = execute(nodes, run_id=result["run_id"])
    assert result["status"] == "BLOCKED"
    assert server.calls.count("docs_create_document") == 1


def test_missing_tool_has_zero_writes(conn, config, tmp_path):
    snap, nodes, server = publishing(conn, config, tmp_path, FakeServer(missing="gmail_get_draft"))
    result = execute(nodes, snap.snapshot_id, mode="publish")
    assert result["reason_code"] == "CAPABILITY_UNAVAILABLE"
    assert not server.calls


def test_doc_ok_gmail_read_failure_resume(conn, config, tmp_path):
    class GmailReadFailure(FakeServer):
        fail = True

        def call(self, name, args):
            if name == "gmail_get_draft" and self.fail:
                raise TimeoutError()
            return super().call(name, args)
    snap, nodes, server = publishing(conn, config, tmp_path, GmailReadFailure())
    result = execute(nodes, snap.snapshot_id, mode="publish")
    assert result["status"] == "BLOCKED"
    server.fail = False
    resumed = execute(nodes, run_id=result["run_id"])
    assert resumed["status"] == "COMPLETE", resumed
    assert server.calls.count("docs_create_document") == 1
    assert server.calls.count("docs_write_content") == 1
    assert server.calls.count("gmail_create_draft") == 1


def test_manual_layout_gate(conn, config, tmp_path):
    snap, nodes, server = publishing(conn, config, tmp_path, FakeServer(page_tool=False))
    result = execute(nodes, snap.snapshot_id, mode="publish")
    assert result["reason_code"] == "MANUAL_CHECK_REQUIRED"
    assert not server.drafts
    confirm_layout(conn, result["run_id"])
    assert execute(nodes, run_id=result["run_id"])["status"] == "COMPLETE"


def test_human_edit_is_never_overwritten(conn, config, tmp_path):
    snap, nodes, server = publishing(conn, config, tmp_path, FakeServer(page_tool=False))
    result = execute(nodes, snap.snapshot_id, mode="publish")
    server.documents["doc_0"]["text"] = "Human changed this document."
    confirm_layout(conn, result["run_id"])
    result = execute(nodes, run_id=result["run_id"])
    assert result["reason_code"] == "HUMAN_EDIT_DETECTED"
    assert server.calls.count("docs_write_content") == 1
    assert not server.drafts


def test_duplicate_marker_blocks(conn, config, tmp_path):
    server = FakeServer(fail_after="docs_create_document")
    server.duplicate = True
    snap, nodes, server = publishing(conn, config, tmp_path, server)
    result = execute(nodes, snap.snapshot_id, mode="publish")
    assert result["reason_code"] == "RECONCILIATION_REQUIRED"
    assert server.calls.count("docs_create_document") == 1


def test_duplicate_publication_run_blocks(conn, config, tmp_path):
    snap, nodes, server = publishing(conn, config, tmp_path)
    first = execute(nodes, snap.snapshot_id, mode="publish", interrupt_after=["check_capabilities"])
    second = execute(nodes, snap.snapshot_id, mode="publish")
    assert second["reason_code"] == "PUBLICATION_CONFLICT"
    assert execute(nodes, run_id=first["run_id"])["status"] == "COMPLETE"
    assert len(server.documents) == len(server.drafts) == 1


def test_transient_reads_retry(conn, config, tmp_path):
    snap, nodes, server = publishing(conn, config, tmp_path)
    server.fail_reads = 2
    assert execute(nodes, snap.snapshot_id, mode="publish")["status"] == "COMPLETE"


def test_allowlist_rejects_forbidden_even_if_exposed():
    client = SafeClient(FakeServer())
    client.check()
    with pytest.raises(PulseError) as err:
        client.call("gmail_send_draft", draft_id="anything")
    assert err.value.code == ErrorCode.CAPABILITY_UNAVAILABLE


@pytest.mark.parametrize("stop", ["check_capabilities", "publish_doc", "verify_doc", "create_draft"])
def test_publication_checkpoint_resume(conn, config, tmp_path, stop):
    snap, nodes, server = publishing(conn, config, tmp_path)
    paused = execute(nodes, snap.snapshot_id, mode="publish", interrupt_after=[stop])
    assert paused["status"] == "RUNNING"
    final = execute(nodes, run_id=paused["run_id"])
    assert final["status"] == "COMPLETE", final
    assert server.calls.count("docs_create_document") == server.calls.count("gmail_create_draft") == 1
