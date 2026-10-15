import csv
import json
from pathlib import Path

import pytest
import yaml

from groww_pulse.config import RunConfig
from groww_pulse.repository.db import connect

AS_OF = "2026-10-09"  # Report week 2026-09-28..2026-10-04, history from 2026-07-27.

# Fictional canary identifiers; none may survive into stored data.
CANARIES = ["Rahul Sharma", "rahul.s@example.com", "98765 43210", "@grow_helper",
            "ABCDE1234F", "GRW1234567", "https://example.com/u/42", "CanaryUser77"]

MANIFEST = {
    "store": "google_play", "product_id": "com.nextbillion.groww",
    "provider_ref": "test-fixture", "acquisition_method": "manual_public_export",
    "public_export_confirmed": True, "generated_at": "2026-10-06T09:00:00+05:30",
    "coverage_start": "2026-07-27", "coverage_end": "2026-10-06",
    "coverage_status": "complete_for_declared_export", "format": "csv",
    "date_precision": "timestamp", "date_format": "iso8601", "sort_order": "newest",
    "columns": {"text": "content", "date": "at", "rating": "score",
                "identity_columns": ["userName", "reviewId"]},
}

ROWS = [
    # In report week, with PII to redact.
    {"userName": "CanaryUser77", "reviewId": "gp:AOqpTOF1", "score": "1",
     "at": "2026-09-30T10:00:00+05:30",
     "content": "Withdrawal stuck for 3 days. Thanks Rahul Sharma from support but mail rahul.s@example.com never replied, call 98765 43210"},
    {"userName": "CanaryUser77", "reviewId": "gp:AOqpTOF2", "score": "2",
     "at": "2026-10-01T12:00:00+05:30",
     "content": "KYC verification failed twice, PAN ABCDE1234F rejected and client id GRW1234567 blocked"},
    {"userName": "x", "reviewId": "gp:3", "score": "5", "at": "2026-09-28T00:00:00+05:30",
     "content": "Great app for mutual funds and SIP, very smooth experience"},
    {"userName": "x", "reviewId": "gp:4", "score": "4", "at": "2026-10-04T23:59:00+05:30",
     "content": "Charts are good but the app is really slow during market open every single morning, ping @grow_helper see https://example.com/u/42"},
    # Exact duplicate of the row above (different reviewer): counted once.
    {"userName": "y", "reviewId": "gp:5", "score": "4", "at": "2026-10-04T23:59:00+05:30",
     "content": "Charts are good but the app is really slow during market open every single morning, ping @grow_helper see https://example.com/u/42"},
    # Similar but distinct complaint: kept.
    {"userName": "z", "reviewId": "gp:6", "score": "4", "at": "2026-10-03T09:00:00+05:30",
     "content": "Charts are good but the app is very slow during market open"},
    # History (before report week).
    {"userName": "x", "reviewId": "gp:7", "score": "3", "at": "2026-08-15T09:00:00+05:30",
     "content": "UPI payment for adding money failed but amount was debited"},
    # report_end boundary: Monday 00:00 belongs to the next week.
    {"userName": "x", "reviewId": "gp:8", "score": "3", "at": "2026-10-05T00:00:00+05:30",
     "content": "Order placement is slow sometimes in the morning right after the market opens"},
    # Exclusions.
    {"userName": "x", "reviewId": "gp:9", "score": "9", "at": "2026-09-29T09:00:00+05:30",
     "content": "Rating out of range in this fictional review"},
    {"userName": "x", "reviewId": "gp:10", "score": "5", "at": "not-a-date",
     "content": "Bad date in this fictional review text"},
    {"userName": "x", "reviewId": "gp:11", "score": "5", "at": "2026-05-01T09:00:00+05:30",
     "content": "Too old for the history window and coverage"},
    {"userName": "x", "reviewId": "gp:12", "score": "5", "at": "2026-07-28T09:00:00+05:30",
     "content": "यह ऐप बहुत अच्छा है और निवेश आसान है"},
    {"userName": "x", "reviewId": "gp:13", "score": "5", "at": "2026-09-29T09:00:00+05:30",
     "content": "   "},
    {"userName": "x", "reviewId": "gp:14", "score": "", "at": "2026-09-29T11:00:00+05:30",
     "content": "No star rating on this fictional review but text is fine"},
]


def write_csv(path: Path, rows: list[dict]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["userName", "reviewId", "score", "at", "content"])
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.fixture
def config(tmp_path) -> RunConfig:
    return RunConfig.model_validate({"storage": {"database": str(tmp_path / "pulse.sqlite3")}})


@pytest.fixture
def conn(config):
    connection = connect(config.storage.database)
    yield connection
    connection.close()


@pytest.fixture
def export(tmp_path) -> tuple[Path, Path]:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(yaml.safe_dump(MANIFEST), encoding="utf-8")
    return manifest, write_csv(tmp_path / "export.csv", ROWS)


def manifest_with(tmp_path: Path, **changes) -> Path:
    data = {**MANIFEST, **changes}
    path = tmp_path / "manifest-variant.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def dump_tools(path: Path, tools: dict[str, list[str]]) -> Path:
    path.write_text(json.dumps({"tools": [
        {"name": n, "inputSchema": {"type": "object", "properties": {f: {"type": "string"} for f in fields}}}
        for n, fields in tools.items()
    ]}), encoding="utf-8")
    return path
