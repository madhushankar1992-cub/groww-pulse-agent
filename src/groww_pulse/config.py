from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from groww_pulse.errors import ErrorCode, PulseError

Reference = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ProductConfig(Contract):
    name: Literal["Groww"] = "Groww"
    google_play_id: Literal["com.nextbillion.groww"] = "com.nextbillion.groww"


class ReportingConfig(Contract):
    timezone: str = "Asia/Kolkata"
    history_weeks: int = Field(default=10, ge=8, le=12, strict=True)
    period: Literal["latest_complete_week"] = "latest_complete_week"
    required_stores: tuple[Literal["google_play"], ...] = ("google_play",)

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("invalid timezone") from None
        return value

    @field_validator("required_stores")
    @classmethod
    def only_google_play(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != ("google_play",):
            raise ValueError("exactly Google Play is required")
        return value


class AnalysisConfig(Contract):
    max_themes: int = Field(default=5, ge=3, le=5, strict=True)
    highlighted_themes: Literal[3] = 3
    min_reviews_per_highlighted_theme: int = Field(default=2, ge=1, strict=True)
    min_weekly_reviews: int = Field(default=10, ge=3, strict=True)
    quotes: Literal[3] = 3
    actions: Literal[3] = 3


class OutputConfig(Contract):
    max_words: int = Field(default=250, ge=50, le=250, strict=True)
    max_pages: Literal[1] = 1
    recipient_ref: Reference = "self"
    document_template_ref: Reference = "weekly-pulse"
    email_mode: Literal["draft_only"] = "draft_only"


class ExecutionConfig(Contract):
    model_batch_attempts: int = Field(default=3, ge=1, le=3, strict=True)
    composition_repairs: int = Field(default=2, ge=0, le=2, strict=True)
    safe_mcp_attempts: int = Field(default=3, ge=1, le=3, strict=True)
    active_run_timeout_minutes: int = Field(default=15, ge=1, le=60, strict=True)
    max_concurrent_model_calls: int = Field(default=2, ge=1, le=4, strict=True)


class IngestionConfig(Contract):
    # Keep only reviews with more than 8 words (counted after redaction).
    min_review_words: int = Field(default=9, ge=1, le=50, strict=True)


class PrivacyConfig(Contract):
    supported_languages: tuple[Literal["en"], ...] = ("en",)
    spacy_model: Literal["en_core_web_sm"] = "en_core_web_sm"
    max_text_chars: int = Field(default=20000, ge=100, le=50000, strict=True)
    max_redaction_ratio: float = Field(default=0.4, gt=0, le=0.8, allow_inf_nan=False)
    min_safe_span_words: int = Field(default=3, ge=1, le=20, strict=True)

    @field_validator("supported_languages")
    @classmethod
    def only_validated_english(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != ("en",):
            raise ValueError("only the English privacy pipeline is validated")
        return value


class ModelConfig(Contract):
    # Analysis code depends only on the llm.base.LLM protocol; this selects the adapter.
    provider: Literal["anthropic", "groq"] = "groq"
    model: Annotated[str, Field(min_length=1, max_length=100)] = "openai/gpt-oss-120b"
    classify_effort: Literal["low", "medium", "high"] = "low"
    reasoning_effort: Literal["low", "medium", "high"] = "medium"
    request_timeout_seconds: int = Field(default=120, ge=10, le=600, strict=True)
    discovery_sample_size: int = Field(default=300, ge=30, le=1000, strict=True)
    classify_batch_size: int = Field(default=40, ge=5, le=100, strict=True)
    max_input_tokens_per_run: int = Field(default=300_000, ge=1, strict=True)
    max_output_tokens_per_run: int = Field(default=60_000, ge=1, strict=True)
    # Used only to stop a run that would exceed its budget; update if pricing changes.
    input_usd_per_mtok: float = Field(default=0.15, ge=0, allow_inf_nan=False)
    output_usd_per_mtok: float = Field(default=0.60, ge=0, allow_inf_nan=False)
    max_cost_usd_per_run: float = Field(default=3.0, gt=0, le=100, allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def provider_defaults(cls, value):
        # Preserve explicit legacy configurations; all current defaults use Groq.
        if isinstance(value, dict) and value.get("provider") == "anthropic":
            value = dict(value)
            value.setdefault("model", "claude-opus-5-5")
            value.setdefault("input_usd_per_mtok", 4.0)
            value.setdefault("output_usd_per_mtok", 20.0)
        if isinstance(value, dict) and value.get("provider", "groq") == "groq":
            value = dict(value)
            value.setdefault("model", "openai/gpt-oss-120b")
            prices = {"openai/gpt-oss-120b": (0.15, 0.60), "openai/gpt-oss-20b": (0.075, 0.30)}
            if value["model"] not in prices:
                raise ValueError("Groq model must support the validated strict JSON mode")
            incoming, outgoing = prices[value["model"]]
            value.setdefault("input_usd_per_mtok", incoming)
            value.setdefault("output_usd_per_mtok", outgoing)
        return value


class StorageConfig(Contract):
    database: Path = Path("data/pulse.sqlite3")
    max_input_bytes: int = Field(default=100_000_000, ge=1, le=1_000_000_000, strict=True)
    max_rows: int = Field(default=100000, ge=1, le=1_000_000, strict=True)


class RunConfig(Contract):
    product: ProductConfig = Field(default_factory=ProductConfig)
    reporting: ReportingConfig = Field(default_factory=ReportingConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)


def read_yaml(path: Path, error_code: ErrorCode) -> dict:
    try:
        if path.stat().st_size > 100_000:
            raise PulseError(error_code)
        value = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise PulseError(error_code)
        return value
    except (OSError, UnicodeError, yaml.YAMLError, RecursionError):
        raise PulseError(error_code) from None


def load_config(path: Path | None) -> RunConfig:
    try:
        return RunConfig.model_validate(read_yaml(path, ErrorCode.CONFIG_INVALID)) if path else RunConfig()
    except (ValidationError, ValueError, RecursionError):
        raise PulseError(ErrorCode.CONFIG_INVALID) from None
