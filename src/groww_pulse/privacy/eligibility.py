"""Local, conservative English-only eligibility. Never rewrites source evidence."""

import re
import unicodedata
from functools import lru_cache

import emoji
from lingua import Language, LanguageDetectorBuilder

from groww_pulse.errors import ErrorCode, PulseError

FILTER_VERSION = "english-emoji-v2"
# Romanised Hindi is not recognised reliably by script-based language detectors.
HINGLISH = re.compile(r"\b(?:hai|hain|bahut|bohot|nahi|nahin|mera|meri|mujhe|paisa|paise|karo|karna|aap|acha|accha)\b", re.I)


@lru_cache(maxsize=1)
def detector():
    return LanguageDetectorBuilder.from_all_languages().with_low_accuracy_mode().build()


def check_eligibility(text: str) -> None:
    if emoji.emoji_list(text):
        raise PulseError(ErrorCode.EMOJI_REVIEW)
    cleaned = text.replace("[redacted]", " ")
    if any(c.isalpha() and not unicodedata.name(c, "").startswith("LATIN") for c in cleaned):
        raise PulseError(ErrorCode.LANGUAGE_UNSUPPORTED)
    if HINGLISH.search(cleaned):
        raise PulseError(ErrorCode.LANGUAGE_UNSUPPORTED)
    scores = detector().compute_language_confidence_values(cleaned)
    if not scores or scores[0].language != Language.ENGLISH or scores[0].value < 0.5:
        raise PulseError(ErrorCode.LANGUAGE_UNSUPPORTED)
