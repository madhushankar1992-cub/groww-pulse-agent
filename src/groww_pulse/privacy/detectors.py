import re
from collections.abc import Callable, Iterable
from functools import lru_cache
from typing import NamedTuple

from groww_pulse.errors import ErrorCode, PulseError


class Detection(NamedTuple):
    start: int
    end: int
    kind: str


# Order matters only for readability; overlapping spans are merged afterwards.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Emails and UPI IDs (name@okaxis) share a shape.
    ("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*")),
    ("url", re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+\.[a-z]{2,6}/\S+", re.IGNORECASE)),
    ("handle", re.compile(r"(?<![\w@])@\w{2,}")),
    ("pan", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    # Phones, account, client, order and Aadhaar-like numbers: 7+ digits, single separators allowed.
    ("number", re.compile(r"(?<![\w₹])(?<!rs )(?<!rs\. )(?<!inr )\+?\d(?:[ -]?\d){6,}(?!\w)", re.IGNORECASE)),
    # Mixed letter/digit tokens of 8+ chars: order IDs, IFSC codes, client codes.
    ("token", re.compile(r"\b(?=[A-Za-z]*\d)(?=\d*[A-Za-z])[A-Za-z0-9]{8,}\b")),
    ("self_name", re.compile(
        r"\b(?i:my name is)\s+((?i:[a-z]+)(?:\s+[A-Z][a-z]+)?)\b")),
    ("salutation_name", re.compile(
        r"\b(?i:mr|mrs|ms|miss|sir|madam|dear)\.?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)")),
)

# Words spaCy often tags as PERSON in app reviews that are not people.
NOT_PEOPLE = frozenset(
    w.lower()
    for w in """Groww Grow Zerodha Kite Upstox Angel Paytm PhonePe GPay Google Play Android iPhone
    UPI KYC SIP NSE BSE IPO NFO ETF MF FD SEBI RBI NPCI Nifty Sensex Demat Aadhaar PAN OTP ITR
    App Bhai Sir Madam Ji Bro Plz Pls Lol Ok Okay Thanks Excellent Pathetic Worst Best Good Nice""".split()
)

NameDetector = Callable[[str], Iterable[tuple[int, int]]]


@lru_cache(maxsize=1)
def _spacy_model(name: str):
    try:
        import spacy

        return spacy.load(name, disable=["parser", "lemmatizer", "tagger", "attribute_ruler"])
    except (ImportError, OSError):
        raise PulseError(ErrorCode.PRIVACY_MODEL_UNAVAILABLE) from None


def spacy_names(model_name: str = "en_core_web_sm") -> NameDetector:
    nlp = _spacy_model(model_name)

    def detect(text: str) -> Iterable[tuple[int, int]]:
        for ent in nlp(text).ents:
            if ent.label_ != "PERSON":
                continue
            words = [w for w in re.findall(r"\w+", ent.text) if w.lower() not in NOT_PEOPLE]
            if words and any(len(w) > 1 for w in words):
                yield ent.start_char, ent.end_char

    return detect


def detect(text: str, names: NameDetector | None) -> list[Detection]:
    found: list[Detection] = []
    for kind, pattern in PATTERNS:
        for m in pattern.finditer(text):
            start, end = (m.start(1), m.end(1)) if pattern.groups else (m.start(), m.end())
            if kind.endswith("name") and m.group(1).split()[0].lower() in NOT_PEOPLE:
                continue
            found.append(Detection(start, end, kind))
    if names is not None:
        found.extend(Detection(s, e, "person") for s, e in names(text))
    return merge(found)


def merge(spans: list[Detection]) -> list[Detection]:
    merged: list[Detection] = []
    for span in sorted(spans):
        if merged and span.start <= merged[-1].end:
            last = merged[-1]
            merged[-1] = Detection(last.start, max(last.end, span.end), last.kind)
        else:
            merged.append(span)
    return merged
