import json

import pytest

from groww_pulse.composition.composer import Composition
from groww_pulse.composition.renderer import word_count
from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.llm.base import Budget
from groww_pulse.privacy.eligibility import check_eligibility
from groww_pulse.repository.workflow import get_result
from groww_pulse.validation.pulse import validate
from groww_pulse.workflow.graph import execute
from groww_pulse.workflow.nodes import Nodes
from tests.unit.test_analysis import FakeLLM, seed_reviews


class ComposerLLM(FakeLLM):
    def __init__(self, bad=0, privacy=False, **kwargs):
        super().__init__(**kwargs)
        self.bad, self.privacy, self.compositions = bad, privacy, 0

    def complete_json(self, **kwargs):
        if "summaries" not in kwargs["schema"]["properties"]:
            return super().complete_json(**kwargs)
        self.compositions += 1
        self.calls.append("summaries")
        if self.budget:
            self.budget.record(500, 100)
        payload = json.loads(kwargs["user"])
        bad = self.compositions <= self.bad
        return {
            "summaries": [{"theme_id": t["theme_id"],
                           "text": "word " * 100 if bad else "Reviewers report difficulties."}
                          for t in payload["themes"]],
            "actions": [{"theme_id": q["theme_id"], "text": "Investigate the reported experience."
                         if not self.privacy else "Contact canary.private@example.com for details.",
                         "evidence_review_ids": [q["review_id"]]} for q in payload["quotes"]],
        }


def setup_nodes(conn, config, tmp_path, llm=None, publisher=None):
    snap = seed_reviews(conn, config, tmp_path)
    budget = Budget(config.model)
    llm = llm or ComposerLLM(budget=budget)
    nodes = Nodes(conn, config, llm, budget, None, publisher, tmp_path / "artifacts")
    return snap, nodes


def test_preview_artifacts_and_evidence(conn, config, tmp_path):
    snap, nodes = setup_nodes(conn, config, tmp_path)
    result = execute(nodes, snap.snapshot_id)
    assert result["status"] == "PREVIEW_READY", result
    folder = tmp_path / "artifacts" / result["run_id"]
    assert {p.name for p in folder.iterdir()} == {"pulse.md", "pulse.json", "evidence.json", "validation.json"}
    pulse = json.loads((folder / "pulse.json").read_text(encoding="utf-8"))
    assert word_count(pulse["rendered_body"]) == pulse["word_count"] <= 250
    assert "excluded across the export" in pulse["rendered_body"]
    assert "excerpt" not in json.dumps(result)
    assert execute(nodes, run_id=result["run_id"])["status"] == "PREVIEW_READY"


@pytest.mark.parametrize("stop", ["load_snapshot", "discover_taxonomy", "classify_reviews", "rank_themes",
                                  "select_quotes", "compose_pulse", "validate_pulse"])
def test_checkpoint_resume_preserves_completed_work(conn, config, tmp_path, stop):
    snap, nodes = setup_nodes(conn, config, tmp_path)
    paused = execute(nodes, snap.snapshot_id, interrupt_after=[stop])
    assert paused["status"] == "RUNNING"
    calls = list(nodes.llm.calls)
    # A fresh runtime and saver re-open the on-disk checkpoint, as a restarted process does.
    resumed_llm = ComposerLLM()
    resumed = Nodes(conn, config, resumed_llm, Budget(config.model), None, artifacts=tmp_path / "artifacts")
    final = execute(resumed, run_id=paused["run_id"])
    assert final["status"] == "PREVIEW_READY", final
    if "themes" in calls:
        assert "themes" not in resumed_llm.calls
    if "assignments" in calls:
        assert "assignments" not in resumed_llm.calls
    if "summaries" in calls:
        assert "summaries" not in resumed_llm.calls


@pytest.mark.parametrize("bad,status,attempts", [(1, "PREVIEW_READY", 2), (10, "VALIDATION_FAILED", 3)])
def test_bounded_repair(conn, config, tmp_path, bad, status, attempts):
    llm = ComposerLLM(bad=bad)
    snap, nodes = setup_nodes(conn, config, tmp_path, llm)
    result = execute(nodes, snap.snapshot_id)
    assert result["status"] == status, result
    assert llm.compositions == attempts


def test_unsafe_generation_not_persisted(conn, config, tmp_path):
    snap, nodes = setup_nodes(conn, config, tmp_path, ComposerLLM(privacy=True))
    result = execute(nodes, snap.snapshot_id)
    assert result["status"] == "VALIDATION_FAILED"
    for name in ("pulse.sqlite3", "pulse.sqlite3-wal", "checkpoints.sqlite3", "logs/events.jsonl"):
        path = tmp_path / name
        if path.exists():
            assert b"canary.private" not in path.read_bytes()


