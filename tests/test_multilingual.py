from __future__ import annotations

from jobagent.extract import compact_text
from jobagent.language import language_policy_summary


def test_multilingual_config_is_active(temp_loaded):
    cfg = temp_loaded.config
    assert cfg.multilingual.enabled is True
    assert cfg.multilingual.primary_market_language == "German"
    assert cfg.target.languages == ["German", "English"]


def test_compact_text_keeps_german_and_english_signals(temp_loaded):
    text = "hello\nStrategischer Einkäufer in München mit Homeoffice\nSupplier Quality Manager München\nother"
    compacted = compact_text(text, temp_loaded.config)
    assert "Strategischer Einkäufer" in compacted
    assert "Supplier Quality Manager" in compacted


def test_language_policy_is_config_driven(temp_loaded):
    policy = language_policy_summary(temp_loaded.config)
    assert "Primary market language: German" in policy
    assert "Output language for reasons/notes: English" in policy
