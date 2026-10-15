import time
from pathlib import Path
from uuid import UUID, uuid4

from groww_pulse.analysis import PROMPT_VERSION
from groww_pulse.analysis.classify import classify
from groww_pulse.analysis.corpus import discovery_sample, load, require_current_filter
from groww_pulse.analysis.quotes import candidates_for, select_quote
from groww_pulse.analysis.ranking import rank
from groww_pulse.analysis.taxonomy import discover
from groww_pulse.composition.artifacts import write_artifacts
from groww_pulse.composition.composer import Composition, propose
from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.observability.events import emit
from groww_pulse.privacy.detectors import detect
from groww_pulse.repository import store
from groww_pulse.repository.workflow import get_result, put_result, save_run
from groww_pulse.schemas import AnalysisResult, Pulse, Taxonomy
from groww_pulse.validation.pulse import validate

BLOCKING = {ErrorCode.FILTER_REFRESH_REQUIRED, ErrorCode.COVERAGE_MISSING,
            ErrorCode.LLM_AUTH_FAILED, ErrorCode.PRIVACY_MODEL_UNAVAILABLE,
            ErrorCode.CAPABILITY_UNAVAILABLE, ErrorCode.MCP_UNAVAILABLE,
            ErrorCode.MCP_RESPONSE_INVALID, ErrorCode.RECONCILIATION_REQUIRED,
            ErrorCode.HUMAN_EDIT_DETECTED, ErrorCode.MANUAL_CHECK_REQUIRED,
            ErrorCode.LAYOUT_INVALID, ErrorCode.PUBLICATION_CONFLICT}