def test_missing_key_records_blocked_run(conn, config, tmp_path):
    class MissingKey(ComposerLLM):
        def complete_json(self, **kwargs):
            raise PulseError(ErrorCode.LLM_AUTH_FAILED)
    snap, nodes = setup_nodes(conn, config, tmp_path, MissingKey())
    result = execute(nodes, snap.snapshot_id)
    assert result["status"] == "BLOCKED"
    assert result["reason_code"] == "LLM_AUTH_FAILED"
    assert result["last_completed_node"] == "load_snapshot"
    nodes.llm = ComposerLLM()
    assert execute(nodes, run_id=result["run_id"])["status"] == "PREVIEW_READY"


@pytest.mark.parametrize("text,code", [
    ("The app works perfectly for my monthly investment needs today 😊", "EMOJI_REVIEW"),
    ("😊👍", "EMOJI_REVIEW"),
    ("Esta aplicación no funciona bien y necesito ayuda con mi dinero", "LANGUAGE_UNSUPPORTED"),
    ("ye app bahut slow hai aur mera paisa nahi mil raha", "LANGUAGE_UNSUPPORTED"),
    ("यह ऐप बहुत खराब है और मेरा पैसा नहीं मिला", "LANGUAGE_UNSUPPORTED"),
])
def test_review_filters(text, code):
    with pytest.raises(PulseError) as err:
        check_eligibility(text)
    assert err.value.code.value == code


def test_english_review_passes():
    check_eligibility("I cannot withdraw my money and support has not answered my request")


def test_tampered_evidence_and_actions_fail(conn, config, tmp_path):
    snap, nodes = setup_nodes(conn, config, tmp_path)
    state = execute(nodes, snap.snapshot_id)
    analysis = nodes.analysis(state)
    comp = Composition.model_validate(get_result(conn, state["run_id"], "composition_1")["composition"])
    action = comp.actions[0].model_copy(update={"text": "We will fix it because of the bug in storage"})
    comp = comp.model_copy(update={"actions": (action, *comp.actions[1:])})
    quote = analysis.quotes[0].model_copy(update={"excerpt": "A fabricated quotation with enough words to pass"})
    analysis = analysis.model_copy(update={"quotes": (quote, *analysis.quotes[1:])})
    pulse, report = validate(conn, config, analysis, snap, comp, None)
    assert pulse is None
    assert {"ACTION_UNSUPPORTED", "QUOTE_INVALID"} <= set(report["errors"])


def test_word_count_includes_heading_and_coverage():
    assert word_count("# Heading\nCoverage: " + "word " * 249) == 251


def test_budget_and_timeout_guards(conn, config, tmp_path):
    snap, nodes = setup_nodes(conn, config, tmp_path)
    nodes.budget.input_tokens = config.model.max_input_tokens_per_run + 1
    state = execute(nodes, snap.snapshot_id)
    assert state["reason_code"] == "BUDGET_EXCEEDED"
    nodes.budget.input_tokens = 0
    guarded = nodes.wrap("load_snapshot")({**state, "status": "RUNNING", "active_seconds": 999999})
    assert guarded["reason_code"] == "RUN_TIMEOUT"


def test_old_snapshot_is_blocked(conn, config, tmp_path):
    snap, nodes = setup_nodes(conn, config, tmp_path)
    conn.execute("UPDATE imports SET sanitiser_version='1'")
    result = execute(nodes, snap.snapshot_id)
    assert result["reason_code"] == "FILTER_REFRESH_REQUIRED"
    assert nodes.llm.calls == []


def test_classification_cap_and_sparse_graph(conn, config, tmp_path):
    class NoAssignments(ComposerLLM):
        def complete_json(self, **kwargs):
            if "assignments" in kwargs["schema"]["properties"]:
                return {"assignments": []}
            return super().complete_json(**kwargs)
    snap, nodes = setup_nodes(conn, config, tmp_path, NoAssignments())
    state = execute(nodes, snap.snapshot_id)
    assert state["reason_code"] == "CLASSIFICATION_FAILED"
    assert state["classify_attempts"] == 3
    assert execute(nodes, run_id=state["run_id"])["status"] == "FAILED"


def test_routing_branches():
    from groww_pulse.workflow.routing import after_classify, after_validate, evidence_gate, mode
    assert evidence_gate({"status": "BLOCKED"}) == "stop"
    assert evidence_gate({"status": "RUNNING"}) == "continue"
    assert after_classify({"status": "FAILED"}) == "stop"
    assert after_classify({"status": "RUNNING", "reason_code": "CLASSIFICATION_FAILED"}) == "retry"
    assert after_classify({"status": "RUNNING"}) == "continue"
    assert after_validate({"status": "FAILED"}) == "stop"
    assert after_validate({"status": "RUNNING"}) == "valid"
    base = {"status": "RUNNING", "validation_errors": ["WORD_LIMIT"], "repair_cap": 2}
    assert after_validate({**base, "compose_attempts": 2}) == "repair"
    assert after_validate({**base, "compose_attempts": 3}) == "stop"
    assert mode({"mode": "publish"}) == "publish"
    assert mode({"mode": "preview"}) == "preview"


