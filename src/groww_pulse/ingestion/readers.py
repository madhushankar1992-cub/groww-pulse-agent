import csv
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import ijson

from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.schemas import ImportManifest

Row = dict[str, Any] | None  # None marks a malformed record, counted as ROW_INVALID.


def iter_rows(path: Path, manifest: ImportManifest, max_bytes: int, max_rows: int) -> Iterator[Row]:
    """Stream records without loading the export; never echo content in errors."""
    try:
        if path.stat().st_size > max_bytes:
            raise PulseError(ErrorCode.INPUT_LIMIT_EXCEEDED)
    except OSError:
        raise PulseError(ErrorCode.FILE_UNREADABLE) from None
    reader = {"csv": _csv, "json": _json, "jsonl": _jsonl}[manifest.format]
    try:
        for count, row in enumerate(reader(path, manifest), start=1):
            if count > max_rows:
                raise PulseError(ErrorCode.INPUT_LIMIT_EXCEEDED)
            yield row
    except OSError:
        raise PulseError(ErrorCode.FILE_UNREADABLE) from None
    except (UnicodeError, csv.Error, ijson.JSONError):
        raise PulseError(ErrorCode.FORMAT_INVALID) from None


def _csv(path: Path, manifest: ImportManifest) -> Iterator[Row]:
    csv.field_size_limit(1_000_000)
    with path.open(encoding=manifest.encoding, newline="") as handle:
        reader = csv.DictReader(handle)
        header = set(reader.fieldnames or ())
        if not {manifest.columns.text, manifest.columns.date} <= header:
            raise PulseError(ErrorCode.COLUMNS_INVALID)
        for row in reader:
            # Ragged rows put extras under the None key or leave values as None.
            yield None if None in row or None in row.values() else row


def _json(path: Path, manifest: ImportManifest) -> Iterator[Row]:
    with path.open("rb") as handle:
        for item in ijson.items(handle, "item", use_float=True):
            yield item if isinstance(item, dict) else None


def _jsonl(path: Path, manifest: ImportManifest) -> Iterator[Row]:
    with path.open(encoding=manifest.encoding) as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                yield None
                continue
            yield item if isinstance(item, dict) else None
