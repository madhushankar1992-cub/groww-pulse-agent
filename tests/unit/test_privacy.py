import pytest

from groww_pulse.config import PrivacyConfig
from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.privacy.detectors import detect
from groww_pulse.privacy.sanitiser import REDACTION, sanitise

CFG = PrivacyConfig()


def kinds(text):
    return [(text[d.start:d.end], d.kind) for d in detect(text, None)]


@pytest.mark.parametrize("text, hit", [
    ("mail me at someone.x@example.com", "someone.x@example.com"),
    ("pay to rahul99@okaxis please", "rahul99@okaxis"),
    ("call +91 98765-43210 now", "+91 98765-43210"),
    ("aadhaar 1234 5678 9012 linked", "1234 5678 9012"),
    ("see https://example.com/a?b=1", "https://example.com/a?b=1"),
    ("ask @support_team", "@support_team"),
    ("PAN ABCDE1234F rejected", "ABCDE1234F"),
    ("order GRW12345678X failed", "GRW12345678X"),
    ("My name is Ankit Verma and", "Ankit Verma"),
    ("thanks Mr. Kumar for help", "Kumar"),
])
def test_detects_identifiers(text, hit):
    assert hit in [found for found, _ in kinds(text)]


@pytest.mark.parametrize("text", [
    "Lost Rs 1500000 in F&O this month",
    "₹2500000 portfolio and 3 SIPs",
    "Great app for SIP and mutual funds",
    "version 5.2.1 crashes on login",
    "I am very disappointed with Groww",
])
def test_keeps_ordinary_text(text):
    assert kinds(text) == []


def test_sanitise_redacts_and_attests_spans():
    source = "Withdrawal stuck, mailed help@example.com twice and nobody replied to me"
    safe = sanitise(source, CFG, None)
    assert "example.com" not in safe.evidence_text
    assert safe.evidence_text.count(REDACTION) == 1 and safe.redaction_count == 1
    for span in safe.spans:
        assert safe.evidence_text[span.start:span.end] == source[span.source_start:span.source_end]
        assert REDACTION not in safe.evidence_text[span.start:span.end]


def test_analysis_text_is_whitespace_normalised():
    safe = sanitise("Good   app\n\nbut   slow  charts", CFG, None)
    assert safe.analysis_text == "Good app but slow charts"
    assert safe.evidence_text == "Good   app\n\nbut   slow  charts"


@pytest.mark.parametrize("text, code", [
    ("   ", ErrorCode.TEXT_EMPTY),
    ("यह ऐप बहुत अच्छा है", ErrorCode.LANGUAGE_UNSUPPORTED),
    ("a@b.com c@d.com 9876543210 ok", ErrorCode.PRIVACY_UNCERTAIN),
    ("ok call 9876543210", ErrorCode.PRIVACY_UNCERTAIN),
    ("x" * 20001, ErrorCode.INPUT_LIMIT_EXCEEDED),
])
def test_sanitise_exclusions(text, code):
    with pytest.raises(PulseError) as err:
        sanitise(text, CFG, None)
    assert err.value.code is code


def test_names_from_detector_are_redacted():
    safe = sanitise("Support agent Ravi was rude to me today", CFG, lambda t: [(14, 18)])
    assert "Ravi" not in safe.evidence_text


def test_romanised_hinglish_allowed():
    assert sanitise("app bahut slow hai market open pe", CFG, None).redaction_count == 0


def test_short_clean_review_kept():
    assert sanitise("good app", CFG, None).analysis_text == "good app"