def test_exact_251_word_render_is_rejected(conn, config, tmp_path, monkeypatch):
    snap, nodes = setup_nodes(conn, config, tmp_path)
    state = execute(nodes, snap.snapshot_id)
    comp = Composition.model_validate(get_result(conn, state["run_id"], "composition_1")["composition"])
    monkeypatch.setattr("groww_pulse.validation.pulse.render", lambda *args: "word " * 251)
    pulse, report = validate(conn, config, nodes.analysis(state), snap, comp, None)
    assert pulse is None and "WORD_LIMIT" in report["errors"]
    assert report["word_count"] == 251


def test_cross_theme_action_evidence_rejected(conn, config, tmp_path):
    snap, nodes = setup_nodes(conn, config, tmp_path)
    state = execute(nodes, snap.snapshot_id)
    comp = Composition.model_validate(get_result(conn, state["run_id"], "composition_1")["composition"])
    action = comp.actions[0].model_copy(update={"evidence_review_ids": comp.actions[1].evidence_review_ids})
    comp = comp.model_copy(update={"actions": (action, *comp.actions[1:])})
    pulse, report = validate(conn, config, nodes.analysis(state), snap, comp, None)
    assert pulse is None and "EVIDENCE_INVALID" in report["errors"]


def test_config_change_rejected_on_resume(conn, config, tmp_path):
    snap, nodes = setup_nodes(conn, config, tmp_path)
    state = execute(nodes, snap.snapshot_id, interrupt_after=["load_snapshot"])
    nodes.config = config.model_copy(update={"output": config.output.model_copy(update={"max_words": 200})})
    with pytest.raises(PulseError) as err:
        execute(nodes, run_id=state["run_id"])
    assert err.value.code == ErrorCode.CONFIG_CHANGED


def test_concurrent_workflow_lock(tmp_path):
    from groww_pulse.workflow.locking import run_lock
    with run_lock(tmp_path, "workflow"), pytest.raises(PulseError) as err:
        with run_lock(tmp_path, "workflow"):
            pass
    assert err.value.code == ErrorCode.PUBLICATION_CONFLICT


@pytest.mark.ner
def test_preview_with_real_name_detector(conn, config, tmp_path):
    from groww_pulse.privacy.detectors import spacy_names
    snap, nodes = setup_nodes(conn, config, tmp_path)
    nodes.names = spacy_names()
    result = execute(nodes, snap.snapshot_id)
    assert result["status"] == "PREVIEW_READY", result


def test_sparse_graph_makes_no_model_calls(conn, config, tmp_path):
    snap = seed_reviews(conn, config, tmp_path, per_topic=(2, 2, 2, 2))
    llm = ComposerLLM()
    nodes = Nodes(conn, config, llm, Budget(config.model), None, artifacts=tmp_path / "artifacts")
    state = execute(nodes, snap.snapshot_id)
    assert state["status"] == "INSUFFICIENT_EVIDENCE"
    assert llm.calls == []


def test_quote_gate(conn, config, tmp_path, monkeypatch):
    monkeypatch.setattr("groww_pulse.workflow.nodes.select_quote", lambda *args: None)
    snap, nodes = setup_nodes(conn, config, tmp_path)
    state = execute(nodes, snap.snapshot_id)
    assert state["status"] == "INSUFFICIENT_EVIDENCE"
    assert state["reason_code"] == "NO_VALID_QUOTE"
    assert "summaries" not in nodes.llm.calls


def test_nodes_directly_accept_constructed_reference_state(conn, config, tmp_path):
    from uuid import uuid4

    from groww_pulse.workflow.state import config_hash
    snap, nodes = setup_nodes(conn, config, tmp_path)
    state = {"run_id": str(uuid4()), "snapshot_id": str(snap.snapshot_id), "mode": "preview",
             "config_hash": config_hash(config), "status": "RUNNING", "repair_cap": 2, "classify_cap": 3}
    for name in ("load_snapshot", "discover_taxonomy", "classify_reviews", "rank_themes", "select_quotes",
                 "compose_pulse", "validate_pulse", "preview_ready"):
        state.update(nodes.wrap(name)(dict(state)))
        assert state["last_completed_node"] == name
        assert state["status"] in {"RUNNING", "PREVIEW_READY"}, state
    assert state["status"] == "PREVIEW_READY"
