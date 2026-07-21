from __future__ import annotations

import pytest

from jobagent.models import LinkCandidate
from jobagent.urltools import filter_links, filter_url, source_key


def test_filter_url_normalizes_relative_path_and_tracking_params(temp_loaded):
    url = filter_url(
        "../jobs/?utm_source=x&a=1#frag",
        "https://www.Example.com/careers/openings/",
        temp_loaded.config,
    )

    assert url == "https://www.example.com/careers/jobs/?a=1"


def test_filter_url_preserves_trailing_slash_semantics(temp_loaded):
    without_slash = filter_url("https://example.test/job/123", None, temp_loaded.config)
    with_slash = filter_url("https://example.test/job/123/", None, temp_loaded.config)

    assert without_slash == "https://example.test/job/123"
    assert with_slash == "https://example.test/job/123/"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https:///missing-host",
        "https://example.test:invalid/jobs",
        "https://example.test/jobs\nInjected",
        "mailto:jobs@example.test",
        "javascript:void(0)",
    ],
)
def test_filter_url_rejects_empty_malformed_and_unsupported_urls(temp_loaded, url):
    assert filter_url(url, None, temp_loaded.config) is None


def test_filter_url_rejects_excluded_domains_and_files(temp_loaded):
    assert filter_url("https://www.facebook.com/jobs/1", None, temp_loaded.config) is None
    assert filter_url("https://example.test/job-description%2Epdf", None, temp_loaded.config) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://login.example.test/session",
        "https://example.test/account/settings",
        "https://example.test/checkpoint/verify",
        "https://example.test/jobs/initiativbewerbung",
        "https://example.test/jobs/talent-pool",
        "https://example.test/jobs?type=general-application",
        "https://example.test/%6cogin",
        "https://example.test/jobs/talent%20pool",
    ],
)
def test_filter_url_rejects_denied_url_patterns(temp_loaded, url):
    assert filter_url(url, None, temp_loaded.config) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/jobs/account-manager",
        "https://example.test/apply/submit",
        "https://example.test/application/submit",
    ],
)
def test_filter_url_does_not_block_account_titles_or_submission_endpoints(temp_loaded, url):
    assert filter_url(url, None, temp_loaded.config) == url


def test_filter_links_removes_self_links_and_canonical_duplicates(temp_loaded):
    source = "https://example.test/careers?utm_source=seed"
    links = [
        LinkCandidate(text="This page", url="https://example.test/careers#top"),
        LinkCandidate(text="Buyer", url="/jobs/buyer?utm_campaign=x"),
        LinkCandidate(text="Buyer duplicate", url="https://example.test/jobs/buyer"),
    ]

    assert filter_links(links, source, temp_loaded.config) == [
        LinkCandidate(text="Buyer", url="https://example.test/jobs/buyer")
    ]


def test_filter_links_does_not_inspect_link_text_or_limit_candidates(temp_loaded):
    links = [
        LinkCandidate(text="Submit application", url="https://example.test/apply/submit"),
        LinkCandidate(text="Log in", url="https://example.test/jobs/login-specialist"),
        *[
            LinkCandidate(text=f"Job {index}", url=f"https://example.test/jobs/{index}")
            for index in range(60)
        ],
    ]

    filtered = filter_links(links, "https://example.test/careers", temp_loaded.config)

    assert len(filtered) == 62
    assert filtered[0].url == "https://example.test/apply/submit"
    assert filtered[1].url == "https://example.test/jobs/login-specialist"


def test_source_key_uses_normalized_domain_and_first_path_segment(temp_loaded):
    assert (
        source_key("https://www.Example.com/Careers/openings")
        == "example.com/careers"
    )
