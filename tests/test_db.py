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
    "backlog": ["url", "status", "queued_at"],
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


def test_fresh_database_creates_strict_v2_schema(tmp_path: Path, temp_loaded) -> None:
    db = Database(tmp_path / "fresh.sqlite", temp_loaded.config)

    assert db.conn.execute("pragma user_version").fetchone()[0] == 2
    assert SCHEMA_VERSION == 2
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
    conn.execute(f"pragma user_version = {SCHEMA_VERSION}")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="invalid v2 primary key"):
        Database(path, temp_loaded.config)


def test_backlog_rejects_duplicates_and_retries_error_pages(
    tmp_path: Path, temp_loaded
) -> None:
    db = Database(tmp_path / "backlog.sqlite", temp_loaded.config)

    assert db.enqueue("https://queue.test/new") is True
    assert db.enqueue("https://queue.test/new") is False
    assert db.queued_count() == 1
    assert db.pop_backlog() == "https://queue.test/new"
    assert db.pop_backlog() is None
    db.mark_backlog("https://queue.test/new", "done")

    db.record_page("https://pages.test/error", "https://pages.test/error", "error:Timeout")
    assert db.can_revisit("https://pages.test/error") is True
    assert db.enqueue("https://pages.test/error") is True

    db.record_page("https://pages.test/ok", "https://pages.test/final", "ok")
    assert db.was_visited("https://pages.test/final") is True
    assert db.enqueue("https://pages.test/final") is False
    assert db.reset_backlog() == 2
    db.close()


def test_reopening_database_recovers_interrupted_backlog(
    tmp_path: Path, temp_loaded
) -> None:
    path = tmp_path / "recovery.sqlite"
    temp_loaded.config.run.backlog_order = "fifo"
    db = Database(path, temp_loaded.config)
    assert db.enqueue("https://queue.test/interrupted") is True
    assert db.pop_backlog() == "https://queue.test/interrupted"
    assert db.enqueue("https://queue.test/error") is True
    db.mark_backlog("https://queue.test/error", "error")
    db.close()

    reopened = Database(path, temp_loaded.config)
    assert reopened.queued_count() == 2
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
