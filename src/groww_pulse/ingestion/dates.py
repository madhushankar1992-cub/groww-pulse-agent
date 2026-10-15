from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from groww_pulse.config import ReportingConfig
from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.schemas import ImportManifest, ReportingWindow


def reporting_window(config: ReportingConfig, as_of: date | None = None) -> ReportingWindow:
    today = as_of or datetime.now(ZoneInfo(config.timezone)).date()
    end = today - timedelta(days=today.weekday())
    return ReportingWindow(
        history_start=end - timedelta(weeks=config.history_weeks),
        report_start=end - timedelta(days=7), report_end=end, timezone=config.timezone,
    )


def parse_review_date(raw: str, manifest: ImportManifest, timezone: str) -> tuple[date, date]:
    """Return reporting-calendar and source-calendar dates without inventing precision."""
    try:
        if manifest.date_precision == "date":
            # A date-only observation cannot safely be shifted between calendar zones.
            if manifest.source_timezone != timezone:
                raise PulseError(ErrorCode.DATE_INVALID)
            parsed = datetime.strptime(raw, manifest.date_format).date()
            if parsed.strftime(manifest.date_format) != raw:
                raise PulseError(ErrorCode.DATE_INVALID)
            return parsed, parsed
        parsed_timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed_timestamp.tzinfo is None:
            raise PulseError(ErrorCode.DATE_INVALID)
        return (
            parsed_timestamp.astimezone(ZoneInfo(timezone)).date(),
            parsed_timestamp.astimezone(ZoneInfo(manifest.source_timezone)).date(),
        )
    except (ValueError, TypeError, OverflowError):
        raise PulseError(ErrorCode.DATE_INVALID) from None
