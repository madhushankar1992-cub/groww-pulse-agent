import hashlib
import json
import sqlite3
from collections import Counter
from datetime import UTC, date, datetime
from uuid import UUID

from groww_pulse.config import RunConfig
from groww_pulse.ingestion.dates import reporting_window
from groww_pulse.repository import store
from groww_pulse.schemas import DatasetSnapshot, SnapshotCounts


def create_snapshot(conn: sqlite3.Connection, config: RunConfig, import_id: UUID | None = None,
                    as_of: date | None = None) -> DatasetSnapshot:
    """Freeze the reviews of one import that fall inside the history window.

    Coverage is judged against the import's manifest so that a missing export is
    never confused with a genuinely quiet week.
    """
    import_id, manifest, import_counts = store.get_import(conn, import_id)
    window = reporting_window(config.reporting, as_of)
    rows = store.import_reviews_between(conn, import_id, window.history_start, window.report_end)
    weekly = [r for r in rows if r["review_date"] >= window.report_start.isoformat()]

    week_covered = (manifest.coverage_start <= window.report_start
                    and manifest.coverage_end >= window.report_end)
    if not week_covered:
        outcome = "COVERAGE_MISSING"
    elif not rows:
        outcome = "NO_ELIGIBLE_REVIEWS"
    elif not weekly:
        outcome = "EMPTY_WEEK"
    else:
        outcome = "READY"

    ratings = Counter(str(r["rating"]) if r["rating"] else "none" for r in weekly)
    digest = hashlib.sha256(json.dumps(
        [window.model_dump(mode="json"), [r["fingerprint"] for r in rows]]
    ).encode()).hexdigest()

    snapshot = DatasetSnapshot(
        snapshot_id=store.new_id(),
        import_id=import_id,
        snapshot_hash=digest,
        window=window,
        counts=SnapshotCounts(
            history_reviews=len(rows), weekly_reviews=len(weekly),
            weekly_by_rating=dict(sorted(ratings.items())),
            import_excluded=import_counts.excluded,
        ),
        history_complete=manifest.coverage_start <= window.history_start and not manifest.truncated,
        source_status=manifest.coverage_status,
        outcome=outcome,
        created_at=datetime.now(UTC),
    )
    store.save_snapshot(conn, snapshot, [
        (r["review_id"], r["review_date"] >= window.report_start.isoformat()) for r in rows
    ])
    return snapshot
