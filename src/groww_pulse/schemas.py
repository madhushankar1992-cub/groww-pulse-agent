from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from groww_pulse.config import Contract, Reference, ReportingConfig
from groww_pulse.errors import ErrorCode

ColumnName = Annotated[str, Field(min_length=1, max_length=100)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class ColumnMapping(Contract):
    text: ColumnName = "text"
    date: ColumnName = "date"
    title: ColumnName | None = None
    rating: ColumnName | None = None
    product_id: ColumnName | None = None
    language: ColumnName | None = None
    identity_columns: tuple[ColumnName, ...] = ()

    @model_validator(mode="after")
    def distinct_columns(self):
        mapped = [getattr(self, k) for k in ("text", "date", "title", "rating", "product_id", "language")]
        mapped = [v for v in mapped if v is not None]
        if len(mapped) != len(set(mapped)) or set(mapped).intersection(self.identity_columns):
            raise ValueError("column mappings must be distinct")
        return self


class ImportManifest(Contract):
    store: Literal["google_play"] = "google_play"
    product_id: Literal["com.nextbillion.groww"] = "com.nextbillion.groww"
    listing_url: Literal["https://play.google.com/store/apps/details?id=com.nextbillion.groww"] = (
        "https://play.google.com/store/apps/details?id=com.nextbillion.groww"
    )
    provider_ref: Reference
    acquisition_method: Literal["manual_public_export", "permitted_public_export"]
    public_export_confirmed: bool = Field(strict=True)
    generated_at: datetime
    coverage_start: date
    coverage_end: date  # Exclusive, in the declared source calendar.
    coverage_status: Literal["complete_for_declared_export", "sampled", "unknown"]
    storefront: Literal["IN"] = "IN"
    source_timezone: str = "Asia/Kolkata"
    format: Literal["csv", "json", "jsonl"]
    encoding: Literal["utf-8", "utf-8-sig"] = "utf-8-sig"
    date_precision: Literal["date", "timestamp"] = "date"
    date_format: Literal["%Y-%m-%d", "%d/%m/%Y", "iso8601"] = "%Y-%m-%d"
    default_language: Literal["en", "hi", "unknown"] = "en"
    language_filter: tuple[Literal["en", "hi", "all"], ...] = ("all",)
    rating_filter: tuple[Annotated[int, Field(ge=1, le=5, strict=True)], ...] = ()
    sort_order: Literal["newest", "oldest", "rating", "unspecified"] = "unspecified"
    truncated: bool = Field(default=False, strict=True)
    export_limit: int | None = Field(default=None, ge=1, strict=True)
    mapping_version: Reference = "v1"
    columns: ColumnMapping = Field(default_factory=ColumnMapping)

    @field_validator("source_timezone")
    @classmethod
    def valid_timezone(cls, value):
        return ReportingConfig.timezone_exists(value)

    @model_validator(mode="after")
    def coherent_manifest(self):
        if not self.public_export_confirmed or self.coverage_start >= self.coverage_end:
            raise ValueError("invalid export declaration")
        if self.generated_at.tzinfo is None:
            raise ValueError("export timestamp must include timezone")
        if (self.date_precision == "timestamp") != (self.date_format == "iso8601"):
            raise ValueError("date precision and format disagree")
        return self


class ReportingWindow(Contract):
    history_start: date
    report_start: date
    report_end: date
    timezone: str


class SafeSpan(Contract):
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)
    source_start: int = Field(ge=0, strict=True)
    source_end: int = Field(gt=0, strict=True)
    source_match: Literal[True] = True

    @model_validator(mode="after")
    def valid_bounds(self):
        if self.end <= self.start or self.source_end - self.source_start != self.end - self.start:
            raise ValueError("invalid safe span")
        return self


class SafeText(Contract):
    evidence_text: str
    analysis_text: str
    spans: tuple[SafeSpan, ...]
    redaction_count: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def in_bounds(self):
        last = 0
        for span in self.spans:
            if span.start < last or span.end > len(self.evidence_text):
                raise ValueError("invalid span sequence")
            last = span.end
        return self


class SanitisedReview(Contract):
    review_id: UUID
    store: Literal["google_play"] = "google_play"
    product_id: Literal["com.nextbillion.groww"] = "com.nextbillion.groww"
    review_date: date
    date_precision: Literal["date", "timestamp"]
    rating: int | None = Field(default=None, ge=1, le=5, strict=True)
    title: SafeText | None = None
    text: SafeText
    language: Literal["en"] = "en"
    sanitiser_version: str
    fingerprint: Digest


