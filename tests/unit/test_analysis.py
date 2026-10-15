import json
import re
from datetime import date, timedelta

import pytest

from groww_pulse.analysis.classify import classify_batch
from groww_pulse.analysis.corpus import Review, load
from groww_pulse.analysis.pipeline import run_analysis
from groww_pulse.analysis.quotes import locate, select_quote, verify
from groww_pulse.analysis.ranking import rank
from groww_pulse.config import ModelConfig
from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.ingestion.importer import import_export
from groww_pulse.ingestion.manifest import load_manifest
from groww_pulse.ingestion.snapshot import create_snapshot
from groww_pulse.llm.base import Budget
from groww_pulse.privacy.sanitiser import sanitise
from groww_pulse.schemas import Taxonomy, Theme, ThemeRank
from tests.conftest import manifest_with, write_csv

TODAY = date(2026, 10, 9)
WEEK = date(2026, 9, 28)

# Fictional reviews; the first word picks the theme in the fake model.
TOPICS = {
    "withdrawals": ["Withdrawal of my money to the bank is stuck for three days now",
                    "Withdrawal request pending since Monday and support gives no clear timeline"],
    "payments": ["Payment through UPI failed but the amount was debited from my account",
                 "Payment page keeps loading forever when I try to add money for SIP"],
    "performance": ["Performance is terrible at market open, the app freezes and orders lag",
                    "Performance of charts is slow and the app crashes during trading hours"],
    "praise": ["Praise for this app, very simple to invest in mutual funds every month",
               "Praise from me, smooth experience and clean design for a new investor"],
}
THEMES = [{"theme_id": k, "name": k.title(), "definition": f"Reviews about {k} issues",
           "is_catch_all": k == "praise"} for k in TOPICS]


def topic_of(text: str) -> str:
    return next(t for t in TOPICS if text.lower().startswith(t[:6]))


class FakeLLM:
    model_ref = "fake-model"

    def __init__(self, themes=None, quote_mode="exact", budget=None):
        self.themes = THEMES if themes is None else themes
        self.quote_mode = quote_mode
        self.calls: list[str] = []
        self.budget = budget

    def complete_json(self, *, system, user, schema, effort, max_tokens):
        kind = next(iter(schema["properties"]))
        self.calls.append(kind)
        if self.budget:
            self.budget.record(1000, 100)
        reviews = json.loads(user[user.rindex("Reviews (JSON):") + len("Reviews (JSON):"):])
        if kind == "themes":
            return {"themes": self.themes}
        if kind == "assignments":
            return {"assignments": [{"id": r["id"], "theme_id": topic_of(r["text"])}
                                    for r in reviews]}
        if self.quote_mode == "fabricated":
            return {"candidates": [{"id": reviews[0]["id"], "excerpt": "Users love this amazing app a lot"}]}
        return {"candidates": [{"id": reviews[0]["id"], "excerpt": reviews[0]["text"]}]}


def seed_reviews(conn, config, tmp_path, per_topic=(5, 4, 3, 6)):
    rows = []
    for (topic, texts), n in zip(TOPICS.items(), per_topic, strict=True):
        for i in range(n):
            day = WEEK + timedelta(days=i % 7)
            rows.append({"userName": "x", "reviewId": f"{topic}{i}", "score": "2",
                         "at": f"{day.isoformat()}T10:{i:02d}:00+05:30",
                         "content": f"{texts[i % 2]} (case {i})"})
    rows.append({"userName": "x", "reviewId": "old", "score": "3", "at": "2026-08-10T10:00:00+05:30",
                 "content": TOPICS["payments"][0]})
    manifest = load_manifest(manifest_with(tmp_path))
    import_export(conn, config, manifest, write_csv(tmp_path / "a.csv", rows), None, TODAY)
    return create_snapshot(conn, config, as_of=TODAY)


def test_pipeline_ready(conn, config, tmp_path):
    snap = seed_reviews(conn, config, tmp_path)
    llm = FakeLLM()
    result = run_analysis(conn, config, snap.snapshot_id, llm, Budget(config.model))
    assert result.outcome == "READY" and result.eligible_weekly == 18
    highlighted = [r.theme_id for r in result.ranking if r.highlighted]
    # Praise is the largest group but is the catch-all, so it is never highlighted.
    assert highlighted == ["withdrawals", "payments", "performance"]
    counts = dict(conn.execute(
        "SELECT a.theme_id, COUNT(*) FROM assignments a JOIN snapshot_members m "
        "ON m.review_id = a.review_id AND m.in_report_week = 1 GROUP BY a.theme_id").fetchall())
    assert {r.theme_id: r.count for r in result.ranking} == counts
    stored = {r.review_id: r.text.evidence_text for r in load(conn, snap.snapshot_id)}
    for quote in result.quotes:
        assert quote.excerpt in stored[str(quote.review_id)]
    assert len({q.review_id for q in result.quotes}) == 3


def test_rerun_reuses_taxonomy_and_assignments(conn, config, tmp_path):
    snap = seed_reviews(conn, config, tmp_path)
    run_analysis(conn, config, snap.snapshot_id, FakeLLM(), Budget(config.model))
    llm = FakeLLM()
    run_analysis(conn, config, snap.snapshot_id, llm, Budget(config.model))
    assert "themes" not in llm.calls and "assignments" not in llm.calls


def test_too_few_weekly_reviews_costs_nothing(conn, config, tmp_path):
    snap = seed_reviews(conn, config, tmp_path, per_topic=(2, 2, 2, 2))
    llm = FakeLLM()
    result = run_analysis(conn, config, snap.snapshot_id, llm, Budget(config.model))
    assert result.outcome == "INSUFFICIENT_EVIDENCE"
    assert result.reason_code is ErrorCode.TOO_FEW_WEEKLY_REVIEWS and llm.calls == []


