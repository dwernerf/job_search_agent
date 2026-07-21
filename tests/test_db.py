from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from jobagent.db import SCHEMA_VERSION, Database
from jobagent.models import JobMatch


EXPECTED_COLUMNS = {
    "jobs": [
        "url",
        "title",
        "company",
        "location",
        "fit_score",
        "reason",
        "evidence",
        "source_key",
        "first_seen_at",
        "last_seen_at",
        "original_url",
    ],
    "pages": ["url", "final_url", "status"],
    "backlog": ["url", "status", "queued_at", "rating", "queue_position"],
}


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "select name from sqlite_master where type = 'table' and name not like 'sqlite_%'"
        )
    }


def job(url: str = "https://jobs.test/1", *, title: str = "Buyer") -> JobMatch:
    return JobMatch(
        title=title,
        company="Example",
        location="Munich",
        url=url,
        original_url=url,
        fit_score=80,
        reason="Fit",
        evidence="Buying",
    )


def test_fresh_database_creates_strict_v3_schema(tmp_path: Path, temp_loaded) -> None:
    db = Database(tmp_path / "fresh.sqlite", temp_loaded.config)

    assert db.conn.execute("pragma user_version").fetchone()[0] == 3
    assert SCHEMA_VERSION == 3
    assert table_names(db.conn) == set(EXPECTED_COLUMNS)
    for table, expected_names in EXPECTED_COLUMNS.items():
        info = db.conn.execute(f"pragma table_info({table})").fetchall()
        assert [row["name"] for row in info] == expected_names
        assert info[0]["name"] == "url"
        assert info[0]["pk"] == 1
        assert info[0]["notnull"] == 1
        assert all(row["notnull"] == 1 for row in info)
    db.close()


def test_unversioned_legacy_database_is_rejected(tmp_path: Path, temp_loaded) -> None:
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("create table jobs (url text primary key)")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="delete the database"):
        Database(path, temp_loaded.config)

    conn = sqlite3.connect(path)
    assert conn.execute("pragma user_version").fetchone()[0] == 0
    assert table_names(conn) == {"jobs"}
    conn.close()


def test_future_schema_is_rejected(tmp_path: Path, temp_loaded) -> None:
    path = tmp_path / "future.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(f"pragma user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="newer than supported"):
        Database(path, temp_loaded.config)


def test_valid_v2_database_is_migrated_with_rated_fifo_backlog(
    tmp_path: Path, temp_loaded
) -> None:
    path = tmp_path / "v2.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table jobs (
            url text not null primary key, title text not null, company text not null,
            location text not null, fit_score integer not null, reason text not null,
            evidence text not null, source_key text not null,
            first_seen_at text not null, last_seen_at text not null,
            original_url text not null
        );
        create table pages (
            url text not null primary key, final_url text not null, status text not null
        );
        create table backlog (
            url text not null primary key, status text not null, queued_at text not null
        );
        pragma user_version = 2;
        """
    )
    conn.execute(
        "insert into pages(url, final_url, status) values (?, ?, 'ok')",
        ("https://pages.test/old", "https://pages.test/final"),
    )
    conn.execute(
        """
        insert into jobs(
            url, title, company, location, fit_score, reason, evidence,
            source_key, first_seen_at, last_seen_at, original_url
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "https://jobs.test/old",
            "Buyer",
            "Example",
            "Munich",
            80,
            "Fit",
            "Buying",
            "source",
            "first",
            "last",
            "https://jobs.test/old",
        ),
    )
    conn.execute(
        "insert into backlog(url, status, queued_at) values (?, 'queued', ?)",
        ("https://queue.test/later", "2026-01-01T00:00:02+00:00"),
    )
    conn.execute(
        "insert into backlog(url, status, queued_at) values (?, 'queued', ?)",
        ("https://queue.test/earlier", "2026-01-01T00:00:01+00:00"),
    )
    conn.commit()
    conn.close()

    db = Database(path, temp_loaded.config)

    assert db.conn.execute("pragma user_version").fetchone()[0] == 3
    assert db.count_rows("jobs") == 1
    assert db.page_status("https://pages.test/final") == "ok"
    rows = db.conn.execute(
        "select url, rating, queue_position from backlog order by queue_position"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("https://queue.test/earlier", 80, 1),
        ("https://queue.test/later", 80, 2),
    ]
    db.close()


