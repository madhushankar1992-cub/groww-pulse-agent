from datetime import date, datetime, timedelta, timezone

import pytest

from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.ingestion.fetcher import fetch_public_reviews
from groww_pulse.ingestion.importer import import_export
from groww_pulse.ingestion.manifest import load_manifest

TODAY = date(2026, 10, 9)
IST = timezone(timedelta(hours=5, minutes=30))


def fake_pages(pages_of_days_back: list[list[int]]):
    pages = iter(pages_of_days_back)

    def fetch(token):
        batch = next(pages, [])
        items = [{"userName": "CanaryUser77", "userImage": "https://img/x", "reviewId": "gp:1",
                  "content": f"Fictional review written {d} days ago about slow withdrawals to my bank account", "score": 3,
                  "at": datetime(2026, 10, 8, 12, tzinfo=IST) - timedelta(days=d)} for d in batch]
        return items, (object() if batch else None)

    return fetch


def test_requires_confirmation(config, tmp_path):
    with pytest.raises(PulseError) as err:
        fetch_public_reviews(config, tmp_path, confirmed=False, pages=fake_pages([]))
    assert err.value.code is ErrorCode.FETCH_NOT_CONFIRMED


def test_reaches_history_and_writes_minimal_export(config, conn, tmp_path):
    export, manifest_path, count = fetch_public_reviews(
        config, tmp_path, confirmed=True, today=TODAY, pages=fake_pages([[0, 5, 20], [40, 80]]))
    assert count == 5
    text = export.read_text(encoding="utf-8")
    assert "CanaryUser77" not in text and "userImage" not in text and "gp:1" not in text
    manifest = load_manifest(manifest_path)
    assert manifest.coverage_start == date(2026, 7, 27) and not manifest.truncated
    assert manifest.coverage_end == TODAY
    _, counts = import_export(conn, config, manifest, export, None, TODAY)
    # 80 days back is before the history window and the declared coverage.
    assert counts.eligible_rows == 4
    assert counts.excluded == {ErrorCode.OUTSIDE_COVERAGE: 1}


def test_truncated_when_page_limit_hit(config, tmp_path):
    _, manifest_path, _ = fetch_public_reviews(
        config, tmp_path, confirmed=True, today=TODAY, max_pages=1, pages=fake_pages([[1, 2], [3]]))
    manifest = load_manifest(manifest_path)
    assert manifest.truncated and manifest.coverage_start == date(2026, 10, 7)


def test_library_errors_become_codes(config, tmp_path):
    def boom(token):
        raise RuntimeError("network error text that might contain content")

    with pytest.raises(PulseError) as err:
        fetch_public_reviews(config, tmp_path, confirmed=True, today=TODAY, pages=boom)
    assert err.value.code is ErrorCode.FETCH_FAILED
