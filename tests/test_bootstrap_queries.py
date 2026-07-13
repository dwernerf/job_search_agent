from __future__ import annotations

from pathlib import Path

from jobagent.config import load_config
from jobagent.discover import bootstrap_queries, search_urls_for_query


def test_bootstrap_queries_generate_text():
    """Verify bootstrap queries contain role + location vocabulary."""
    loaded = load_config(Path("config/config.yaml"))
    queries = bootstrap_queries(loaded.config)
    assert queries, "bootstrap_queries returned no queries"
    assert any("Munich" in q for q in queries)
    assert any("Purchasing Manager" in q for q in queries)
    assert any("Strategic" in q for q in queries)


def test_bootstrap_queries_produce_search_urls():
    """Verify bootstrap queries render into crawlable URLs."""
    loaded = load_config(Path("config/config.yaml"))
    queries = bootstrap_queries(loaded.config)
    urls = []
    for query in queries:
        urls.extend(search_urls_for_query(query, loaded.config))
        print(query)
    assert urls, "No URLs produced from bootstrap queries"


def test_bootstrap_queries_deduplicate():
    """Duplicate query templates should deduplicate."""
    loaded = load_config(Path("config/config.yaml"))
    queries = bootstrap_queries(loaded.config)
    assert len(queries) == len(set(queries)), "bootstrap_queries should deduplicate"


def test_bootstrap_queries_within_reasonable_length():
    """Queries should not exceed a practical length."""
    loaded = load_config(Path("config/config.yaml"))
    queries = bootstrap_queries(loaded.config)
    for q in queries:
        assert len(q) <= 450, f"Query too long: {q!r} ({len(q)} chars)"
