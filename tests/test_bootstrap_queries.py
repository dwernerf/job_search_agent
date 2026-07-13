from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest

from jobagent.config import load_config
from jobagent.discover import bootstrap_queries, search_urls_for_query


def test_bootstrap_queries_generate_text():
    """Simple queries use Role+City format."""
    loaded = load_config("config/config.yaml")
    queries = bootstrap_queries(loaded.config)
    assert queries, "bootstrap_queries returned no queries"
    assert all(" " in q for q in queries), "queries should use space-separated Role + City format"
    assert any("Munich" in q for q in queries)
    assert any("Purchasing" in q or "Einkauf" in q for q in queries)


def test_bootstrap_queries_produce_search_urls():
    """Bootstrap queries render into URLs via search_url_templates."""
    loaded = load_config("config/config.yaml")
    queries = bootstrap_queries(loaded.config)
    urls = []
    for q in queries:
        urls.extend(search_urls_for_query(q, loaded.config))
    assert urls, "No URLs produced from bootstrap queries"
    assert any("brave.com" in u for u in urls), "expected brave.com URL"


def test_bootstrap_query_urls_respond():
    """Call bootstrap URLs and verify they don't return 404."""
    loaded = load_config("config/config.yaml")
    queries = bootstrap_queries(loaded.config)
    all_urls: list[str] = []
    for q in queries:
        all_urls.extend(search_urls_for_query(q, loaded.config))

    assert all_urls, "No URLs generated"

    succeeded = 0
    failed = 0
    skipped = 0
    for url in all_urls:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "Mozilla/5.0")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 404:
                    failed += 1
                    print(f"  FAIL: {url} returned 404")
                else:
                    succeeded += 1
        except urllib.error.HTTPError as e:
            if e.code == 404:
                failed += 1
                print(f"  FAIL: {url} returned {e.code}")
            else:
                skipped += 1
                print(f"  SKIP: {url} returned {e.code} (not 404)")
        except Exception as e:
            skipped += 1
            print(f"  SKIP: {url} — {type(e).__name__}: {e}")

    if skipped == len(all_urls):
        pytest.skip(f"All {skipped} URLs unreachable (likely no network)")
    elif failed > 0:
        pytest.fail(f"{failed} of {succeeded + failed} URLs returned 404. {skipped} skipped (unreachable).")


def test_bootstrap_queries_deduplicate():
    """Identical role+city combos should deduplicate."""
    loaded = load_config("config/config.yaml")
    queries = bootstrap_queries(loaded.config)
    assert len(queries) == len(set(queries)), "bootstrap_queries should deduplicate"


def test_bootstrap_queries_are_short():
    """Queries should stay short for URL templating."""
    loaded = load_config("config/config.yaml")
    queries = bootstrap_queries(loaded.config)
    for q in queries:
        assert len(q) <= 120, f"Query too long: {q!r} ({len(q)} chars)"


def test_bootstrap_queries_randomly_injects_whitelist(tmp_path: Path):
    """With a populated whitelist, ~50% of queries should include a company name."""
    import shutil
    import yaml

    shutil.copytree(Path(__file__).resolve().parents[1] / "config", tmp_path / "config")
    config_path = tmp_path / "config" / "config.yaml"
    data = yaml.safe_load(config_path.read_text())
    data["seeding"]["bootstrapped_search"]["company_whitelist"] = ["Zeiss", "Trumpf", "Rohde-Schwarz"]
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    loaded = load_config(config_path)
    whitelist = loaded.config.seeding.bootstrapped_search.company_whitelist

    if not whitelist:
        pytest.skip("No company whitelist configured — nothing to test")

    total_queries = 0
    company_hits = 0
    for _ in range(100):
        queries = bootstrap_queries(loaded.config)
        total_queries += len(queries)
        for q in queries:
            if any(c in q for c in whitelist):
                company_hits += 1

    # With p=0.5 per query, expect ~50% of total queries to contain a company name.
    ratio = company_hits / total_queries if total_queries else 0
    assert 0.35 < ratio < 0.65, (
        f"Expected ~50% company injection across {total_queries} queries, got {company_hits}/{total_queries} ({ratio:.1%}). "
        f"Check the bootstrap_queries() randomization logic."
    )
