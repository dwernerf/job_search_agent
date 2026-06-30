from __future__ import annotations

from jobagent.location import evaluate_job_location
from jobagent.models import JobMatch
from jobagent.scoring import normalize_job_score


def _job(title: str, location: str, score: int = 82) -> JobMatch:
    return JobMatch(
        title=title,
        company="Example GmbH",
        location=location,
        url="https://example.test/jobs/procurement-manager",
        fit_score=score,
        reason="Procurement role for supplier quality and optical components",
        evidence=f"{title} {location}",
        score_source="llm",
    )


def test_location_radius_allows_munich_and_nearby_places(temp_loaded):
    for place in ["München", "Garching bei München", "Starnberg", "Ottobrunn"]:
        verdict = evaluate_job_location(_job("Procurement Manager Optical Components", place), temp_loaded.config)
        assert verdict.allowed, place
        assert verdict.distance_km is None or verdict.distance_km <= temp_loaded.config.location_radius.radius_km


def test_location_radius_drops_known_cities_outside_30_km(temp_loaded):
    for place in ["Oberkochen", "Aalen", "Berlin", "Freising"]:
        job = _job("Procurement Manager Optical Components", place, score=90)
        verdict = evaluate_job_location(job, temp_loaded.config)
        assert not verdict.allowed, place
        assert verdict.score_cap == temp_loaded.config.location_radius.outside_radius_cap
        assert normalize_job_score(job, temp_loaded.config) is None


def test_broad_bavaria_only_location_is_not_enough(temp_loaded):
    job = _job("Supplier Quality Manager Optical Components", "Bayern", score=88)
    verdict = evaluate_job_location(job, temp_loaded.config)
    assert not verdict.allowed
    assert verdict.unknown
    assert normalize_job_score(job, temp_loaded.config) is None


def test_germany_remote_procurement_role_is_allowed(temp_loaded):
    job = _job("Procurement Manager Optical Components", "Remote Germany", score=86)
    verdict = evaluate_job_location(job, temp_loaded.config)
    assert verdict.allowed
    assert verdict.remote
    normalized = normalize_job_score(job, temp_loaded.config)
    assert normalized is not None
    assert normalized.fit_score == 86


def test_exploration_url_rejects_outside_city_in_url_even_with_munich_context(temp_loaded):
    from jobagent.location import evaluate_exploration_url_location

    verdict = evaluate_exploration_url_location(
        "https://www.xing.com/jobs/pforzheim-technischer-einkaeufer-155616011",
        "search results for Procurement Manager Munich",
        temp_loaded.config,
    )
    assert not verdict.allowed
    assert verdict.matched_place == "Pforzheim"


def test_exploration_url_rejects_outside_city_in_link_text(temp_loaded):
    from jobagent.location import evaluate_exploration_url_location

    verdict = evaluate_exploration_url_location(
        "https://www.xing.com/jobs/155206253",
        "Strategischer Einkäufer in Buchloe | XING Jobs",
        temp_loaded.config,
    )
    assert not verdict.allowed
    assert verdict.matched_place == "Buchloe"


def test_exploration_search_url_with_munich_location_is_still_allowed(temp_loaded):
    from jobagent.location import evaluate_exploration_url_location

    verdict = evaluate_exploration_url_location(
        "https://www.linkedin.com/jobs/search/?keywords=HENSOLDT+Procurement+Manager&location=Munich%2C+Bavaria%2C+Germany",
        "HENSOLDT Procurement Manager",
        temp_loaded.config,
    )
    assert verdict.allowed
