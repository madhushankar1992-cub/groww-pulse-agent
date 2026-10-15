import re

from groww_pulse.analysis.corpus import load
from groww_pulse.analysis.quotes import verify
from groww_pulse.composition.renderer import digest, render, word_count
from groww_pulse.privacy.detectors import detect
from groww_pulse.repository import store
from groww_pulse.schemas import Pulse

BANNED = re.compile(r"because of|root cause|caused by|\bwill\b|guarantee|promise|definitely|certainly", re.I)
PLACEHOLDER = re.compile(r"[{}]|\b(?:TODO|TBD|INSERT_HERE)\b", re.I)


def validate(conn, config, analysis, snapshot, composition, names):
    errors = []
    themes = tuple(r for r in analysis.ranking if r.highlighted)
    ids = {r.theme_id for r in themes}
    if ({s.theme_id for s in composition.summaries} != ids
            or {a.theme_id for a in composition.actions} != ids or len(ids) != 3):
        errors.append("CARDINALITY")
    if any(word_count(s.text) > 25 for s in composition.summaries):
        errors.append("SUMMARY_TOO_LONG")
    reviews = {r.review_id: r for r in load(conn, snapshot.snapshot_id) if r.weekly}
    assignments = store.assignments(conn, analysis.taxonomy_id)
    if len({q.review_id for q in analysis.quotes}) != 3:
        errors.append("QUOTE_INVALID")
    for q in analysis.quotes:
        review = reviews.get(str(q.review_id))
        checked = verify(review, q.theme_id, q.span.start, q.span.end) if review else None
        if (not checked or checked.excerpt != q.excerpt or checked.span != q.span
                or assignments.get(str(q.review_id)) != q.theme_id):
            errors.append("QUOTE_INVALID")
    approved = {(str(q.review_id), q.theme_id) for q in analysis.quotes}
    for action in composition.actions:
        if any((str(rid), action.theme_id) not in approved or str(rid) not in reviews
               for rid in action.evidence_review_ids):
            errors.append("EVIDENCE_INVALID")
        if BANNED.search(action.text):
            errors.append("ACTION_UNSUPPORTED")
    body = render(analysis, snapshot, composition)
    count = word_count(body)
    if count > config.output.max_words:
        errors.append("WORD_LIMIT")
    if PLACEHOLDER.search(body):
        errors.append("PLACEHOLDER")
    if detect(body, names):
        errors.append("PRIVACY")
    # Generated prose must not introduce markup, control characters or factual numerals.
    for text in [s.text for s in composition.summaries] + [a.text for a in composition.actions]:
        if re.search(r"[\r\n<>`#]|\d", text):
            errors.append("GENERATED_FORMAT")
    errors = sorted(set(errors))
    report = {"valid": not errors, "errors": errors, "word_count": count}
    pulse = None if errors else Pulse(
        snapshot_id=snapshot.snapshot_id, themes=themes, quotes=analysis.quotes,
        actions=composition.actions, rendered_body=body, word_count=count, content_hash=digest(body))
    return pulse, report
