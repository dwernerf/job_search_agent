from __future__ import annotations

import json

from jobagent.extract import rank_candidate_links
from jobagent.heuristics import heuristic_jobs_from_page, structured_jobs_from_page
from jobagent.models import LinkCandidate, PageSnapshot
from jobagent.structured import extract_jobpostings, structured_jobs_as_text


def test_extract_jobposting_json_ld_and_convert_to_text():
    raw = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": "Procurement Manager - Indirekter Einkauf",
            "hiringOrganization": {"name": "Example GmbH"},
            "jobLocation": {"address": {"addressLocality": "München", "addressCountry": "DE"}},
            "url": "/jobs/procurement-manager",
            "description": "Strategischer Einkauf und Lieferantenmanagement",
        },
        ensure_ascii=False,
    )
    jobs = extract_jobpostings([raw], "https://example.test/careers")
    assert jobs[0]["title"] == "Procurement Manager - Indirekter Einkauf"
    assert jobs[0]["company"] == "Example GmbH"
    assert "München" in jobs[0]["location"]
    assert jobs[0]["url"] == "https://example.test/jobs/procurement-manager"
    assert "STRUCTURED JOBPOSTING DATA" in structured_jobs_as_text(jobs)


def test_heuristic_structured_job_matches_current_profile(temp_loaded):
    temp_loaded.config.heuristic_extraction.enabled = True
    snapshot = PageSnapshot(
        url="https://example.test/careers",
        final_url="https://example.test/careers",
        title="Careers",
        text="",
        links=[],
        structured_jobs=[
            {
                "title": "Supplier Quality Manager Optics",
                "company": "Example GmbH",
                "location": "München, Bayern",
                "url": "https://example.test/jobs/supplier-quality-manager-optics",
                "description": "Supplier quality for optical and laser components",
            }
        ],
    )
    jobs = structured_jobs_from_page(snapshot, temp_loaded.config)
    assert len(jobs) == 1
    assert jobs[0].fit_score >= temp_loaded.config.matching.min_fit_score_to_save


def test_heuristic_link_job_matches_munich_procurement_result_page(temp_loaded):
    temp_loaded.config.heuristic_extraction.enabled = True
    temp_loaded.config.heuristic_extraction.suppress_link_jobs_on_index_pages = False
    snapshot = PageSnapshot(
        url="https://www.stepstone.de/jobs/procurement-manager/in-m%C3%BCnchen",
        final_url="https://www.stepstone.de/jobs/procurement-manager/in-m%C3%BCnchen",
        title="Procurement Manager Jobs in München",
        text="Procurement Manager Jobs in München. Einkauf Beschaffung Supply Chain.",
        links=[
            LinkCandidate(
                text="Procurement Manager - Indirekter Einkauf (all genders)",
                url="https://www.stepstone.de/stellenangebote--procurement-manager-indirekter-einkauf-muenchen-example--123.html",
            )
        ],
    )
    candidate_links = rank_candidate_links(snapshot, temp_loaded.config)
    jobs = heuristic_jobs_from_page(snapshot, candidate_links, temp_loaded.config)
    assert len(jobs) == 1
    assert "Procurement Manager" in jobs[0].title
    assert jobs[0].fit_score >= temp_loaded.config.matching.min_fit_score_to_save


def test_heuristic_suppresses_index_page_link_jobs(temp_loaded):
    temp_loaded.config.heuristic_extraction.enabled = True
    snapshot = PageSnapshot(
        url="https://www.zeiss.com/career/de/stellensuche.html",
        final_url="https://www.zeiss.com/career/de/stellensuche.html?page=1",
        title="Jobsuche bei ZEISS",
        text="Jobsuche Einkauf Beschaffung Supply Chain München",
        links=[
            LinkCandidate(
                text="Procurement Manager",
                url="https://www.zeiss.com/career/de/stellensuche.html?page=1",
            )
        ],
    )
    candidate_links = rank_candidate_links(snapshot, temp_loaded.config)
    jobs = heuristic_jobs_from_page(snapshot, candidate_links, temp_loaded.config)
    assert jobs == []


def test_heuristic_allows_probable_detail_job_link(temp_loaded):
    temp_loaded.config.heuristic_extraction.enabled = True
    snapshot = PageSnapshot(
        url="https://www.stepstone.de/jobs/procurement-manager/in-muenchen",
        final_url="https://www.stepstone.de/jobs/procurement-manager/in-muenchen",
        title="Procurement Manager Jobs in München",
        text="Procurement Manager Jobs in München",
        links=[
            LinkCandidate(
                text="Supplier Quality Manager Optics München",
                url="https://www.stepstone.de/stellenangebote--supplier-quality-manager-optics-muenchen-example--123456.html",
            )
        ],
    )
    # A real result page can still yield detail links if the URL has a detail-page signature.
    temp_loaded.config.heuristic_extraction.suppress_link_jobs_on_index_pages = False
    candidate_links = rank_candidate_links(snapshot, temp_loaded.config)
    jobs = heuristic_jobs_from_page(snapshot, candidate_links, temp_loaded.config)
    assert len(jobs) == 1
    assert "Supplier Quality Manager" in jobs[0].title
