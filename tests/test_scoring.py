from __future__ import annotations

from jobagent.models import JobMatch
from jobagent.scoring import normalize_job_score


def test_llm_sales_representative_is_capped_below_save_threshold(temp_loaded):
    job = JobMatch(
        title="Sales Representative Optics",
        company="Example",
        location="Munich, Germany",
        url="https://example.test/jobs/sales-representative",
        fit_score=78,
        reason="LLM thought optics was relevant",
        evidence="Sales Representative for optical products",
        score_source="llm",
    )
    assert normalize_job_score(job, temp_loaded.config) is None


def test_llm_electronics_engineer_without_procurement_signal_is_capped(temp_loaded):
    job = JobMatch(
        title="Electronics Engineer Laser Systems",
        company="Example",
        location="München",
        url="https://example.test/jobs/electronics-engineer",
        fit_score=82,
        reason="Laser systems company in Munich",
        evidence="Electronics Engineer Laser Systems",
        score_source="llm",
    )
    assert normalize_job_score(job, temp_loaded.config) is None


def test_supplier_quality_engineer_is_protected_relevant_role(temp_loaded):
    job = JobMatch(
        title="Supplier Quality Engineer Optical Components",
        company="Example",
        location="München",
        url="https://example.test/jobs/supplier-quality-engineer-optics",
        fit_score=83,
        reason="Supplier quality for optical components in Munich",
        evidence="Supplier Quality Engineer Optical Components",
        score_source="llm",
    )
    normalized = normalize_job_score(job, temp_loaded.config)
    assert normalized is not None
    assert normalized.fit_score == 83
    assert normalized.score_source == "llm"
    assert "target role signal" in normalized.score_basis


def test_llm_score_without_target_role_signal_is_capped(temp_loaded):
    job = JobMatch(
        title="Project Manager Photonics",
        company="Example",
        location="München",
        url="https://example.test/jobs/project-manager-photonics",
        fit_score=72,
        reason="Photonics industry in Munich",
        evidence="Project Manager Photonics",
        score_source="llm",
    )
    assert normalize_job_score(job, temp_loaded.config) is None


def test_unclear_location_caps_otherwise_relevant_job(temp_loaded):
    job = JobMatch(
        title="Procurement Manager Optical Components",
        company="Example",
        location="Worldwide",
        url="https://example.test/jobs/procurement-manager-optics",
        fit_score=88,
        reason="Procurement for optical components",
        evidence="Procurement Manager Optical Components",
        score_source="llm",
    )
    normalized = normalize_job_score(job, temp_loaded.config)
    assert normalized is None  # capped to 50, below configured min_fit_score_to_save=55