def test_too_few_supported_themes(conn, config, tmp_path):
    snap = seed_reviews(conn, config, tmp_path, per_topic=(6, 1, 1, 6))
    result = run_analysis(conn, config, snap.snapshot_id, FakeLLM(), Budget(config.model))
    assert result.outcome == "INSUFFICIENT_EVIDENCE" and result.reason_code is ErrorCode.TOO_FEW_THEMES


def test_six_themes_rejected(conn, config, tmp_path):
    snap = seed_reviews(conn, config, tmp_path)
    six = THEMES + [{"theme_id": f"extra{i}", "name": f"Extra {i}", "definition": "Something else",
                     "is_catch_all": False} for i in range(2)]
    with pytest.raises(PulseError) as err:
        run_analysis(conn, config, snap.snapshot_id, FakeLLM(themes=six), Budget(config.model))
    assert err.value.code is ErrorCode.TAXONOMY_INVALID


def test_fabricated_quote_rejected_and_fallback_is_verbatim(conn, config, tmp_path):
    snap = seed_reviews(conn, config, tmp_path)
    result = run_analysis(conn, config, snap.snapshot_id, FakeLLM(quote_mode="fabricated"),
                          Budget(config.model))
    stored = {r.review_id: r.text.evidence_text for r in load(conn, snap.snapshot_id)}
    assert result.outcome == "READY"
    for quote in result.quotes:
        assert "amazing" not in quote.excerpt and quote.excerpt in stored[str(quote.review_id)]


def test_budget_stops_run(conn, config, tmp_path):
    snap = seed_reviews(conn, config, tmp_path)
    budget = Budget(ModelConfig(max_input_tokens_per_run=1500))
    with pytest.raises(PulseError) as err:
        run_analysis(conn, config, snap.snapshot_id, FakeLLM(budget=budget), budget)
    assert err.value.code is ErrorCode.BUDGET_EXCEEDED


def make_review(text, rid="00000000-0000-0000-0000-0000000000aa", weekly=True):
    return Review(rid, WEEK, 2, sanitise(text, config_privacy(), None), weekly)


def config_privacy():
    from groww_pulse.config import PrivacyConfig
    return PrivacyConfig()


TAXONOMY = Taxonomy(taxonomy_id="00000000-0000-0000-0000-000000000001",
                    snapshot_id="00000000-0000-0000-0000-000000000002", model_ref="m",
                    prompt_version="p", themes=tuple(Theme(**t) for t in THEMES))


class ScriptedLLM:
    model_ref = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)

    def complete_json(self, **kwargs):
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_classify_retries_missing_and_ignores_invented_ids():
    a, b = "00000000-0000-0000-0000-00000000000a", "00000000-0000-0000-0000-00000000000b"
    batch = [make_review(TOPICS["payments"][0], a), make_review(TOPICS["withdrawals"][0], b)]
    llm = ScriptedLLM([
        {"assignments": [{"id": "r1", "theme_id": "payments"}, {"id": "r9", "theme_id": "payments"},
                         {"id": "r2", "theme_id": "not_a_theme"}]},
        {"assignments": [{"id": "r1", "theme_id": "withdrawals"}]},  # r1 is now review "b"
    ])
    assert classify_batch(llm, TAXONOMY, batch, 3, "low") == {a: "payments", b: "withdrawals"}


def test_classify_gives_up_after_attempts():
    llm = ScriptedLLM([{"assignments": []}] * 3)
    with pytest.raises(PulseError) as err:
        classify_batch(llm, TAXONOMY, [make_review(TOPICS["payments"][0])], 3, "low")
    assert err.value.code is ErrorCode.CLASSIFICATION_FAILED


def test_quote_must_not_cross_redaction():
    review = make_review("Support never replied to my mail at someone@example.com about the stuck withdrawal")
    text = review.text.evidence_text
    assert verify(review, "withdrawals", 0, len(text)) is None
    start = text.index("Support")
    end = text.index(" at [redacted]")
    quote = verify(review, "withdrawals", start, end)
    assert quote and quote.excerpt == "Support never replied to my mail"


def test_locate_tolerates_whitespace_only():
    text = "The app is slow\n\nat market open every day"
    assert locate(text, "slow at market open") == (11, len("The app is slow\n\nat market open"))
    assert locate(text, "slow at the market open") is None


def test_select_quote_prefers_model_excerpt():
    review = make_review("Withdrawal is stuck for days. Support is not helping at all with my issue.")
    llm = ScriptedLLM([{"candidates": [{"id": "r1", "excerpt": "Support is not helping at all with my issue."}]}])
    theme = ThemeRank(theme_id="withdrawals", name="W", count=3, share=0.5, highlighted=True)
    quote = select_quote(llm, theme, "def", [review], 3, "medium")
    assert quote.excerpt == "Support is not helping at all with my issue."
    assert re.search(r"\w", quote.excerpt) and quote.word_count == 9


def test_ranking_tie_break_and_catch_all():
    assignment = {"1": "payments", "2": "payments", "3": "withdrawals", "4": "withdrawals",
                  "5": "praise", "6": "praise", "7": "praise", "8": "performance", "9": "performance"}
    ranking = rank(TAXONOMY, assignment, 2)
    assert [r.theme_id for r in ranking] == ["praise", "payments", "performance", "withdrawals"]
    assert [r.theme_id for r in ranking if r.highlighted] == ["payments", "performance", "withdrawals"]
