import json
import re
from pathlib import Path

import pytest

from groww_pulse.errors import PulseError
from groww_pulse.evaluation.harness import evaluate, load_dataset, reference_taxonomy

DATA = Path("tests/eval/labelled_reviews.jsonl")
THEMES = Path("tests/eval/reference_themes.json")


class Oracle:
    """Fake model that classifies from a lookup; ``wrong`` flips that share of answers."""

    model_ref = "oracle"

    def __init__(self, rows, wrong=0.0):
        self.by_text = {r["text"]: r["theme"] for r in rows}
        self.themes = sorted(set(self.by_text.values()))
        self.wrong, self.seen = wrong, 0

    def complete_json(self, *, system, user, schema, effort, max_tokens):
        reviews = json.loads(user[user.rindex("Reviews (JSON):") + len("Reviews (JSON):"):])
        out = []
        for r in reviews:
            theme = self.by_text[r["text"]]
            self.seen += 1
            if self.wrong and self.seen % round(1 / self.wrong) == 0:
                theme = next(t for t in self.themes if t != theme)
            out.append({"id": r["id"], "theme_id": theme})
        return {"assignments": out}


def test_dataset_is_fictional_clean_and_sized():
    rows = load_dataset(DATA)
    assert 60 <= len(rows) <= 100
    taxonomy = reference_taxonomy(THEMES)
    valid = {t.theme_id for t in taxonomy.themes}
    for row in rows:
        assert row["theme"] in valid
        assert len(row["text"].split()) > 8 and row["text"].isascii()
        assert not re.search(r"@|https?://|\d{6,}", row["text"])
    assert len({r["text"] for r in rows}) == len(rows)


def test_perfect_classifier_meets_all_targets():
    rows = load_dataset(DATA)
    report = evaluate(Oracle(rows), rows, reference_taxonomy(THEMES))
    assert report["theme_agreement"] == 1.0 and report["count_accuracy"] == 1.0
    assert report["quote_validity"] == 1.0 and report["quotes_checked"] == len(rows)
    assert report["passed"] and report["disagreements"] == []


def test_weak_classifier_fails_the_agreement_target():
    rows = load_dataset(DATA)
    report = evaluate(Oracle(rows, wrong=0.25), rows, reference_taxonomy(THEMES))
    assert report["theme_agreement"] < 0.85 and not report["passed"]
    assert report["disagreements"]


def test_bad_dataset_is_rejected(tmp_path):
    path = tmp_path / "small.jsonl"
    path.write_text(json.dumps({"id": "a", "text": "short", "theme": "x"}) + "\n", encoding="utf-8")
    with pytest.raises(PulseError, match="FORMAT_INVALID"):
        load_dataset(path)
