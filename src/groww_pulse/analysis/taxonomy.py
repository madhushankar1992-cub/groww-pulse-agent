import json
from uuid import UUID, uuid4

from pydantic import ValidationError

from groww_pulse.analysis import PROMPT_VERSION, SYSTEM_PREAMBLE
from groww_pulse.analysis.corpus import Review
from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.llm.base import LLM, Effort, strict_schema
from groww_pulse.schemas import Taxonomy, Theme

SCHEMA = strict_schema({
    "themes": {"type": "array", "items": strict_schema({
        "theme_id": {"type": "string", "description": "lowercase snake_case slug"},
        "name": {"type": "string", "description": "2-5 word human label"},
        "definition": {"type": "string", "description": "one sentence: what belongs here"},
        "is_catch_all": {"type": "boolean"},
    })},
})

INSTRUCTIONS = """Propose between {min_themes} and {max_themes} themes that together cover these reviews.
Rules:
- Themes describe what users talk about (e.g. account opening/KYC, payments and adding money,
  withdrawals, order execution and app performance, charges, customer support, ease of use).
  Use what the reviews actually show; these are examples, not a list to copy.
- Themes must not overlap; each review should fit exactly one primary theme.
- Prefer specific, actionable themes. Generic praise ("great app") can form one theme.
- At most one theme may be a catch-all for everything else (is_catch_all: true).

Reviews (JSON):
{reviews}"""


def discover(llm: LLM, sample: list[Review], snapshot_id: UUID, max_themes: int, attempts: int,
             effort: Effort) -> Taxonomy:
    payload = json.dumps([{"text": r.text.analysis_text, "rating": r.rating} for r in sample],
                         ensure_ascii=False)
    user = INSTRUCTIONS.format(min_themes=min(4, max_themes), max_themes=max_themes, reviews=payload)
    for _ in range(attempts):
        data = llm.complete_json(system=SYSTEM_PREAMBLE, user=user, schema=SCHEMA,
                                 effort=effort, max_tokens=4000)
        try:
            themes = tuple(Theme.model_validate(t) for t in data.get("themes", []))
            if not 3 <= len(themes) <= max_themes:
                continue
            return Taxonomy(taxonomy_id=uuid4(), snapshot_id=snapshot_id, model_ref=llm.model_ref,
                            prompt_version=PROMPT_VERSION, themes=themes)
        except (ValidationError, TypeError, AttributeError):
            continue
    raise PulseError(ErrorCode.TAXONOMY_INVALID)
