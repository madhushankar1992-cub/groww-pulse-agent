import csv
import json
from datetime import date

import pytest

from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.ingestion.importer import import_export
from groww_pulse.ingestion.manifest import load_manifest
from groww_pulse.ingestion.snapshot import create_snapshot
from tests.conftest import AS_OF, ROWS, manifest_with, write_csv

TODAY = date.fromisoformat(AS_OF)


def run_import(conn, config, manifest_path, file_path):
    return import_export(conn, config, load_manifest(manifest_path), file_path, None, TODAY)


def test_import_counts_and_exclusions(conn, config, export):
    _, counts = run_import(conn, config, *export)
    assert counts.rows_seen == len(ROWS)
    assert counts.excluded == {
        ErrorCode.DATE_INVALID: 1, ErrorCode.LANGUAGE_UNSUPPORTED: 1,
        ErrorCode.OUTSIDE_COVERAGE: 1, ErrorCode.RATING_INVALID: 1, ErrorCode.TEXT_EMPTY: 1,
    }
    assert counts.eligible_rows == 9
    # The identical repeat (different reviewer) is a separate review, not collapsed.
    assert counts.duplicates == 1 and counts.unique_rows == 9 == counts.inserted_rows
    assert counts.weekly_rows == 7  # Excludes the history row and the report_end boundary row.


def test_reimport_does_not_inflate(conn, config, export):
    run_import(conn, config, *export)
    _, again = run_import(conn, config, *export)
    assert again.inserted_rows == 0 and again.reused_rows == 9
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 9


def test_identity_and_regex_pii_never_stored(conn, config, export):
    run_import(conn, config, *export)
    dump = "\n".join(conn.iterdump())
    for canary in ["CanaryUser77", "gp:AOqpTOF1", "rahul.s@example.com", "98765 43210",
                   "@grow_helper", "ABCDE1234F", "GRW1234567", "https://example.com/u/42"]:
        assert canary not in dump
    assert "export.csv" not in dump


def test_reviews_of_eight_words_or_fewer_dropped(conn, config, tmp_path, export):
    rows = [{**ROWS[2], "content": "Good app, very easy to use for investing"},          # 8 words
            {**ROWS[2], "content": "Good app, very easy to use for investing daily"}]    # 9 words
    _, counts = run_import(conn, config, export[0], write_csv(tmp_path / "w.csv", rows))
    assert counts.eligible_rows == 1 and counts.excluded == {ErrorCode.TEXT_TOO_SHORT: 1}


def test_null_rating_kept(conn, config, export):
    run_import(conn, config, *export)
    assert conn.execute("SELECT COUNT(*) FROM reviews WHERE rating IS NULL").fetchone()[0] == 1


def test_wrong_product_rows_excluded(conn, config, tmp_path):
    rows = [{**ROWS[2], "product": "com.other.app"}, {**ROWS[3], "product": "com.nextbillion.groww"}]
    path = tmp_path / "p.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*ROWS[0].keys(), "product"])
        writer.writeheader()
        writer.writerows(rows)
    manifest = manifest_with(tmp_path, columns={"text": "content", "date": "at", "rating": "score",
                                                "product_id": "product"})
    _, counts = run_import(conn, config, manifest, path)
    assert counts.excluded == {ErrorCode.PRODUCT_MISMATCH: 1} and counts.eligible_rows == 1


def test_wrong_store_manifest_rejected(tmp_path):
    with pytest.raises(PulseError) as err:
        load_manifest(manifest_with(tmp_path, store="app_store"))
    assert err.value.code is ErrorCode.MANIFEST_INVALID


def test_unconfirmed_public_export_rejected(tmp_path):
    with pytest.raises(PulseError):
        load_manifest(manifest_with(tmp_path, public_export_confirmed=False))


def test_missing_columns_rejected(conn, config, tmp_path, export):
    path = tmp_path / "bad.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(PulseError) as err:
        run_import(conn, config, export[0], path)
    assert err.value.code is ErrorCode.COLUMNS_INVALID
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 0  # Rolled back.


