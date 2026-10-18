"""Score theme assignment, counting and quote validity on a labelled, fictional review set."""

import json
from collections import Counter
from datetime import date
from pathlib import Path
from uuid import UUID, uuid5

from groww_pulse.analysis.classify import classify
from groww_pulse.analysis.corpus import Review
from groww_pulse.analysis.quotes import fallback
from groww_pulse.analysis.ranking import rank
from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.llm.base import LLM, Effort
from groww_pulse.schemas import SafeSpan, SafeText, Taxonomy, Theme

NAMESPACE = UUID("6f1d2c1e-8a4b-4a52-9d57-3f4c1f0b7a10")
TARGETS = {"theme_agreement": 0.85, "quote_validity": 1.0, "count_accuracy": 1.0}


def load_dataset(path: Path) -> list[dict]:
    try:
        rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line]
    except (OSError, ValueError):
        raise PulseError(ErrorCode.FILE_UNREADABLE) from None
    if not 60 <= len(rows) <= 100 or any({"id", "text", "theme"} - row.keys() for row in rows):
        raise PulseError(ErrorCode.FORMAT_INVALID)
    return rows


def reference_taxonomy(path: Path) -> Taxonomy:
    themes = tuple(Theme.model_validate(t) for t in json.loads(Path(path).read_text(encoding="utf-8")))
    return Taxonomy(taxonomy_id=uuid5(NAMESPACE, "taxonomy"), snapshot_id=uuid5(NAMESPACE, "snapshot"),
                    model_ref="reference", prompt_version="eval-1", themes=themes)


def to_review(row: dict) -> Review:
    text = row["text"]
    span = SafeSpan(start=0, end=len(text), source_start=0, source_end=len(text))
    return Review(str(uuid5(NAMESPACE, row["id"])), date(2026, 10, 5), row.get("rating"),
                  SafeText(evidence_text=text, analysis_text=text, spans=(span,), redaction_count=0), True)


def evaluate(llm: LLM, rows: list[dict], taxonomy: Taxonomy, *, batch_size: int = 5,
             effort: Effort = "low") -> dict:
    reviews = [to_review(r) for r in rows]
    truth = {rv.review_id: r["theme"] for rv, r in zip(reviews, rows, strict=True)}
    predicted = classify(llm, taxonomy, reviews, batch_size, 3, 1, effort)
    hits = sum(predicted.get(rid) == theme for rid, theme in truth.items())
    ids = {rv.review_id: r["id"] for rv, r in zip(reviews, rows, strict=True)}
    # Counts must equal an independent recount of the assignments the model produced.
    ranking = rank(taxonomy, predicted, min_per_theme=1, highlights=len(taxonomy.themes))
    recount = Counter(predicted.values())
    count_ok = all(item.count == recount[item.theme_id] for item in ranking)
    quotes = [fallback(rv, predicted[rv.review_id]) for rv in reviews if rv.review_id in predicted]
    produced = [q for q in quotes if q is not None]
    by_id = {rv.review_id: rv for rv in reviews}
    valid = sum(by_id[str(q.review_id)].text.evidence_text.find(q.excerpt) >= 0 for q in produced)
    per_theme = {}
    for theme in taxonomy.themes:
        members = [rid for rid, t in truth.items() if t == theme.theme_id]
        per_theme[theme.theme_id] = {
            "reviews": len(members), "correct": sum(predicted.get(rid) == theme.theme_id for rid in members)}
    report = {
        "reviews": len(rows), "classified": len(predicted),
        "theme_agreement": round(hits / len(rows), 4),
        "count_accuracy": 1.0 if count_ok and sum(recount.values()) == len(predicted) else 0.0,
        "quote_validity": round(valid / len(produced), 4) if produced else 0.0,
        "quotes_checked": len(produced),
        "per_theme": per_theme,
        "disagreements": sorted(ids[rid] for rid, t in truth.items() if predicted.get(rid) != t),
        "targets": TARGETS,
    }
    report["passed"] = all(report[k] >= v for k, v in TARGETS.items())
    return report
