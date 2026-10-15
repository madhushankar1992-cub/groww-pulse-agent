from typing import TypedDict


class PulseState(TypedDict, total=False):
    run_id: str
    snapshot_id: str
    config_hash: str
    mode: str
    status: str
    reason_code: str | None
    last_completed_node: str
    failed_node: str | None
    taxonomy_id: str
    analysis_id: str
    compose_attempts: int
    classify_attempts: int
    validation_errors: list[str]
    word_count: int
    usage: dict
    active_seconds: float
    repair_cap: int
    classify_cap: int
    eligible_weekly: int
    supported_themes: int
    document_id: str
    document_url: str
    draft_id: str
    layout_status: str


def config_hash(config):
    from groww_pulse.composition.renderer import digest
    return digest(config.model_dump_json())