def test_empty_file(conn, config, tmp_path, export):
    _, counts = run_import(conn, config, export[0], write_csv(tmp_path / "empty.csv", []))
    assert counts.rows_seen == 0 and counts.eligible_rows == 0


def test_row_limit(conn, config, export):
    small = config.model_copy(update={"storage": config.storage.model_copy(update={"max_rows": 3})})
    with pytest.raises(PulseError) as err:
        run_import(conn, small, *export)
    assert err.value.code is ErrorCode.INPUT_LIMIT_EXCEEDED


@pytest.mark.parametrize("fmt", ["json", "jsonl"])
def test_json_formats(conn, config, tmp_path, fmt):
    rows = [{k: v for k, v in r.items() if k in ("content", "at", "score")} for r in ROWS[:3]]
    rows.append("not an object")
    path = tmp_path / f"e.{fmt}"
    if fmt == "json":
        path.write_text(json.dumps(rows), encoding="utf-8")
    else:
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n{broken\n", encoding="utf-8")
    _, counts = run_import(conn, config, manifest_with(tmp_path, format=fmt, encoding="utf-8"), path)
    assert counts.eligible_rows == 3
    assert counts.excluded[ErrorCode.ROW_INVALID] == (1 if fmt == "json" else 2)


def test_date_only_export(conn, config, tmp_path):
    path = write_csv(tmp_path / "d.csv", [{**ROWS[2], "at": "2026-09-29"}, {**ROWS[3], "at": "29/09/2026"}])
    manifest = manifest_with(tmp_path, date_precision="date", date_format="%Y-%m-%d")
    _, counts = run_import(conn, config, manifest, path)
    assert counts.eligible_rows == 1 and counts.excluded == {ErrorCode.DATE_INVALID: 1}


def test_snapshot_ready(conn, config, export):
    run_import(conn, config, *export)
    snap = create_snapshot(conn, config, as_of=TODAY)
    assert snap.outcome == "READY"
    assert snap.counts.history_reviews == 8 and snap.counts.weekly_reviews == 7
    assert snap.counts.weekly_by_rating == {"1": 1, "2": 1, "4": 3, "5": 1, "none": 1}
    assert snap.history_complete
    members = conn.execute("SELECT SUM(in_report_week), COUNT(*) FROM snapshot_members").fetchone()
    assert tuple(members) == (7, 8)


def test_snapshot_is_deterministic_and_immutable(conn, config, export, tmp_path):
    import_id, _ = run_import(conn, config, *export)
    first = create_snapshot(conn, config, import_id, TODAY)
    run_import(conn, config, export[0], write_csv(tmp_path / "more.csv", [
        {**ROWS[2], "content": "A new fictional review arriving later for the same week"}]))
    second = create_snapshot(conn, config, import_id, TODAY)
    assert first.snapshot_hash == second.snapshot_hash
    assert first.snapshot_id != second.snapshot_id


def test_snapshot_coverage_missing(conn, config, tmp_path):
    manifest = manifest_with(tmp_path, coverage_end="2026-10-02")
    run_import(conn, config, manifest, write_csv(tmp_path / "c.csv", ROWS[:3]))
    assert create_snapshot(conn, config, as_of=TODAY).outcome == "COVERAGE_MISSING"


def test_snapshot_empty_week_differs_from_missing(conn, config, tmp_path, export):
    run_import(conn, config, export[0], write_csv(tmp_path / "h.csv", [ROWS[6]]))
    assert create_snapshot(conn, config, as_of=TODAY).outcome == "EMPTY_WEEK"


def test_snapshot_without_import(conn, config):
    with pytest.raises(PulseError) as err:
        create_snapshot(conn, config, as_of=TODAY)
    assert err.value.code is ErrorCode.IMPORT_NOT_FOUND
