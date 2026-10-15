"""Optional, opt-in collection of publicly visible Google Play reviews (no login).

Writes a minimal export (text, rating, timestamp only; reviewer identity is never
written) plus a matching manifest. It does not sanitise or store anything: the
normal `pulse import` path does that. The operator must confirm the source is
permitted for their use before running it.
"""

import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from groww_pulse.config import RunConfig
from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.ingestion.dates import reporting_window

PAGE_SIZE = 200
Page = tuple[list[dict[str, Any]], Any]
PageFetcher = Callable[[Any], Page]


def scraper_pages(app_id: str) -> PageFetcher:
    try:
        from google_play_scraper import Sort, reviews
    except ImportError:
        raise PulseError(ErrorCode.FETCH_FAILED) from None

    def fetch(token: Any) -> Page:
        return reviews(app_id, lang="en", country="in", sort=Sort.NEWEST,
                       count=PAGE_SIZE, continuation_token=token)

    return fetch


def _aware(value: Any, tz: ZoneInfo) -> datetime:
    if not isinstance(value, datetime):
        raise PulseError(ErrorCode.FETCH_FAILED)
    # The library returns naive local timestamps; attach the host zone, then convert.
    return (value.astimezone() if value.tzinfo is None else value).astimezone(tz)


def fetch_public_reviews(config: RunConfig, out_dir: Path, *, confirmed: bool,
                         pages: PageFetcher | None = None, today: date | None = None,
                         max_pages: int = 100) -> tuple[Path, Path, int]:
    if not confirmed:
        raise PulseError(ErrorCode.FETCH_NOT_CONFIRMED)
    tz = ZoneInfo(config.reporting.timezone)
    today = today or datetime.now(tz).date()
    window = reporting_window(config.reporting, today)
    pages = pages or scraper_pages(config.product.google_play_id)

    records: list[dict[str, Any]] = []
    token, oldest, reached_history, exhausted = None, None, False, False
    try:
        for _ in range(max_pages):
            batch, token = pages(token)
            for item in batch:
                at = _aware(item.get("at"), tz)
                oldest = at if oldest is None or at < oldest else oldest
                records.append({"content": item.get("content"), "score": item.get("score"),
                                "at": at.isoformat()})
            if oldest is not None and oldest.date() < window.history_start:
                reached_history = True
                break
            if not batch or token is None:
                exhausted = True
                break
            if len(records) >= config.storage.max_rows:
                break
    except PulseError:
        raise
    except Exception:  # Network/library errors: report a code, never the message.
        raise PulseError(ErrorCode.FETCH_FAILED) from None

    if reached_history or exhausted or oldest is None:
        coverage_start = window.history_start if reached_history else (oldest or datetime.now(tz)).date()
        truncated = False
    else:
        coverage_start = oldest.date() + timedelta(days=1)  # The oldest day may be partial.
        truncated = True

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = today.isoformat()
    export = out_dir / f"google_play_{stamp}.jsonl"
    with export.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    manifest = {
        "store": "google_play",
        "product_id": config.product.google_play_id,
        "provider_ref": "google-play-public",
        "acquisition_method": "permitted_public_export",
        "public_export_confirmed": True,
        "generated_at": datetime.now(tz).isoformat(),
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": today.isoformat(),
        "coverage_status": "complete_for_declared_export",
        "storefront": "IN",
        "source_timezone": config.reporting.timezone,
        "format": "jsonl",
        "encoding": "utf-8",
        "date_precision": "timestamp",
        "date_format": "iso8601",
        "default_language": "en",
        "language_filter": ["en"],
        "sort_order": "newest",
        "truncated": truncated,
        "columns": {"text": "content", "date": "at", "rating": "score"},
    }
    manifest_path = out_dir / f"google_play_{stamp}.manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return export, manifest_path, len(records)