class ImportCounts(Contract):
    rows_seen: int = 0
    eligible_rows: int = 0
    unique_rows: int = 0
    duplicates: int = 0  # Identical repeats within this export; stored as separate reviews.
    inserted_rows: int = 0
    reused_rows: int = 0
    weekly_rows: int = 0
    excluded: dict[ErrorCode, int] = Field(default_factory=dict)


class SnapshotCounts(Contract):
    history_reviews: int = Field(ge=0, strict=True)
    weekly_reviews: int = Field(ge=0, strict=True)
    weekly_by_rating: dict[str, int] = Field(default_factory=dict)  # "1".."5", "none"
    import_excluded: dict[ErrorCode, int] = Field(default_factory=dict)


class DatasetSnapshot(Contract):
    snapshot_id: UUID
    import_id: UUID
    snapshot_hash: Digest
    window: ReportingWindow
    counts: SnapshotCounts
    history_complete: bool
    source_status: Literal["complete_for_declared_export", "sampled", "unknown"]
    outcome: Literal["READY", "EMPTY_WEEK", "NO_ELIGIBLE_REVIEWS", "COVERAGE_MISSING"]
    created_at: datetime


ThemeId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")]


class Theme(Contract):
    theme_id: ThemeId
    name: Annotated[str, Field(min_length=2, max_length=60)]
    definition: Annotated[str, Field(min_length=5, max_length=300)]
    is_catch_all: bool = False


class Taxonomy(Contract):
    taxonomy_id: UUID
    snapshot_id: UUID
    model_ref: str
    prompt_version: str
    themes: tuple[Theme, ...] = Field(min_length=3, max_length=5)

    @model_validator(mode="after")
    def coherent(self):
        if len({t.theme_id for t in self.themes}) != len(self.themes):
            raise ValueError("duplicate theme")
        if sum(t.is_catch_all for t in self.themes) > 1:
            raise ValueError("at most one catch-all theme")
        return self


class ThemeRank(Contract):
    theme_id: ThemeId
    name: str
    count: int = Field(ge=0, strict=True)
    share: float = Field(ge=0, le=1)
    highlighted: bool


class QuoteEvidence(Contract):
    quote_id: UUID
    review_id: UUID
    theme_id: ThemeId
    excerpt: str
    rating: int | None = None
    span: SafeSpan  # Position of the excerpt in the stored text and in the original source.
    word_count: int = Field(ge=1, strict=True)


class AnalysisResult(Contract):
    analysis_id: UUID
    snapshot_id: UUID
    taxonomy_id: UUID | None
    outcome: Literal["READY", "INSUFFICIENT_EVIDENCE"]
    reason_code: ErrorCode | None = None
    eligible_weekly: int = Field(ge=0, strict=True)
    ranking: tuple[ThemeRank, ...] = ()
    quotes: tuple[QuoteEvidence, ...] = ()
    usage: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def complete_when_ready(self):
        if self.outcome == "READY" and (
            sum(r.highlighted for r in self.ranking) != 3 or len(self.quotes) != 3
            or len({q.review_id for q in self.quotes}) != 3
        ):
            raise ValueError("a ready analysis needs 3 highlighted themes and 3 distinct quotes")
        return self


class ActionIdea(Contract):
    theme_id: ThemeId
    text: str
    evidence_review_ids: tuple[UUID, ...] = Field(min_length=1)


class Pulse(Contract):
    snapshot_id: UUID
    themes: tuple[ThemeRank, ...] = Field(min_length=3, max_length=3)
    quotes: tuple[QuoteEvidence, ...] = Field(min_length=3, max_length=3)
    actions: tuple[ActionIdea, ...] = Field(min_length=3, max_length=3)
    rendered_body: str
    word_count: int = Field(ge=1, le=250, strict=True)
    content_hash: Digest


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    PREVIEW_READY = "PREVIEW_READY"
    COMPLETE = "COMPLETE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class RunState(Contract):
    schema_version: Literal[1] = 1
    run_id: UUID
    snapshot_id: UUID
    config_hash: Digest
    current_node: Reference
    status: RunStatus = RunStatus.RUNNING
    reason_code: Reference | None = None
    attempts_by_step: dict[Reference, int] = Field(default_factory=dict)
    evidence_ref: UUID | None = None
    pulse_ref: UUID | None = None


class ExternalOperation(Contract):
    operation_key: Digest
    kind: Literal["docs.create", "docs.write", "gmail.create_draft", "gmail.update_draft"]
    payload_hash: Digest
    target_ref: str | None = None
    status: Literal["PENDING", "VERIFIED", "UNKNOWN_OUTCOME", "FAILED"] = "PENDING"
    attempts: int = Field(default=0, ge=0, strict=True)
