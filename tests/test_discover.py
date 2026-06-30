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


def test_duplicate_generated_query_requeues_previously_blocked_page_when_robots_now_disabled(temp_loaded):
    from jobagent.discover import enqueue_query_suggestions, search_urls_for_query
    from jobagent.models import QuerySuggestion

    cfg = temp_loaded.config
    assert cfg.crawler.respect_robots_txt is False
    assert cfg.crawler.retry_previously_blocked_when_robots_disabled is True

    temp_loaded.paths.seeds_path.write_text("", encoding="utf-8")
    db = Database(temp_loaded.paths.database_path, cfg)
    query = "Procurement Manager Munich careers"
    url = search_urls_for_query(query, cfg)[0]

    db.save_query(query, "already generated in a previous run", "llm")
    item_count = seed_frontier(cfg, db, temp_loaded.paths.seeds_path)
    assert item_count == 0

    db.record_page(
        url=url,
        final_url=url,
        title="",
        source_key="duckduckgo.com/html",
        depth=0,
        status="blocked_by_robots",
        jobs_found=0,
        high_fit_jobs=0,
        source_quality=0,
        discovered_from="previous-run",
    )
    db.enqueue(
        __import__("jobagent.discover", fromlist=["make_frontier_item"]).make_frontier_item(
            url=url,
            depth=0,
            discovered_from="previous-run",
            reason=query,
            config=cfg,
            db=db,
            link_hint=1.0,
        )
    )
    db.mark_frontier(url, "blocked")

    enqueued = enqueue_query_suggestions([QuerySuggestion(query=query, reason="retry duplicate")], cfg, db)
    assert enqueued >= 1
    assert db.queued_count() >= 1
    db.close()



def test_exploration_mode_controls_whitelist_query_seeding(temp_loaded):
    from jobagent.discover import seed_frontier
    from jobagent.db import Database

    temp_loaded.paths.seeds_path.write_text("", encoding="utf-8")
    temp_loaded.config.exploration.mode = "whitelist_only"
    temp_loaded.config.exploration.seed_search_when_empty = True
    temp_loaded.config.companies.whitelist_search_when_seeding = True
    temp_loaded.config.companies.whitelist = ["ZEISS"]
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    added = seed_frontier(temp_loaded.config, db, temp_loaded.paths.seeds_path)
    rows = db.conn.execute("select reason, discovered_from from frontier").fetchall()
    assert added > 0
    assert any(row["discovered_from"] == "company-direct-career" for row in rows)
    assert any(row["discovered_from"] == "company-whitelist-portal-query" for row in rows)
    assert all("ZEISS" in row["reason"] for row in rows)
    db.close()


def test_exploration_mode_can_disable_whitelist_query_seeding(temp_loaded):
    from jobagent.discover import seed_frontier
    from jobagent.db import Database

    temp_loaded.paths.seeds_path.write_text("", encoding="utf-8")
    temp_loaded.config.exploration.mode = "exploratory_only"
    temp_loaded.config.exploration.seed_search_when_empty = True
    temp_loaded.config.companies.whitelist_search_when_seeding = True
    temp_loaded.config.companies.whitelist = ["ZEISS"]
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    added = seed_frontier(temp_loaded.config, db, temp_loaded.paths.seeds_path)
    rows = db.conn.execute("select reason, discovered_from from frontier").fetchall()
    assert added > 0
    assert all(row["discovered_from"] == "bootstrap-query" for row in rows)
    assert not any("ZEISS" in row["reason"] for row in rows)
    db.close()
