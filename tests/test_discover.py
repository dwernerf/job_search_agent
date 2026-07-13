from __future__ import annotations

from jobagent.db import Database
from jobagent.discover import bootstrap_queries, read_seed_urls, seed_frontier


def test_bootstrap_queries_use_target_config(loaded_sample):
    queries = bootstrap_queries(loaded_sample.config)
    assert any("Munich" in q for q in queries)
    assert any("Purchasing Manager" in q or "Procurement Manager" in q for q in queries)


def test_seed_frontier_reads_seed_file(temp_loaded):
    temp_loaded.paths.seeds_path.write_text("https://acme.test/careers\n", encoding="utf-8")
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    count = seed_frontier(temp_loaded.config, db, temp_loaded.paths.seeds_path)
    assert count == 1
    assert db.queued_count() == 1
    db.close()


def test_read_seed_urls_ignores_comments(temp_loaded):
    temp_loaded.paths.seeds_path.write_text("# ignored\n\nhttps://acme.test/jobs\n", encoding="utf-8")
    urls = read_seed_urls(temp_loaded.paths.seeds_path, temp_loaded.config)
    assert urls == ["https://acme.test/jobs"]


def test_default_seed_file_has_active_munich_sources(loaded_sample):
    urls = read_seed_urls(loaded_sample.paths.seeds_path, loaded_sample.config)
    assert len(urls) >= 8
    assert any("stepstone.de/jobs/procurement-manager" in url for url in urls)
    assert any("jobs.personio.de" in url or "join.com/companies" in url for url in urls)




