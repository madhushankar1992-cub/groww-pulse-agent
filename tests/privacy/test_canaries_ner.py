from datetime import date

import pytest

from groww_pulse.ingestion.importer import import_export
from groww_pulse.ingestion.manifest import load_manifest
from groww_pulse.privacy.detectors import spacy_names
from tests.conftest import CANARIES

pytestmark = pytest.mark.ner


def test_no_canary_in_database_with_ner(conn, config, export):
    import_export(conn, config, load_manifest(export[0]), export[1],
                  spacy_names(config.privacy.spacy_model), date(2026, 10, 9))
    dump = "\n".join(conn.iterdump())
    assert [c for c in CANARIES if c in dump] == []
    assert "Rahul" not in dump


def test_ner_ignores_brand_words():
    text = "Groww is better than Zerodha for SIP, Nifty charts are fine"
    assert list(spacy_names()(text)) == []
