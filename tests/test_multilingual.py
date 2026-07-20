from __future__ import annotations

from jobagent.discover import bootstrap_queries
from jobagent.extract import compact_text, page_decision_from_dict
from jobagent.language import bootstrap_template_values, language_policy_summary, multilingual_role_terms
from jobagent.models import PageSnapshot
from jobagent.urltools import denied_by_safety


def test_multilingual_config_is_active(loaded_sample):
    cfg = loaded_sample.config
    assert cfg.multilingual.enabled is True
    assert cfg.multilingual.primary_market_language == "German"
    assert "German" in cfg.multilingual.accepted_languages
    assert "English" in cfg.multilingual.accepted_languages


def test_compact_text_keeps_german_and_english_signals(loaded_sample):
    cfg = loaded_sample.config
    text = "hello\nStrategischer Einkäufer in München mit Homeoffice\nSupplier Quality Manager München\nother"
    compacted = compact_text(text, cfg)
    assert "Strategischer Einkäufer" in compacted
    assert "Supplier Quality Manager" in compacted


def test_bootstrap_queries_include_german_english_and_mixed_terms(loaded_sample):
    cfg = loaded_sample.config
    queries = bootstrap_queries(cfg)
    assert any("procurement" in query.lower() or "purchasing" in query.lower() for query in queries)
    assert any("Munich" in query for query in queries)


def test_german_apply_and_login_text_are_blocked_by_config(loaded_sample):
    cfg = loaded_sample.config
    assert denied_by_safety("https://firma.test/jobs/1", "Jetzt bewerben", cfg)
    assert denied_by_safety("https://firma.test/anmelden", "Anmelden", cfg)


def test_page_decision_parses_posting_language(loaded_sample):
    decision = page_decision_from_dict(
        {
            "jobs": [
                {
                    "title": "Strategischer Einkäufer",
                    "company": "Firma",
                    "location": "München",
                    "posting_language": "German",
                    "url": "https://firma.test/jobs/1",
                    "fit_score": 88,
                    "reason": "Strong fit",
                    "evidence": "Strategischer Einkäufer",
                }
            ],
            "follow_urls": [],
            "source_quality": 90,
        }
    )
    assert decision.jobs[0].posting_language == "German"


def test_language_policy_and_template_values_are_config_driven(loaded_sample):
    cfg = loaded_sample.config
    policy = language_policy_summary(cfg)
    values = bootstrap_template_values(cfg)
    assert "Primary market language: German" in policy
    assert "Strategischer Einkäufer" in values["german_roles"]
    assert "Procurement Manager" in values["english_roles"]
    assert "Munich" in values["location_terms"]


def test_language_policy_does_not_use_positive_or_restrictive_language_terms(loaded_sample):
    policy = language_policy_summary(loaded_sample.config)
    assert "Positive language signals" not in policy
    assert "Restrictive language signals" not in policy
    assert not hasattr(loaded_sample.config.multilingual, "language_positive_terms")
    assert not hasattr(loaded_sample.config.multilingual, "language_restrictive_terms")
