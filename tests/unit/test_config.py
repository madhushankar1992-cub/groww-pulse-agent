from datetime import date

import pytest
import yaml

from groww_pulse.config import RunConfig, load_config
from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.ingestion.dates import reporting_window


def test_defaults_are_valid():
    config = RunConfig()
    assert config.product.google_play_id == "com.nextbillion.groww"
    assert config.reporting.required_stores == ("google_play",)
    assert config.output.email_mode == "draft_only"


@pytest.mark.parametrize("override", [
    {"reporting": {"required_stores": ["google_play", "app_store"]}},
    {"product": {"google_play_id": "com.zerodha.kite3"}},
    {"reporting": {"history_weeks": 13}},
    {"reporting": {"history_weeks": 7}},
    {"analysis": {"max_themes": 6}},
    {"output": {"max_words": 300}},
    {"output": {"email_mode": "send"}},
    {"reporting": {"timezone": "Mars/Base"}},
    {"unknown_section": {}},
])
def test_rejects_invalid_settings(tmp_path, override):
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(override), encoding="utf-8")
    with pytest.raises(PulseError) as err:
        load_config(path)
    assert err.value.code is ErrorCode.CONFIG_INVALID


def test_no_apple_settings_required(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("reporting:\n  history_weeks: 8\n", encoding="utf-8")
    assert load_config(path).reporting.history_weeks == 8


@pytest.mark.parametrize("as_of, start, end", [
    (date(2026, 10, 9), date(2026, 9, 28), date(2026, 10, 5)),   # Friday
    (date(2026, 10, 5), date(2026, 9, 28), date(2026, 10, 5)),   # Monday: last week is complete
    (date(2026, 10, 4), date(2026, 9, 21), date(2026, 9, 28)),   # Sunday: current week incomplete
])
def test_reporting_window(as_of, start, end):
    window = reporting_window(RunConfig().reporting, as_of)
    assert (window.report_start, window.report_end) == (start, end)
    assert (window.report_end - window.history_start).days == 70