class Nodes:
    def __init__(self, conn, config, llm, budget, names, publisher=None, artifacts=Path("artifacts")):
        self.conn, self.config, self.llm, self.budget = conn, config, llm, budget
        self.names, self.publisher, self.artifacts = names, publisher, artifacts

    def snapshot(self, state):
        return store.get_snapshot(self.conn, UUID(state["snapshot_id"]))

    def taxonomy(self, state):
        return Taxonomy.model_validate(get_result(self.conn, state["run_id"], "taxonomy"))

    def analysis(self, state):
        return AnalysisResult.model_validate(get_result(self.conn, state["run_id"], "analysis"))

    def wrap(self, name):
        def node(state):
            start = time.monotonic()
            try:
                if state.get("active_seconds", 0) >= self.config.execution.active_run_timeout_minutes * 60:
                    raise PulseError(ErrorCode.RUN_TIMEOUT)
                self.budget.check()
                update = getattr(self, name)(state)
                update["last_completed_node"] = name
                update["failed_node"] = None
                self.budget.check()
            except PulseError as exc:
                update = {"status": "BLOCKED" if exc.code in BLOCKING else "FAILED",
                          "reason_code": exc.code.value, "failed_node": name}
            except Exception:
                # Never put upstream exception messages in LangGraph checkpoints.
                update = {"status": "FAILED", "reason_code": "INTERNAL_ERROR", "failed_node": name}
            duration = time.monotonic() - start
            update.update(usage=self.budget.summary(),
                          active_seconds=state.get("active_seconds", 0) + duration)
            if (update["active_seconds"] >= self.config.execution.active_run_timeout_minutes * 60
                    and update.get("status", state["status"]) == "RUNNING"):
                update.update(status="FAILED", reason_code="RUN_TIMEOUT", failed_node=name)
            merged = {**state, **update}
            save_run(self.conn, merged)
            emit(self.config.storage.database.parent / "logs" / "events.jsonl",
                 run_id=state["run_id"], node=name, status=merged["status"],
                 reason_code=merged.get("reason_code"), duration_seconds=round(duration, 3),
                 usage=self.budget.summary())
            return update
        return node

    def load_snapshot(self, state):
        snap = self.snapshot(state)
        require_current_filter(self.conn, snap.snapshot_id)
        if snap.outcome == "COVERAGE_MISSING":
            raise PulseError(ErrorCode.COVERAGE_MISSING)
        if snap.counts.weekly_reviews < self.config.analysis.min_weekly_reviews:
            return {"status": "INSUFFICIENT_EVIDENCE", "reason_code": "TOO_FEW_WEEKLY_REVIEWS"}
        return {"eligible_weekly": snap.counts.weekly_reviews}

    def discover_taxonomy(self, state):
        snap = self.snapshot(state)
        taxonomy = store.find_taxonomy(self.conn, snap.snapshot_id, self.llm.model_ref, PROMPT_VERSION)
        if taxonomy is None:
            sample = discovery_sample(load(self.conn, snap.snapshot_id),
                                      self.config.model.discovery_sample_size, snap.snapshot_hash)
            taxonomy = discover(self.llm, sample, snap.snapshot_id, self.config.analysis.max_themes,
                                self.config.execution.model_batch_attempts,
                                self.config.model.reasoning_effort)
            store.save_taxonomy(self.conn, taxonomy)
        put_result(self.conn, state["run_id"], "taxonomy", taxonomy.model_dump(mode="json"))
        return {"taxonomy_id": str(taxonomy.taxonomy_id)}

    def classify_reviews(self, state):
        tax = self.taxonomy(state)
        known = store.assignments(self.conn, tax.taxonomy_id)
        todo = [r for r in load(self.conn, UUID(state["snapshot_id"])) if r.weekly and r.review_id not in known]
        attempt = state.get("classify_attempts", 0) + 1
        try:
            if todo:
                classify(self.llm, tax, todo, self.config.model.classify_batch_size, 1,
                         self.config.execution.max_concurrent_model_calls, self.config.model.classify_effort,
                         on_batch=lambda pairs: store.save_assignments(self.conn, tax.taxonomy_id, pairs))
        except PulseError as exc:
            if exc.code != ErrorCode.CLASSIFICATION_FAILED:
                raise
            return {"classify_attempts": attempt, "reason_code": exc.code.value,
                    "status": "FAILED" if attempt >= state["classify_cap"] else "RUNNING"}
        return {"classify_attempts": attempt, "reason_code": None}

    def rank_themes(self, state):
        tax = self.taxonomy(state)
        weekly = {r.review_id for r in load(self.conn, UUID(state["snapshot_id"])) if r.weekly}
        pairs = {k: v for k, v in store.assignments(self.conn, tax.taxonomy_id).items() if k in weekly}
        ranking = rank(tax, pairs, self.config.analysis.min_reviews_per_highlighted_theme)
        put_result(self.conn, state["run_id"], "ranking", [r.model_dump(mode="json") for r in ranking])
        if sum(r.highlighted for r in ranking) != 3:
            return {"status": "INSUFFICIENT_EVIDENCE", "reason_code": "TOO_FEW_THEMES"}
        return {"supported_themes": 3}

    def select_quotes(self, state):
        if get_result(self.conn, state["run_id"], "analysis"):
            return {"analysis_id": str(self.analysis(state).analysis_id)}
        from groww_pulse.schemas import QuoteEvidence, ThemeRank
        snap, tax = self.snapshot(state), self.taxonomy(state)
        ranking = tuple(ThemeRank.model_validate(r) for r in get_result(self.conn, state["run_id"], "ranking"))
        reviews = [r for r in load(self.conn, snap.snapshot_id) if r.weekly]
        pairs = store.assignments(self.conn, tax.taxonomy_id)
        definitions = {t.theme_id: t.definition for t in tax.themes}
        quotes, used = [], set()
        for theme in (r for r in ranking if r.highlighted):
            key = "quote_" + theme.theme_id
            saved = get_result(self.conn, state["run_id"], key)
            pool = candidates_for(theme, reviews, pairs, used, snap.snapshot_hash)
            quote = QuoteEvidence.model_validate(saved) if saved else select_quote(
                self.llm, theme, definitions[theme.theme_id], pool,
                self.config.execution.model_batch_attempts, self.config.model.reasoning_effort)
            if quote is None:
                return {"status": "INSUFFICIENT_EVIDENCE", "reason_code": "NO_VALID_QUOTE"}
            put_result(self.conn, state["run_id"], key, quote.model_dump(mode="json"))
            used.add(str(quote.review_id))
            quotes.append(quote)
        analysis = AnalysisResult(analysis_id=uuid4(), snapshot_id=snap.snapshot_id,
            taxonomy_id=tax.taxonomy_id, outcome="READY", eligible_weekly=len(reviews),
            ranking=ranking, quotes=tuple(quotes), usage=self.budget.summary())
        store.save_analysis(self.conn, analysis)
        put_result(self.conn, state["run_id"], "analysis", analysis.model_dump(mode="json"))
        return {"analysis_id": str(analysis.analysis_id)}

    def compose_pulse(self, state):
        attempt = state.get("compose_attempts", 0) + 1
        key = f"composition_{attempt}"
        if not get_result(self.conn, state["run_id"], key):
            try:
                comp = propose(self.llm, self.analysis(state), self.snapshot(state), self.config,
                               state.get("validation_errors", ()), state.get("word_count", 0))
                # Reject unsafe generated text before it reaches any persistence boundary.
                texts = [s.text for s in comp.summaries] + [a.text for a in comp.actions]
                value = ({"errors": ["PRIVACY"]} if any(detect(t, self.names) for t in texts)
                         else {"composition": comp.model_dump(mode="json")})
            except PulseError as exc:
                if exc.code != ErrorCode.LLM_OUTPUT_INVALID:
                    raise
                value = {"errors": ["COMPOSITION_SCHEMA"]}
            put_result(self.conn, state["run_id"], key, value)
        return {"compose_attempts": attempt}

    def validate_pulse(self, state):
        data = get_result(self.conn, state["run_id"], f"composition_{state['compose_attempts']}")
        pulse = None
        if "composition" not in data:
            report = {"valid": False, "errors": data["errors"], "word_count": 0}
        else:
            pulse, report = validate(self.conn, self.config, self.analysis(state), self.snapshot(state),
                                    Composition.model_validate(data["composition"]), self.names)
        put_result(self.conn, state["run_id"], "validation", report)
        if pulse:
            put_result(self.conn, state["run_id"], "pulse", pulse.model_dump(mode="json"))
            write_artifacts(self.artifacts, state["run_id"], pulse, report, self.llm.model_ref)
        update = {"validation_errors": report["errors"], "word_count": report["word_count"]}
        if not pulse and state["compose_attempts"] > state["repair_cap"]:
            update.update(status="VALIDATION_FAILED", reason_code="VALIDATION_FAILED")
        return update

    def preview_ready(self, state):
        return {"status": "PREVIEW_READY", "reason_code": "PREVIEW_READY"}

    def publication_step(self, state, name):
        if self.publisher is None:
            raise PulseError(ErrorCode.MCP_UNAVAILABLE)
        pulse = Pulse.model_validate(get_result(self.conn, state["run_id"], "pulse"))
        getattr(self.publisher, name)(state, pulse, self.snapshot(state))
        receipt = self.publisher.receipt(state, pulse, self.snapshot(state))
        update = {key: receipt[key] for key in ("document_id", "document_url", "draft_id", "layout_status")
                  if receipt[key] is not None}
        if name == "verify_draft":
            update.update(status="COMPLETE", reason_code="COMPLETE")
        return update

    def check_capabilities(self, state):
        return self.publication_step(state, "check_capabilities")

    def publish_doc(self, state):
        return self.publication_step(state, "publish_doc")

    def verify_doc(self, state):
        return self.publication_step(state, "verify_doc")

    def create_draft(self, state):
        return self.publication_step(state, "create_draft")

    def verify_draft(self, state):
        return self.publication_step(state, "verify_draft")
