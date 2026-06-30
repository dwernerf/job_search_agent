from __future__ import annotations

from jobagent.urltools import career_candidate_urls, clean_url, denied_by_safety, source_key


def test_clean_url_removes_tracking_params(loaded_sample):
    cfg = loaded_sample.config
    url = clean_url("https://example.com/jobs/?utm_source=x&a=1#frag", None, cfg)
    assert url == "https://example.com/jobs?a=1"


def test_clean_url_decodes_duckduckgo_redirect(loaded_sample):
    cfg = loaded_sample.config
    url = clean_url(
        "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fcompany.example%2Fcareers%3Futm_campaign%3Dx",
        None,
        cfg,
    )
    assert url == "https://company.example/careers"


def test_safety_denies_login(loaded_sample):
    cfg = loaded_sample.config
    assert denied_by_safety("https://example.com/login", "Login", cfg)


def test_source_key_domain_path1(loaded_sample):
    cfg = loaded_sample.config
    assert source_key("https://www.example.com/careers/openings", cfg) == "example.com/careers"


def test_career_candidate_urls_use_configured_paths(loaded_sample):
    cfg = loaded_sample.config
    urls = career_candidate_urls("https://example.com/about", cfg)
    assert "https://example.com/careers" in urls
    assert "https://example.com/jobs" in urls
