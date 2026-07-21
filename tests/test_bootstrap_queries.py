from __future__ import annotations

import pytest

from jobagent.discover import bootstrap_queries, search_urls_for_query


@pytest.fixture(autouse=True)
def deterministic_bootstrap_random(monkeypatch):
    monkeypatch.setattr("jobagent.discover.random.choice", lambda values: values[0])
    monkeypatch.setattr("jobagent.discover.random.random", lambda: 1.0)


def test_bootstrap_queries_generate_role_city_text(temp_loaded):
    queries = bootstrap_queries(temp_loaded.config)

    assert queries
    assert all(" " in query for query in queries)
    assert all("Munich" in query for query in queries)
    assert any("Purchasing" in query or "Einkauf" in query for query in queries)


def test_bootstrap_queries_produce_search_urls(temp_loaded):
    urls = [
        url
        for query in bootstrap_queries(temp_loaded.config)
        for url in search_urls_for_query(query, temp_loaded.config)
    ]

    assert urls
    assert all(url.startswith("https://search.brave.com/search?q=") for url in urls)


def test_bootstrap_queries_deduplicate(temp_loaded):
    queries = bootstrap_queries(temp_loaded.config)
    assert len(queries) == len(set(queries))


def test_bootstrap_queries_inject_whitelist_deterministically(temp_loaded, monkeypatch):
    temp_loaded.config.seeding.bootstrapped_search.company_whitelist = ["Zeiss"]
    monkeypatch.setattr("jobagent.discover.random.random", lambda: 0.0)

    queries = bootstrap_queries(temp_loaded.config)

    assert queries
    assert all(query.endswith("Zeiss") for query in queries)


def test_bootstrap_queries_generate_one_query_per_distinct_role(temp_loaded):
    temp_loaded.config.target.roles = ["Role A", "Role B", "Role A"]

    queries = bootstrap_queries(temp_loaded.config)

    assert queries == ["Role A Munich job offerings", "Role B Munich job offerings"]
