from __future__ import annotations

from jobagent.db import Database
from jobagent.discover import bootstrap_queries, read_seed_urls, seed_backlog


def test_bootstrap_queries_use_target_config(temp_loaded, monkeypatch):
    monkeypatch.setattr("jobagent.discover.random.choice", lambda values: values[0])
    monkeypatch.setattr("jobagent.discover.random.random", lambda: 1.0)

    queries = bootstrap_queries(temp_loaded.config)

    assert any("Munich" in query for query in queries)
    assert any("Purchasing Manager" in query or "Procurement Manager" in query for query in queries)


def test_seed_backlog_reads_seed_file(temp_loaded):
    temp_loaded.paths.seeds_path.write_text("https://acme.test/careers\n", encoding="utf-8")
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)

    count = seed_backlog(temp_loaded.config, db, temp_loaded.paths.seeds_path)

    assert count == 1
    assert db.conn.execute(
        "select rating from backlog where url = ?", ("https://acme.test/careers",)
    ).fetchone()["rating"] == 80
    assert db.pop_backlog() == "https://acme.test/careers"
    db.close()


def test_bootstrap_seeding_is_independent_of_runtime_exploration(temp_loaded, monkeypatch):
    monkeypatch.setattr("jobagent.discover.random.choice", lambda values: values[0])
    monkeypatch.setattr("jobagent.discover.random.random", lambda: 1.0)
    temp_loaded.config.seeding.mode = "both"
    temp_loaded.config.exploration.enabled = False
    temp_loaded.paths.seeds_path.write_text("https://acme.test/careers\n", encoding="utf-8")
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)

    count = seed_backlog(temp_loaded.config, db, temp_loaded.paths.seeds_path)
    urls = {row["url"] for row in db.conn.execute("select url from backlog")}

    assert count == len(urls)
    assert "https://acme.test/careers" in urls
    assert any(url.startswith("https://search.brave.com/search?q=") for url in urls)
    assert {
        row["rating"] for row in db.conn.execute("select rating from backlog")
    } == {80}
    db.close()


def test_read_seed_urls_ignores_comments(temp_loaded):
    temp_loaded.paths.seeds_path.write_text(
        "# ignored\n\nhttps://acme.test/jobs\n",
        encoding="utf-8",
    )

    assert read_seed_urls(temp_loaded.paths.seeds_path, temp_loaded.config) == [
        "https://acme.test/jobs"
    ]
