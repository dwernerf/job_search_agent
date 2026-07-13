from __future__ import annotations

from jobagent.extract import compact_text, page_decision_from_dict, parse_json_object, rank_candidate_links
from jobagent.models import LinkCandidate, PageSnapshot


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


def test_rank_candidate_links_filters_login_and_pdf(loaded_sample):
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
    urls = [x.url for x in ranked]
    assert "https://acme.test/careers" in urls
    assert "https://acme.test/about" in urls
    assert "https://acme.test/login" not in urls
    assert "https://acme.test/brochure.pdf" not in urls


def test_compact_text_keeps_relevant_lines(loaded_sample):
    cfg = loaded_sample.config
    text = "hello\nProcurement Manager role in Munich\nother"
    compacted = compact_text(text, cfg)
    assert "Procurement Manager role in Munich" in compacted
