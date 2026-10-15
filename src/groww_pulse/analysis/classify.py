import json
from concurrent.futures import ThreadPoolExecutor

from groww_pulse.analysis import SYSTEM_PREAMBLE
from groww_pulse.analysis.corpus import Review
from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.llm.base import LLM, Effort, strict_schema
from groww_pulse.schemas import Taxonomy

INSTRUCTIONS = """Assign every review below to exactly one primary theme.

Themes (JSON):
{themes}

Return one assignment per review id, using only the theme_id values above. If a review mentions
several topics, choose the one the reviewer is most upset or most specific about.

Reviews (JSON):
{reviews}"""


def schema_for(taxonomy: Taxonomy) -> dict:
    return strict_schema({
        "assignments": {"type": "array", "items": strict_schema({
            "id": {"type": "string"},
            "theme_id": {"type": "string", "enum": [t.theme_id for t in taxonomy.themes]},
        })},
    })


def classify_batch(llm: LLM, taxonomy: Taxonomy, batch: list[Review], attempts: int,
                   effort: Effort) -> dict[str, str]:
    """Return {review_id: theme_id} for the whole batch; retry only the reviews still missing."""
    theme_ids = {t.theme_id for t in taxonomy.themes}
    themes = json.dumps([t.model_dump(include={"theme_id", "name", "definition"})
                         for t in taxonomy.themes], ensure_ascii=False)
    schema = schema_for(taxonomy)
    result: dict[str, str] = {}
    pending = list(batch)
    for _ in range(attempts):
        # Short positional ids keep prompts small and stop the model inventing UUIDs.
        local = {f"r{i}": r.review_id for i, r in enumerate(pending, start=1)}
        reviews = json.dumps([{"id": k, "text": r.text.analysis_text}
                              for k, r in zip(local, pending, strict=True)], ensure_ascii=False)
        try:
            data = llm.complete_json(system=SYSTEM_PREAMBLE,
                                     user=INSTRUCTIONS.format(themes=themes, reviews=reviews),
                                     schema=schema, effort=effort, max_tokens=60 * len(pending) + 500)
        except PulseError as exc:
            if exc.code is not ErrorCode.LLM_OUTPUT_INVALID:
                raise
            continue
        seen: set[str] = set()
        for item in data.get("assignments", []):
            key, theme = (item.get("id"), item.get("theme_id")) if isinstance(item, dict) else (None, None)
            if key in local and key not in seen and theme in theme_ids:
                seen.add(key)
                result[local[key]] = theme
        pending = [r for r in pending if r.review_id not in result]
        if not pending:
            return result
    raise PulseError(ErrorCode.CLASSIFICATION_FAILED)


def classify(llm: LLM, taxonomy: Taxonomy, reviews: list[Review], batch_size: int, attempts: int,
             workers: int, effort: Effort, on_batch=None) -> dict[str, str]:
    batches = [reviews[i:i + batch_size] for i in range(0, len(reviews), batch_size)]
    out: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(classify_batch, llm, taxonomy, b, attempts, effort) for b in batches]
        for future in futures:
            done = future.result()
            out.update(done)
            if on_batch:
                on_batch(done)  # Persist progress on the caller's thread.
    return out
