import sqlite3
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.repository.workflow import get_result, get_run, put_result, save_run
from groww_pulse.workflow import routing
from groww_pulse.workflow.state import PulseState, config_hash

NODE_NAMES = ("load_snapshot", "discover_taxonomy", "classify_reviews", "rank_themes", "select_quotes",
              "compose_pulse", "validate_pulse", "preview_ready", "check_capabilities",
              "publish_doc", "verify_doc", "create_draft", "verify_draft")


def build_graph(nodes, saver, interrupt_after=None):
    builder = StateGraph(PulseState)
    for name in NODE_NAMES:
        builder.add_node(name, nodes.wrap(name))
    builder.add_edge(START, "load_snapshot")
    for source, target in (("load_snapshot", "discover_taxonomy"),
                           ("discover_taxonomy", "classify_reviews"),
                           ("rank_themes", "select_quotes"), ("select_quotes", "compose_pulse"),
                           ("compose_pulse", "validate_pulse"),
                           ("check_capabilities", "publish_doc"), ("publish_doc", "verify_doc"),
                           ("verify_doc", "create_draft"), ("create_draft", "verify_draft")):
        builder.add_conditional_edges(source, routing.evidence_gate, {"stop": END, "continue": target})
    builder.add_conditional_edges("classify_reviews", routing.after_classify,
        {"stop": END, "retry": "classify_reviews", "continue": "rank_themes"})
    def after_validation(state):
        route = routing.after_validate(state)
        return routing.mode(state) if route == "valid" else route
    builder.add_conditional_edges("validate_pulse", after_validation,
        {"stop": END, "repair": "compose_pulse", "preview": "preview_ready", "publish": "check_capabilities"})
    builder.add_edge("preview_ready", END)
    builder.add_edge("verify_draft", END)
    return builder.compile(checkpointer=saver, interrupt_after=interrupt_after)


def execute(nodes, snapshot_id=None, mode="preview", run_id=None, interrupt_after=None):
    from groww_pulse.workflow.locking import run_lock
    # A single writer per data directory also protects concurrent calls on one run.
    with run_lock(nodes.config.storage.database.parent / "locks", "workflow"):
        return _execute(nodes, snapshot_id, mode, run_id, interrupt_after)


def _execute(nodes, snapshot_id=None, mode="preview", run_id=None, interrupt_after=None):
    cfg = nodes.config
    path = cfg.storage.database.parent / "checkpoints.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, check_same_thread=False) as checkpoint_conn:
        graph = build_graph(nodes, SqliteSaver(checkpoint_conn), interrupt_after)
        if run_id is None:
            run_id = str(uuid4())
            state = PulseState(run_id=run_id, snapshot_id=str(snapshot_id), config_hash=config_hash(cfg),
                mode=mode, status="RUNNING", reason_code=None, last_completed_node="start",
                active_seconds=0, compose_attempts=0, classify_attempts=0,
                repair_cap=cfg.execution.composition_repairs, classify_cap=cfg.execution.model_batch_attempts)
            save_run(nodes.conn, state)
            initial = state
        else:
            run_id = str(run_id)
            state = get_run(nodes.conn, run_id)
            if state["config_hash"] != config_hash(cfg):
                raise PulseError(ErrorCode.CONFIG_CHANGED)
            usage = get_result(nodes.conn, run_id, "usage") or state.get("usage", {})
            nodes.budget.input_tokens = int(usage.get("input_tokens", 0))
            nodes.budget.output_tokens = int(usage.get("output_tokens", 0))
            nodes.budget.calls = int(usage.get("calls", 0))
            if state["status"] in {"PREVIEW_READY", "COMPLETE", "INSUFFICIENT_EVIDENCE", "VALIDATION_FAILED"}:
                return state
            initial = None
        def persist_usage(usage):
            # Each LLM worker writes its updated counters on a separate connection.
            with sqlite3.connect(cfg.storage.database, isolation_level=None) as usage_conn:
                put_result(usage_conn, run_id, "usage", usage)
        nodes.budget.on_record = persist_usage
        thread = {"configurable": {"thread_id": run_id}, "recursion_limit": 60}
        current = graph.get_state(thread)
        if initial is None and state["status"] in {"BLOCKED", "FAILED"}:
            # Requeue the failed node only. All completed evidence/pulse results stay in SQLite.
            from langgraph.types import Command
            failed = state.get("failed_node")
            if not failed or state.get("reason_code") in {"BUDGET_EXCEEDED", "RUN_TIMEOUT", "CLASSIFICATION_FAILED"}:
                return state
            initial = Command(update={"status": "RUNNING", "reason_code": None}, goto=failed)
        elif initial is None and not current.values:
            initial = state  # Crash before the first checkpoint.
        result = graph.invoke(initial, thread)
        save_run(nodes.conn, result)
        return result
