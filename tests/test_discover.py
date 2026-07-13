from __future__ import annotations

from jobagent.db import Database
from jobagent.discover import bootstrap_queries, read_seed_urls, seed_backlog


def test_bootstrap_queries_use_target_config(loaded_sample):
    queries = bootstrap_queries(loaded_sample.config)
    assert any("Munich" in q for q in queries)
    assert any("Purchasing Manager" in q or "Procurement Manager" in q for q in queries)


def test_seed_backlog_reads_seed_file(temp_loaded):
    temp_loaded.paths.seeds_path.write_text("https://acme.test/careers\n", encoding="utf-8")
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    count = seed_backlog(temp_loaded.config, db, temp_loaded.paths.seeds_path)
    assert count == 1
    assert db.queued_count() == 1
    db.close()


def test_read_seed_urls_ignores_comments(temp_loaded):
    temp_loaded.paths.seeds_path.write_text("# ignored\n\nhttps://acme.test/jobs\n", encoding="utf-8")
    urls = read_seed_urls(temp_loaded.paths.seeds_path, temp_loaded.config)
    assert urls == ["https://acme.test/jobs"]


def test_seeds_file_has_minimum_entries(loaded_sample):
    """The default seeds.txt must be a well-formed, non-empty list of https URLs."""
    urls = read_seed_urls(loaded_sample.paths.seeds_path, loaded_sample.config)
    assert len(urls) >= 8, "seeds.txt must have at least 8 entries"
    assert all(u.startswith("https://") for u in urls), "all seeds must use https"
    assert len(urls) == len(set(urls)), "seeds must be deduplicated"




