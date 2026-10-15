import re
import unicodedata

from groww_pulse.config import PrivacyConfig
from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.privacy.detectors import NameDetector, detect
from groww_pulse.schemas import SafeSpan, SafeText

SANITISER_VERSION = "2"
REDACTION = "[redacted]"
WORD = re.compile(r"\w+(?:['’-]\w+)*")


def script_supported(text: str, max_foreign_ratio: float = 0.3) -> bool:
    """English pipeline only: reject text whose letters are mostly non-Latin (e.g. Devanagari)."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    foreign = sum(1 for c in letters if not unicodedata.name(c, "").startswith("LATIN"))
    return foreign / len(letters) <= max_foreign_ratio


def sanitise(source: str, config: PrivacyConfig, names: NameDetector | None,
             *, required: bool = True) -> SafeText:
    """Redact identifiers and attest which parts of the result are verbatim source text.

    Raises PulseError with a safe code when the text cannot be kept. The caller must
    discard `source` afterwards; removed values are never returned.
    """
    if len(source) > config.max_text_chars:
        raise PulseError(ErrorCode.INPUT_LIMIT_EXCEEDED)
    if not source.strip():
        raise PulseError(ErrorCode.TEXT_EMPTY)
    if not script_supported(source):
        raise PulseError(ErrorCode.LANGUAGE_UNSUPPORTED)

    detections = detect(source, names)
    visible = sum(1 for c in source if not c.isspace())
    redacted = sum(len(source[d.start:d.end].replace(" ", "")) for d in detections)
    if visible and redacted / visible > config.max_redaction_ratio:
        raise PulseError(ErrorCode.PRIVACY_UNCERTAIN)

    parts: list[str] = []
    spans: list[SafeSpan] = []
    cursor = out = 0
    for d in [*detections, None]:
        seg_end = d.start if d else len(source)
        segment = source[cursor:seg_end]
        if segment:
            parts.append(segment)
            spans.append(SafeSpan(start=out, end=out + len(segment),
                                  source_start=cursor, source_end=seg_end))
            out += len(segment)
        if d:
            parts.append(REDACTION)
            out += len(REDACTION)
            cursor = d.end

    evidence = "".join(parts)
    # Short clean reviews ("good app") are kept for counting; quote length is enforced later.
    # Only a redacted review with too little safe text left is treated as ambiguous.
    longest = max((len(WORD.findall(evidence[s.start:s.end])) for s in spans), default=0)
    if required and detections and longest < config.min_safe_span_words:
        raise PulseError(ErrorCode.PRIVACY_UNCERTAIN)
    if not WORD.search(evidence.replace(REDACTION, "")):
        raise PulseError(ErrorCode.TEXT_EMPTY)
    return SafeText(
        evidence_text=evidence,
        analysis_text=" ".join(evidence.split()),
        spans=tuple(spans),
        redaction_count=len(detections),
    )
