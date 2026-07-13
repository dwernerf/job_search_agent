from __future__ import annotations

from jobagent.extract import compact_text, page_decision_from_dict, parse_json_object
from jobagent.models import PageSnapshot


def test_parse_json_object_strips_thinking_and_fences():
    raw = '<think>ignored</think>```json\n{"jobs": [], "link_classifications": [], "source_quality": 50}\n```'
    parsed = parse_json_object(raw)
    assert parsed["source_quality"] == 50


def test_page_decision_parses_score_raw():
    decision = page_decision_from_dict(
        {
            "jobs": [
                {
                    "title": "Procurement Manager",
                    "company": "Acme",
                    "location": "München",
                    "url": "https://acme.test/jobs/1",
                    "fit_score": 150,
                    "reason": "Procurement",
                    "evidence": "Procurement Manager",
                }
            ],
            "link_classifications": [
                {"index": 0, "type": "job_listing", "fit_score": 85, "title": "Test", "company": "Test", "location": "Test", "evidence": "test", "reason": "test"},
                {"index": 1, "type": "explore", "fit_score": 0},
            ],
            "source_quality": 120,
        }
    )
    assert decision.jobs[0].fit_score == 150
    assert decision.source_quality == 120
    assert len(decision.link_classifications) == 2
    assert decision.link_classifications[0].type == "job_listing"
    assert decision.link_classifications[0].url == ""


def test_compact_text_keeps_relevant_lines(loaded_sample):
    cfg = loaded_sample.config
    text = "hello\nProcurement Manager role in Munich\nother"
    compacted = compact_text(text, cfg)
    assert "Procurement Manager role in Munich" in compacted
