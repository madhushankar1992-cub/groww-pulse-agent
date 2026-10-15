import hashlib
import json
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

from groww_pulse.config import RunConfig
from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.ingestion.dates import parse_review_date, reporting_window
from groww_pulse.ingestion.readers import iter_rows
from groww_pulse.privacy.detectors import NameDetector
from groww_pulse.privacy.eligibility import check_eligibility
from groww_pulse.privacy.sanitiser import REDACTION, SANITISER_VERSION, WORD, sanitise
from groww_pulse.repository import store
from groww_pulse.schemas import ImportCounts, ImportManifest, ReportingWindow, SanitisedReview


def fingerprint(review_date: date, rating: int | None, title: str | None, text: str) -> str:
    payload = json.dumps(["google_play", "com.nextbillion.groww", review_date.isoformat(),
                          rating, title, text], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_rating(value: Any) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise PulseError(ErrorCode.RATING_INVALID) from None
    if not number.is_integer() or not 1 <= number <= 5:
        raise PulseError(ErrorCode.RATING_INVALID)
    return int(number)


def build_review(row: dict[str, Any], manifest: ImportManifest, config: RunConfig,
                 window: ReportingWindow, today: date, names: NameDetector | None) -> SanitisedReview:
    """Map, validate and sanitise one raw row. Identity columns are never read."""
    cols = manifest.columns
    if cols.product_id and row.get(cols.product_id) != manifest.product_id:
        raise PulseError(ErrorCode.PRODUCT_MISMATCH)
    if cols.language and (row.get(cols.language) or "en").lower() not in config.privacy.supported_languages:
        raise PulseError(ErrorCode.LANGUAGE_UNSUPPORTED)

    raw_date = row.get(cols.date)
    if not isinstance(raw_date, str):
        raise PulseError(ErrorCode.DATE_INVALID)
    review_date, source_date = parse_review_date(raw_date.strip(), manifest, window.timezone)
    if review_date > today:
        raise PulseError(ErrorCode.DATE_INVALID)
    if not manifest.coverage_start <= source_date < manifest.coverage_end:
        raise PulseError(ErrorCode.OUTSIDE_COVERAGE)
    if review_date < window.history_start:
        raise PulseError(ErrorCode.OUTSIDE_HISTORY)

    rating = parse_rating(row.get(cols.rating)) if cols.rating else None
    raw_text = row.get(cols.text)
    if not isinstance(raw_text, str):
        raise PulseError(ErrorCode.TEXT_EMPTY)
    text = sanitise(raw_text, config.privacy, names)
    if len(WORD.findall(text.evidence_text.replace(REDACTION, ""))) < config.ingestion.min_review_words:
        raise PulseError(ErrorCode.TEXT_TOO_SHORT)
    check_eligibility(text.evidence_text)

    title = None
    raw_title = row.get(cols.title) if cols.title else None
    if isinstance(raw_title, str) and raw_title.strip():
        try:
            title = sanitise(raw_title, config.privacy, names, required=False)
        except PulseError:
            title = None  # An unsafe title is dropped; the review body can still be used.

    return SanitisedReview(
        review_id=store.new_id(),
        review_date=review_date,
        date_precision=manifest.date_precision,
        rating=rating,
        title=title,
        text=text,
        sanitiser_version=SANITISER_VERSION,
        fingerprint=fingerprint(review_date, rating, title.evidence_text if title else None,
                                text.evidence_text),
    )


def import_export(conn: sqlite3.Connection, config: RunConfig, manifest: ImportManifest, path: Path,
                  names: NameDetector | None, as_of: date | None = None) -> tuple[UUID, ImportCounts]:
    """Stream, sanitise and store one export atomically. Returns only content-free counts."""
    window = reporting_window(config.reporting, as_of)
    today = as_of or date.today()
    import_id = store.new_id()
    excluded: Counter[ErrorCode] = Counter()
    seen: Counter[str] = Counter()
    rows_seen = eligible = duplicates = inserted = reused = weekly = 0

    conn.execute("BEGIN")
    try:
        store.insert_import(conn, import_id, manifest, SANITISER_VERSION)
        for row in iter_rows(path, manifest, config.storage.max_input_bytes, config.storage.max_rows):
            rows_seen += 1
            if row is None:
                excluded[ErrorCode.ROW_INVALID] += 1
                continue
            try:
                review = build_review(row, manifest, config, window, today, names)
            except PulseError as exc:
                if exc.code is ErrorCode.PRIVACY_MODEL_UNAVAILABLE:
                    raise
                excluded[exc.code] += 1
                continue
            eligible += 1
            # Identical short reviews ("good app") from different people on the same day are
            # distinct reviews. The n-th identical row keeps its own identity, so re-importing
            # the same export still inserts nothing while real repeats are still counted.
            occurrence = seen[review.fingerprint]
            seen[review.fingerprint] += 1
            duplicates += occurrence > 0
            if occurrence:
                review = review.model_copy(update={"fingerprint": hashlib.sha256(
                    f"{review.fingerprint}:{occurrence}".encode()).hexdigest()})
            _, was_inserted = store.add_review(conn, review, import_id)
            inserted += was_inserted
            reused += not was_inserted
            weekly += window.report_start <= review.review_date < window.report_end
        counts = ImportCounts(
            rows_seen=rows_seen, eligible_rows=eligible, unique_rows=inserted + reused,
            duplicates=duplicates, inserted_rows=inserted, reused_rows=reused,
            weekly_rows=weekly, excluded=dict(sorted(excluded.items())),
        )
        store.finish_import(conn, import_id, counts)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return import_id, counts
