import hashlib
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from groww_pulse.repository import store
from groww_pulse.schemas import SafeText


def require_current_filter(conn: sqlite3.Connection, snapshot_id: UUID) -> None:
    from groww_pulse.errors import ErrorCode, PulseError
    from groww_pulse.privacy.sanitiser import SANITISER_VERSION

    row = conn.execute(
        "SELECT i.sanitiser_version FROM snapshots s JOIN imports i ON i.import_id=s.import_id "
        "WHERE s.snapshot_id=?", (str(snapshot_id),)).fetchone()
    if row is None:
        raise PulseError(ErrorCode.SNAPSHOT_NOT_FOUND)
    if row[0] != SANITISER_VERSION:
        raise PulseError(ErrorCode.FILTER_REFRESH_REQUIRED)


@dataclass(frozen=True)
class Review:
    review_id: str
    review_date: date
    rating: int | None
    text: SafeText
    weekly: bool


def load(conn: sqlite3.Connection, snapshot_id: UUID) -> list[Review]:
    return [
        Review(r["review_id"], date.fromisoformat(r["review_date"]), r["rating"],
               SafeText.model_validate_json(r["text_json"]), bool(r["in_report_week"]))
        for r in store.snapshot_reviews(conn, snapshot_id)
    ]


def stable_key(seed: str, review_id: str) -> str:
    return hashlib.sha256(f"{seed}:{review_id}".encode()).hexdigest()


def discovery_sample(reviews: list[Review], size: int, seed: str) -> list[Review]:
    """Deterministic sample stratified by ISO week and rating, always including the report week."""
    weekly = [r for r in reviews if r.weekly]
    history = [r for r in reviews if not r.weekly]
    if len(reviews) <= size:
        return sorted(reviews, key=lambda r: stable_key(seed, r.review_id))
    strata: dict[tuple, list[Review]] = defaultdict(list)
    for r in history:
        strata[(r.review_date.isocalendar()[:2], r.rating)].append(r)
    for bucket in strata.values():
        bucket.sort(key=lambda r: stable_key(seed, r.review_id))
    picked = sorted(weekly, key=lambda r: stable_key(seed, r.review_id))[: size // 3]
    queues = [strata[k] for k in sorted(strata, key=str)]
    while len(picked) < size and any(queues):
        for queue in queues:
            if queue and len(picked) < size:
                picked.append(queue.pop(0))
    return picked
