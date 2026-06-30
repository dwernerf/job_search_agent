from __future__ import annotations

from jobagent.extract import compact_text, page_decision_from_dict, parse_json_object, rank_candidate_links
from jobagent.models import LinkCandidate, PageSnapshot


def test_parse_json_object_strips_thinking_and_fences():
    raw = '<think>ignored</think>```json\n{"jobs": [], "follow_urls": [], "source_quality": 50}\n```'
    parsed = parse_json_object(raw)
    assert parsed["source_quality"] == 50


def test_page_decision_parses_and_clamps_score():
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
            "follow_urls": ["https://acme.test/jobs"],
            "source_quality": 120,
        }
    )
    assert decision.jobs[0].fit_score == 100
    assert decision.source_quality == 100


def test_rank_candidate_links_filters_noise(loaded_sample):
    cfg = loaded_sample.config
    snapshot = PageSnapshot(
        url="https://acme.test",
        final_url="https://acme.test",
        title="Acme",
        text="",
        links=[
            LinkCandidate(text="Careers", url="/careers"),
            LinkCandidate(text="Login", url="/login"),
            LinkCandidate(text="PDF", url="/brochure.pdf"),
            LinkCandidate(text="About", url="/about"),
        ],
    )
    ranked = rank_candidate_links(snapshot, cfg)
    assert [x.url for x in ranked] == ["https://acme.test/careers"]


def test_compact_text_keeps_relevant_lines(loaded_sample):
    cfg = loaded_sample.config
    text = "hello\nProcurement Manager role in Munich\nother"
    compacted = compact_text(text, cfg)
    assert "Procurement Manager role in Munich" in compacted
