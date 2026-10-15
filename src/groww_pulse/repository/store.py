import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.schemas import (
    AnalysisResult,
    DatasetSnapshot,
    ImportCounts,
    ImportManifest,
    SanitisedReview,
    Taxonomy,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def insert_import(conn: sqlite3.Connection, import_id: UUID, manifest: ImportManifest,
                  sanitiser_version: str) -> None:
    conn.execute(
        "INSERT INTO imports VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(import_id), _now(), manifest.store, manifest.product_id,
         manifest.model_dump_json(), ImportCounts().model_dump_json(), sanitiser_version),
    )


def finish_import(conn: sqlite3.Connection, import_id: UUID, counts: ImportCounts) -> None:
    conn.execute("UPDATE imports SET counts_json = ? WHERE import_id = ?",
                 (counts.model_dump_json(), str(import_id)))


def add_review(conn: sqlite3.Connection, review: SanitisedReview, import_id: UUID) -> tuple[UUID, bool]:
    """Insert unless an identical sanitised row exists; link it to this import either way."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(review.review_id), review.fingerprint, str(import_id), review.review_date.isoformat(),
         review.date_precision, review.rating,
         review.title.model_dump_json() if review.title else None,
         review.text.model_dump_json(), review.language, review.sanitiser_version),
    )
    inserted = cursor.rowcount == 1
    review_id = review.review_id if inserted else UUID(conn.execute(
        "SELECT review_id FROM reviews WHERE fingerprint = ?", (review.fingerprint,)
    ).fetchone()[0])
    conn.execute("INSERT OR IGNORE INTO import_reviews VALUES (?, ?)", (str(import_id), str(review_id)))
    return review_id, inserted


def get_import(conn: sqlite3.Connection, import_id: UUID | None = None
               ) -> tuple[UUID, ImportManifest, ImportCounts]:
    row = conn.execute(
        "SELECT import_id, manifest_json, counts_json FROM imports "
        + ("WHERE import_id = ?" if import_id else "ORDER BY created_at DESC, rowid DESC LIMIT 1"),
        (str(import_id),) if import_id else (),
    ).fetchone()
    if row is None:
        raise PulseError(ErrorCode.IMPORT_NOT_FOUND)
    return (UUID(row["import_id"]), ImportManifest.model_validate_json(row["manifest_json"]),
            ImportCounts.model_validate_json(row["counts_json"]))


def import_reviews_between(conn: sqlite3.Connection, import_id: UUID, start: date, end: date
                           ) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT r.review_id, r.fingerprint, r.review_date, r.rating FROM reviews r "
        "JOIN import_reviews ir ON ir.review_id = r.review_id "
        "WHERE ir.import_id = ? AND r.review_date >= ? AND r.review_date < ? "
        "ORDER BY r.review_date, r.fingerprint",
        (str(import_id), start.isoformat(), end.isoformat()),
    ).fetchall()


def save_snapshot(conn: sqlite3.Connection, snapshot: DatasetSnapshot,
                  members: Iterable[tuple[str, bool]]) -> None:
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?)",
            (str(snapshot.snapshot_id), str(snapshot.import_id), snapshot.snapshot_hash,
             snapshot.model_dump_json(), snapshot.outcome, snapshot.created_at.isoformat()),
        )
        conn.executemany("INSERT INTO snapshot_members VALUES (?, ?, ?)",
                         [(str(snapshot.snapshot_id), rid, int(week)) for rid, week in members])
        conn.execute("COMMIT")
    except sqlite3.Error:
        conn.execute("ROLLBACK")
        raise PulseError(ErrorCode.STORE_UNAVAILABLE) from None


def get_snapshot(conn: sqlite3.Connection, snapshot_id: UUID) -> DatasetSnapshot:
    row = conn.execute("SELECT snapshot_json FROM snapshots WHERE snapshot_id = ?",
                       (str(snapshot_id),)).fetchone()
    if row is None:
        raise PulseError(ErrorCode.SNAPSHOT_NOT_FOUND)
    return DatasetSnapshot.model_validate_json(row[0])


def new_id() -> UUID:
    return uuid4()


# --- Phase 2: analysis evidence -------------------------------------------------------------

def snapshot_reviews(conn: sqlite3.Connection, snapshot_id: UUID, weekly: bool | None = None
                     ) -> list[sqlite3.Row]:
    """Sanitised members of a snapshot in a stable order (date, fingerprint)."""
    where = "" if weekly is None else " AND m.in_report_week = ?"
    params: tuple = (str(snapshot_id),) if weekly is None else (str(snapshot_id), int(weekly))
    return conn.execute(
        "SELECT r.review_id, r.fingerprint, r.review_date, r.rating, r.text_json, m.in_report_week "
        "FROM snapshot_members m JOIN reviews r ON r.review_id = m.review_id "
        "WHERE m.snapshot_id = ?" + where + " ORDER BY r.review_date, r.fingerprint",
        params,
    ).fetchall()


def find_taxonomy(conn: sqlite3.Connection, snapshot_id: UUID, model_ref: str, prompt_version: str
                  ) -> Taxonomy | None:
    row = conn.execute(
        "SELECT taxonomy_json FROM taxonomies WHERE snapshot_id = ? AND model_ref = ? "
        "AND prompt_version = ? ORDER BY created_at DESC LIMIT 1",
        (str(snapshot_id), model_ref, prompt_version),
    ).fetchone()
    return Taxonomy.model_validate_json(row[0]) if row else None


def save_taxonomy(conn: sqlite3.Connection, taxonomy: Taxonomy) -> None:
    conn.execute("INSERT INTO taxonomies VALUES (?, ?, ?, ?, ?, ?)",
                 (str(taxonomy.taxonomy_id), str(taxonomy.snapshot_id), taxonomy.model_ref,
                  taxonomy.prompt_version, taxonomy.model_dump_json(), _now()))


def assignments(conn: sqlite3.Connection, taxonomy_id: UUID) -> dict[str, str]:
    return dict(conn.execute("SELECT review_id, theme_id FROM assignments WHERE taxonomy_id = ?",
                             (str(taxonomy_id),)).fetchall())


def save_assignments(conn: sqlite3.Connection, taxonomy_id: UUID, pairs: dict[str, str]) -> None:
    conn.execute("BEGIN")
    try:
        conn.executemany("INSERT OR REPLACE INTO assignments VALUES (?, ?, ?)",
                         [(str(taxonomy_id), rid, tid) for rid, tid in pairs.items()])
        conn.execute("COMMIT")
    except sqlite3.Error:
        conn.execute("ROLLBACK")
        raise PulseError(ErrorCode.STORE_UNAVAILABLE) from None


def save_analysis(conn: sqlite3.Connection, result: AnalysisResult) -> None:
    conn.execute("INSERT INTO analyses VALUES (?, ?, ?, ?, ?, ?)",
                 (str(result.analysis_id), str(result.snapshot_id),
                  str(result.taxonomy_id) if result.taxonomy_id else None,
                  result.outcome, result.model_dump_json(), _now()))


def latest_snapshot_id(conn: sqlite3.Connection) -> UUID:
    row = conn.execute("SELECT snapshot_id FROM snapshots ORDER BY created_at DESC, rowid DESC LIMIT 1"
                       ).fetchone()
    if row is None:
        raise PulseError(ErrorCode.SNAPSHOT_NOT_FOUND)
    return UUID(row[0])
