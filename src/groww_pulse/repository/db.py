import sqlite3
from pathlib import Path

from groww_pulse.errors import ErrorCode, PulseError

# Each entry upgrades the schema by one version; never edit a shipped migration.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE imports (
      import_id TEXT PRIMARY KEY,
      created_at TEXT NOT NULL,
      store TEXT NOT NULL CHECK (store = 'google_play'),
      product_id TEXT NOT NULL,
      manifest_json TEXT NOT NULL,
      counts_json TEXT NOT NULL,
      sanitiser_version TEXT NOT NULL
    );
    CREATE TABLE reviews (
      review_id TEXT PRIMARY KEY,
      fingerprint TEXT NOT NULL UNIQUE,
      first_import_id TEXT NOT NULL REFERENCES imports(import_id),
      review_date TEXT NOT NULL,
      date_precision TEXT NOT NULL,
      rating INTEGER CHECK (rating BETWEEN 1 AND 5),
      title_json TEXT,
      text_json TEXT NOT NULL,
      language TEXT NOT NULL,
      sanitiser_version TEXT NOT NULL
    );
    CREATE INDEX reviews_by_date ON reviews(review_date);
    CREATE TABLE import_reviews (
      import_id TEXT NOT NULL REFERENCES imports(import_id),
      review_id TEXT NOT NULL REFERENCES reviews(review_id),
      PRIMARY KEY (import_id, review_id)
    );
    CREATE TABLE snapshots (
      snapshot_id TEXT PRIMARY KEY,
      import_id TEXT NOT NULL REFERENCES imports(import_id),
      snapshot_hash TEXT NOT NULL,
      snapshot_json TEXT NOT NULL,
      outcome TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE snapshot_members (
      snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
      review_id TEXT NOT NULL REFERENCES reviews(review_id),
      in_report_week INTEGER NOT NULL CHECK (in_report_week IN (0, 1)),
      PRIMARY KEY (snapshot_id, review_id)
    );
    """,
    """
    CREATE TABLE taxonomies (
      taxonomy_id TEXT PRIMARY KEY,
      snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
      model_ref TEXT NOT NULL,
      prompt_version TEXT NOT NULL,
      taxonomy_json TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE assignments (
      taxonomy_id TEXT NOT NULL REFERENCES taxonomies(taxonomy_id),
      review_id TEXT NOT NULL REFERENCES reviews(review_id),
      theme_id TEXT NOT NULL,
      PRIMARY KEY (taxonomy_id, review_id)
    );
    CREATE TABLE analyses (
      analysis_id TEXT PRIMARY KEY,
      snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
      taxonomy_id TEXT REFERENCES taxonomies(taxonomy_id),
      outcome TEXT NOT NULL,
      result_json TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE runs (
      run_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, config_hash TEXT NOT NULL,
      mode TEXT NOT NULL, status TEXT NOT NULL, reason_code TEXT,
      last_node TEXT NOT NULL, state_json TEXT NOT NULL
    );
    CREATE TABLE node_results (
      run_id TEXT NOT NULL, node TEXT NOT NULL, result_json TEXT NOT NULL,
      PRIMARY KEY(run_id, node)
    );
    CREATE TABLE external_operations (
      operation_key TEXT PRIMARY KEY, publication_key TEXT NOT NULL,
      kind TEXT NOT NULL, payload_hash TEXT NOT NULL, target_ref TEXT,
      status TEXT NOT NULL
    );
    CREATE TABLE publication_receipts (
      publication_key TEXT PRIMARY KEY, run_id TEXT NOT NULL, content_hash TEXT NOT NULL,
      document_id TEXT, document_url TEXT, doc_hash TEXT, draft_id TEXT, draft_hash TEXT,
      layout_status TEXT NOT NULL DEFAULT 'UNCHECKED', verified INTEGER NOT NULL DEFAULT 0
    );
    """,
)


def connect(path: Path) -> sqlite3.Connection:
    try:
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        migrate(conn)
        return conn
    except sqlite3.Error:
        raise PulseError(ErrorCode.STORE_UNAVAILABLE) from None


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.execute("BEGIN")
        try:
            for statement in filter(str.strip, script.split(";")):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {number}")
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
