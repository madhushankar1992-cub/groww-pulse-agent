"""Quote evidence: the model nominates excerpts, code proves they are verbatim and safe.

A quote is accepted only if it is found in the stored review text, lies wholly inside
one safe span (text attested identical to the original review, with no redaction),
and has a readable length. Nothing the model writes is ever published unverified.
"""

import json
import re
from uuid import uuid4

from groww_pulse.analysis import SYSTEM_PREAMBLE
from groww_pulse.analysis.corpus import Review, stable_key
from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.llm.base import LLM, Effort, strict_schema
from groww_pulse.privacy.sanitiser import REDACTION, WORD
from groww_pulse.schemas import QuoteEvidence, SafeSpan, ThemeRank

MIN_WORDS, MAX_WORDS = 6, 40
MAX_CANDIDATE_REVIEWS = 30

SCHEMA = strict_schema({
    "candidates": {"type": "array", "items": strict_schema({
        "id": {"type": "string"}, "excerpt": {"type": "string"},
    })},
})

INSTRUCTIONS = """Theme: {name} - {definition}

Pick up to 5 excerpts that best show what users say about this theme, best first.
Each excerpt must be copied EXACTLY from one review: same words, spelling, punctuation and
casing. Do not fix typos, translate, merge reviews or add words. Use one or two full sentences,
{min_words}-{max_words} words, and never include [redacted].

Reviews (JSON):
{reviews}"""


def locate(text: str, excerpt: str) -> tuple[int, int] | None:
    """Find the excerpt in the stored text, tolerating only whitespace differences."""
    excerpt = excerpt.strip().strip('"“”').strip()
    if not excerpt:
        return None
    start = text.find(excerpt)
    if start >= 0:
        return start, start + len(excerpt)
    pattern = r"\s+".join(re.escape(part) for part in excerpt.split())
    match = re.search(pattern, text)
    return (match.start(), match.end()) if match else None


def verify(review: Review, theme_id: str, start: int, end: int) -> QuoteEvidence | None:
    text = review.text.evidence_text
    excerpt = text[start:end]
    words = len(WORD.findall(excerpt))
    if REDACTION in excerpt or not MIN_WORDS <= words <= MAX_WORDS:
        return None
    for span in review.text.spans:
        if span.start <= start and end <= span.end:
            offset = span.source_start - span.start
            return QuoteEvidence(
                quote_id=uuid4(), review_id=review.review_id, theme_id=theme_id, excerpt=excerpt,
                rating=review.rating, word_count=words,
                span=SafeSpan(start=start, end=end, source_start=start + offset,
                              source_end=end + offset),
            )
    return None


def fallback(review: Review, theme_id: str) -> QuoteEvidence | None:
    """Deterministic backup: the first verifiable sentence (or sentence pair) of the review."""
    text = review.text.evidence_text
    bounds = [m.span() for m in re.finditer(r"[^.!?\n]+[.!?]*", text)]
    for i, (start, end) in enumerate(bounds):
        for stop in (end, bounds[i + 1][1] if i + 1 < len(bounds) else None):
            if stop is None:
                continue
            s = start + len(text[start:stop]) - len(text[start:stop].lstrip())
            e = stop - (len(text[s:stop]) - len(text[s:stop].rstrip()))
            quote = verify(review, theme_id, s, e)
            if quote:
                return quote
    return None


def candidates_for(theme: ThemeRank, weekly: list[Review], assignment: dict[str, str],
                   used: set[str], seed: str) -> list[Review]:
    pool = [r for r in weekly if assignment.get(r.review_id) == theme.theme_id and r.review_id not in used]
    pool.sort(key=lambda r: stable_key(seed, r.review_id))
    return pool[:MAX_CANDIDATE_REVIEWS]


def select_quote(llm: LLM, theme: ThemeRank, definition: str, pool: list[Review], attempts: int,
                 effort: Effort) -> QuoteEvidence | None:
    local = {f"r{i}": r for i, r in enumerate(pool, start=1)}
    user = INSTRUCTIONS.format(
        name=theme.name, definition=definition, min_words=MIN_WORDS, max_words=MAX_WORDS,
        reviews=json.dumps([{"id": k, "rating": r.rating, "text": r.text.evidence_text}
                            for k, r in local.items()], ensure_ascii=False))
    for _ in range(attempts):
        try:
            data = llm.complete_json(system=SYSTEM_PREAMBLE, user=user, schema=SCHEMA,
                                     effort=effort, max_tokens=3000)
        except PulseError as exc:
            if exc.code is not ErrorCode.LLM_OUTPUT_INVALID:
                raise
            continue
        for item in data.get("candidates", [])[:5]:
            review = local.get(item.get("id")) if isinstance(item, dict) else None
            found = locate(review.text.evidence_text, str(item.get("excerpt", ""))) if review else None
            quote = verify(review, theme.theme_id, *found) if found else None
            if quote:
                return quote
    for review in pool:  # The model's picks all failed verification.
        quote = fallback(review, theme.theme_id)
        if quote:
            return quote
    return None
