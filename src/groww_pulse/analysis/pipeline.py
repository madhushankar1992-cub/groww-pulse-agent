import sqlite3
from uuid import UUID, uuid4

from groww_pulse.analysis import PROMPT_VERSION
from groww_pulse.analysis import classify as classify_mod
from groww_pulse.analysis.corpus import discovery_sample, load
from groww_pulse.analysis.quotes import candidates_for, select_quote
from groww_pulse.analysis.ranking import rank
from groww_pulse.analysis.taxonomy import discover
from groww_pulse.config import RunConfig
from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.llm.base import LLM, Budget
from groww_pulse.repository import store
from groww_pulse.schemas import AnalysisResult


def run_analysis(conn: sqlite3.Connection, config: RunConfig, snapshot_id: UUID, llm: LLM,
                 budget: Budget) -> AnalysisResult:
    """Taxonomy → weekly classification → ranking → quotes. Reuses stored work on rerun."""
    snapshot = store.get_snapshot(conn, snapshot_id)
    from groww_pulse.analysis.corpus import require_current_filter
    require_current_filter(conn, snapshot_id)
    if snapshot.outcome == "COVERAGE_MISSING":
        raise PulseError(ErrorCode.COVERAGE_MISSING)
    model, analysis = config.model, config.analysis
    attempts, workers = config.execution.model_batch_attempts, config.execution.max_concurrent_model_calls

    def finish(outcome, reason=None, taxonomy_id=None, weekly=0, ranking=(), quotes=()):
        result = AnalysisResult(
            analysis_id=uuid4(), snapshot_id=snapshot_id, taxonomy_id=taxonomy_id,
            outcome=outcome, reason_code=reason, eligible_weekly=weekly, ranking=ranking,
            quotes=quotes, usage=budget.summary())
        store.save_analysis(conn, result)
        return result

    reviews = load(conn, snapshot_id)
    weekly = [r for r in reviews if r.weekly]
    # Check the cheapest stop condition before spending any tokens.
    if len(weekly) < analysis.min_weekly_reviews:
        return finish("INSUFFICIENT_EVIDENCE", ErrorCode.TOO_FEW_WEEKLY_REVIEWS, weekly=len(weekly))

    taxonomy = store.find_taxonomy(conn, snapshot_id, llm.model_ref, PROMPT_VERSION)
    if taxonomy is None:
        sample = discovery_sample(reviews, model.discovery_sample_size, snapshot.snapshot_hash)
        taxonomy = discover(llm, sample, snapshot_id, analysis.max_themes, attempts,
                            model.reasoning_effort)
        store.save_taxonomy(conn, taxonomy)

    known = store.assignments(conn, taxonomy.taxonomy_id)
    todo = [r for r in weekly if r.review_id not in known]
    if todo:
        classify_mod.classify(
            llm, taxonomy, todo, model.classify_batch_size, attempts, workers, model.classify_effort,
            on_batch=lambda done: store.save_assignments(conn, taxonomy.taxonomy_id, done))
        known = store.assignments(conn, taxonomy.taxonomy_id)
    weekly_assignment = {r.review_id: known[r.review_id] for r in weekly}

    ranking = rank(taxonomy, weekly_assignment, analysis.min_reviews_per_highlighted_theme)
    highlighted = [t for t in ranking if t.highlighted]
    if len(highlighted) < analysis.highlighted_themes:
        return finish("INSUFFICIENT_EVIDENCE", ErrorCode.TOO_FEW_THEMES, taxonomy.taxonomy_id,
                      len(weekly), ranking)

    definitions = {t.theme_id: t.definition for t in taxonomy.themes}
    used: set[str] = set()
    quotes = []
    for theme in highlighted:
        pool = candidates_for(theme, weekly, weekly_assignment, used, snapshot.snapshot_hash)
        quote = select_quote(llm, theme, definitions[theme.theme_id], pool, attempts,
                             model.reasoning_effort) if pool else None
        if quote is None:
            return finish("INSUFFICIENT_EVIDENCE", ErrorCode.NO_VALID_QUOTE, taxonomy.taxonomy_id,
                          len(weekly), ranking)
        used.add(str(quote.review_id))
        quotes.append(quote)
    return finish("READY", None, taxonomy.taxonomy_id, len(weekly), ranking, tuple(quotes))