def test_malformed_v2_constraints_are_rejected(tmp_path: Path, temp_loaded) -> None:
    path = tmp_path / "malformed.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        create table jobs (
            url text primary key, title text not null, company text not null,
            location text not null, fit_score integer not null, reason text not null,
            evidence text not null, source_key text not null,
            first_seen_at text not null, last_seen_at text not null,
            original_url text not null
        )
        """
    )
    conn.execute(
        "create table pages (url text primary key, final_url text not null, status text not null)"
    )
    conn.execute(
        "create table backlog (url text primary key, status text not null, queued_at text not null)"
    )
    conn.execute("pragma user_version = 2")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="invalid v2 primary key"):
        Database(path, temp_loaded.config)


def test_backlog_rejects_duplicates_but_does_not_consult_pages(
    tmp_path: Path, temp_loaded
) -> None:
    temp_loaded.config.crawler.retry_error_pages = True
    db = Database(tmp_path / "backlog.sqlite", temp_loaded.config)

    assert db.enqueue("https://queue.test/new", rating=80) is True
    assert db.enqueue("https://queue.test/new", rating=80) is False
    assert db.queued_count() == 1
    assert db.pop_backlog() == "https://queue.test/new"
    assert db.pop_backlog() is None
    db.complete_backlog("https://queue.test/new", "https://queue.test/final")
    assert db.conn.execute(
        "select 1 from backlog where url = ?", ("https://queue.test/new",)
    ).fetchone() is None
    assert db.page_status("https://queue.test/final") == "ok"

    db.record_page("https://pages.test/error", "https://pages.test/error", "error:Timeout")
    assert db.enqueue("https://pages.test/error", rating=50) is True

    db.record_page("https://pages.test/ok", "https://pages.test/final", "ok")
    assert db.enqueue("https://pages.test/final", rating=60) is True
    assert db.reset_backlog() == 2
    db.close()


def test_rating_order_uses_fifo_for_equal_ratings(tmp_path: Path, temp_loaded) -> None:
    temp_loaded.config.run.backlog_order = "rating"
    db = Database(tmp_path / "rating.sqlite", temp_loaded.config)

    assert db.enqueue("https://queue.test/low", rating=20) is True
    assert db.enqueue("https://queue.test/high-first", rating=95) is True
    assert db.enqueue("https://queue.test/high-second", rating=95) is True
    assert db.enqueue("https://queue.test/seed", rating=80) is True

    assert db.pop_backlog() == "https://queue.test/high-first"
    assert db.pop_backlog() == "https://queue.test/high-second"
    assert db.pop_backlog() == "https://queue.test/seed"
    assert db.pop_backlog() == "https://queue.test/low"
    db.close()


def test_duplicate_backlog_url_keeps_maximum_rating_and_fifo_position(
    tmp_path: Path, temp_loaded
) -> None:
    db = Database(tmp_path / "duplicate-rating.sqlite", temp_loaded.config)
    url = "https://queue.test/careers"

    assert db.enqueue(url, rating=80) is True
    original = db.conn.execute(
        "select queued_at, queue_position from backlog where url = ?", (url,)
    ).fetchone()
    assert db.enqueue(url, rating=60) is False
    assert db.enqueue(url, rating=90) is True

    updated = db.conn.execute(
        "select queued_at, rating, queue_position from backlog where url = ?", (url,)
    ).fetchone()
    assert tuple(updated) == (original["queued_at"], 90, original["queue_position"])
    db.close()


@pytest.mark.parametrize("rating", [-1, 101, True, 1.5])
def test_backlog_rating_must_be_an_integer_from_zero_to_one_hundred(
    tmp_path: Path, temp_loaded, rating
) -> None:
    db = Database(tmp_path / "invalid-rating.sqlite", temp_loaded.config)

    with pytest.raises(ValueError, match="integer from 0 to 100"):
        db.enqueue("https://queue.test/invalid", rating=rating)

    db.close()


def test_candidate_claim_retries_only_transient_http_errors(
    tmp_path: Path, temp_loaded
) -> None:
    temp_loaded.config.crawler.retry_error_pages = False
    db = Database(tmp_path / "candidates.sqlite", temp_loaded.config)
    url = "https://pages.test/candidate"
    final_url = "https://pages.test/final"

    assert db.claim_candidate(url) is True
    assert db.page_status(url) == "ok"
    assert db.claim_candidate(url) is False

    db.record_page(url, final_url, "error:http_503")
    assert db.claim_candidate(final_url) is False

    temp_loaded.config.crawler.retry_error_pages = True
    assert db.claim_candidate(final_url) is True
    assert db.page_status(final_url) == "ok"

    for status_code in (400, 403, 404, 499):
        db.record_page(url, final_url, f"error:http_{status_code}")
        assert db.claim_candidate(final_url) is False

    for status_code in (408, 425, 429, 500, 503, 599):
        db.record_page(url, final_url, f"error:http_{status_code}")
        assert db.claim_candidate(final_url) is True
    assert db.enqueue(final_url, rating=80) is True
    assert db.enqueue(final_url, rating=80) is False
    db.close()


def test_reopening_database_recovers_interrupted_backlog(
    tmp_path: Path, temp_loaded
) -> None:
    path = tmp_path / "recovery.sqlite"
    temp_loaded.config.run.backlog_order = "fifo"
    temp_loaded.config.crawler.retry_error_pages = True
    db = Database(path, temp_loaded.config)
    assert db.enqueue("https://queue.test/interrupted", rating=90) is True
    assert db.pop_backlog() == "https://queue.test/interrupted"
    assert db.enqueue("https://queue.test/error", rating=70) is True
    db.mark_backlog("https://queue.test/error", "error")
    assert db.enqueue("https://queue.test/done", rating=50) is True
    db.mark_backlog("https://queue.test/done", "done")
    assert db.enqueue("https://queue.test/skipped", rating=40) is True
    db.mark_backlog("https://queue.test/skipped", "skipped_visited")
    db.close()

    reopened = Database(path, temp_loaded.config)
    assert reopened.queued_count() == 2
    assert reopened.conn.execute(
        "select count(*) from backlog where status in ('done', 'skipped_visited')"
    ).fetchone()[0] == 0
    assert reopened.pop_backlog() == "https://queue.test/interrupted"
    assert reopened.pop_backlog() == "https://queue.test/error"
    reopened.close()


def test_job_upsert_preserves_first_seen_and_synchronizes_exports(
    tmp_path: Path, temp_loaded
) -> None:
    path = tmp_path / "jobs.sqlite"
    db = Database(path, temp_loaded.config)
    original = job()
    assert db.save_jobs([original], "source-one") == 1
    db.conn.execute(
        "update jobs set first_seen_at = 'preserved', last_seen_at = 'old' where url = ?",
        (original.url,),
    )
    db.conn.commit()
    db.close()

    csv_path = tmp_path / "jobs.csv"
    reopened = Database(
        path,
        temp_loaded.config,
        csv_export_path=csv_path,
    )
    assert reopened.save_jobs([job(title="Senior Buyer")], "source-two") == 1

    row = reopened.conn.execute("select * from jobs").fetchone()
    assert row["first_seen_at"] == "preserved"
    assert row["last_seen_at"] != "old"
    assert row["title"] == "Senior Buyer"
    assert row["source_key"] == "source-two"

    with csv_path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    assert list(rows[0]) == [
        "fit_score",
        "title",
        "company",
        "location",
        "url",
        "reason",
        "evidence",
        "source_key",
        "first_seen_at",
        "last_seen_at",
        "original_url",
    ]
    assert rows[0]["title"] == "Senior Buyer"
    reopened.close()


def test_job_save_rolls_back_if_iterable_fails(tmp_path: Path, temp_loaded) -> None:
    db = Database(tmp_path / "rollback.sqlite", temp_loaded.config)

    def broken_jobs():
        yield job()
        raise RuntimeError("broken input")

    with pytest.raises(RuntimeError, match="broken input"):
        db.save_jobs(broken_jobs(), "source")

    assert db.count_rows("jobs") == 0
    db.close()
