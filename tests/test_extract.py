from __future__ import annotations

from jobagent.extract import compact_text, page_decision_from_dict, parse_json_object


def test_parse_json_object_strips_thinking_and_fences():
    raw = '<think>ignored</think>```json\n{"link_classifications": [], "source_quality": 50}\n```'
    parsed = parse_json_object(raw)
    assert parsed["source_quality"] == 50


def test_page_decision_parses_link_classifications_and_source_metadata():
    decision = page_decision_from_dict(
        {
            "link_classifications": [
                {
                    "index": 0,
                    "type": "job_listing",
                    "fit_score": 85,
                    "title": "Buyer",
                    "company": "Example",
                    "location": "Munich",
                    "evidence": "Strategic sourcing",
                    "reason": "Strong fit",
                },
                {"index": 1, "type": "explore", "fit_score": 0},
            ],
            "source_quality": 90,
            "source_notes": "Useful career source",
        }
    )

    assert decision.source_quality == 90
    assert decision.source_notes == "Useful career source"
    assert len(decision.link_classifications) == 2
    assert decision.link_classifications[0].title == "Buyer"
    assert decision.link_classifications[0].url == ""
    assert decision.link_classifications[1].type == "explore"


def test_page_decision_rejects_malformed_classifications_and_clamps_quality():
    decision = page_decision_from_dict(
        {
            "link_classifications": [
                {"type": "job_listing", "fit_score": 80},
                {"index": "bad", "type": "job_listing", "fit_score": 80},
                {"index": 1.9, "type": "job_listing", "fit_score": 80},
                {"index": 1, "type": "invented", "fit_score": 80},
                {"index": 2, "type": "job_listing", "fit_score": 101},
                {"index": 3, "type": "explore", "fit_score": 99},
            ],
            "source_quality": 150,
        }
    )

    assert [(item.index, item.type, item.fit_score) for item in decision.link_classifications] == [
        (3, "explore", 0)
    ]
    assert decision.source_quality == 100


def test_compact_text_keeps_relevant_lines(temp_loaded):
    noise = "unrelated navigation " * 10
    text = "\n".join([noise] * 181 + ["Procurement Manager role in Munich"])
    compacted = compact_text(text, temp_loaded.config)
    assert "Procurement Manager role in Munich" in compacted
