from __future__ import annotations

from jobagent.urltools import clean_url, source_key


def test_clean_url_normalizes_relative_path_and_tracking_params(temp_loaded):
    url = clean_url(
        "../jobs/?utm_source=x&a=1#frag",
        "https://www.Example.com/careers/openings/",
        temp_loaded.config,
    )
    assert url == "https://www.example.com/careers/jobs/?a=1"


def test_clean_url_preserves_trailing_slash_semantics(temp_loaded):
    without_slash = clean_url("https://example.test/job/123", None, temp_loaded.config)
    with_slash = clean_url("https://example.test/job/123/", None, temp_loaded.config)

    assert without_slash == "https://example.test/job/123"
    assert with_slash == "https://example.test/job/123/"


def test_clean_url_decodes_duckduckgo_redirect(temp_loaded):
    url = clean_url(
        "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fcompany.example%2Fcareers%3Futm_campaign%3Dx",
        None,
        temp_loaded.config,
    )
    assert url == "https://company.example/careers"


def test_source_key_uses_normalized_domain_and_first_path_segment(temp_loaded):
    assert (
        source_key("https://www.Example.com/Careers/openings", temp_loaded.config)
        == "example.com/careers"
    )
