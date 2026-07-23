from __future__ import annotations

from jobagent.company_filters import matches_blacklisted_company


def test_blacklist_fuzzy_matches_reported_company_typo(temp_loaded) -> None:
    temp_loaded.config.companies.blacklist = ["Siemens"]

    assert matches_blacklisted_company(
        temp_loaded.config,
        "Siemen AG",
        "Senior Buyer",
        "https://jobs.test/senior-buyer",
        "Procurement role",
    )


def test_blacklist_fuzzy_match_does_not_block_distinct_company(temp_loaded) -> None:
    temp_loaded.config.companies.blacklist = ["Siemens"]

    assert not matches_blacklisted_company(
        temp_loaded.config,
        "Simmons AG",
        "Senior Buyer",
        "https://jobs.test/senior-buyer",
        "Procurement role",
    )
