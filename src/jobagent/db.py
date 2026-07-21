from __future__ import annotations

import csv
import datetime as dt
import sqlite3
from pathlib import Path
from typing import Iterable

from .config import JobAgentConfig
from .models import JobMatch


SCHEMA_VERSION = 2

_V2_COLUMNS = {
    "jobs": (
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
    ),
    "pages": ("url", "final_url", "status"),
    "backlog": ("url", "status", "queued_at"),
}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(
        self,
        path: Path,
        config: JobAgentConfig,
        csv_export_path: str | Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.config = config
        self.csv_export_path = csv_export_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        try:
            self.init_schema()
            self._recover_backlog()
            if self.csv_export_path:
                self.export_csv(Path(self.csv_export_path))
        except Exception:
            self.conn.close()
            raise

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        version = int(self.conn.execute("pragma user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {version} is newer than supported version {SCHEMA_VERSION}"
            )
        tables = self._table_names()
        if version == SCHEMA_VERSION:
            self._validate_v2_schema()
            return
        if tables:
            raise RuntimeError(
                "legacy database schemas are not supported; delete the database "
                "and restart to create schema v2"
            )
        self._create_fresh_v2()

    def _create_fresh_v2(self) -> None:
        try:
            self.conn.execute("begin immediate")
            self._create_v2_tables()
            self._validate_v2_schema()
            self.conn.execute(f"pragma user_version = {SCHEMA_VERSION}")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _create_v2_tables(self) -> None:
        self.conn.execute(
            """
            create table jobs (
                url text not null primary key,
                title text not null,
                company text not null,
                location text not null,
                fit_score integer not null,
                reason text not null,
                evidence text not null,
                source_key text not null,
                first_seen_at text not null,
                last_seen_at text not null,
                original_url text not null
            )
            """
        )
        self.conn.execute(
            """
            create table pages (
                url text not null primary key,
                final_url text not null,
                status text not null
            )
            """
        )
        self.conn.execute(
            """
            create table backlog (
                url text not null primary key,
                status text not null,
                queued_at text not null
            )
            """
        )

    def _validate_v2_schema(self) -> None:
        tables = self._table_names()
        if tables != set(_V2_COLUMNS):
            raise RuntimeError(f"invalid v2 database tables: {sorted(tables)}")
        for table, expected_columns in _V2_COLUMNS.items():
            info = self.conn.execute(f"pragma table_info({table})").fetchall()
            columns = tuple(str(row["name"]) for row in info)
            if columns != expected_columns:
                raise RuntimeError(f"invalid v2 schema for table {table}: {list(columns)}")
            for row in info:
                name = str(row["name"])
                if str(row["type"]).upper() != ("INTEGER" if name == "fit_score" else "TEXT"):
                    raise RuntimeError(f"invalid v2 column type for {table}.{name}")
                if name == "url":
                    if int(row["pk"]) != 1 or int(row["notnull"]) != 1:
                        raise RuntimeError(f"invalid v2 primary key for {table}.url")
                elif int(row["pk"]) != 0 or int(row["notnull"]) != 1:
                    raise RuntimeError(f"invalid v2 nullability for {table}.{name}")

    def _table_names(self) -> set[str]:
        return {
            str(row["name"])
            for row in self.conn.execute(
                "select name from sqlite_master where type = 'table' and name not like 'sqlite_%'"
            ).fetchall()
        }

    def _recover_backlog(self) -> None:
        statuses = ["active"]
        if self.config.crawler.retry_error_pages:
            statuses.append("error")
        placeholders = ", ".join("?" for _ in statuses)
        self.conn.execute(
            f"update backlog set status = 'queued' where status in ({placeholders})",
            statuses,
        )
        self.conn.commit()

    def page_status(self, url: str) -> str | None:
        row = self.conn.execute(
            """
            select status from pages
            where url = ? or final_url = ?
            order by case when url = ? then 0 else 1 end
            limit 1
            """,
            (url, url, url),
        ).fetchone()
        return str(row["status"]) if row else None

    def can_revisit(self, url: str) -> bool:
        status = self.page_status(url)
        return bool(
            status
            and (status == "error" or status.startswith("error:"))
            and self.config.crawler.retry_error_pages
        )

    def was_visited(self, url: str) -> bool:
        return self.page_status(url) is not None and not self.can_revisit(url)

    def enqueue(self, url: str) -> bool:
        if self.was_visited(url):
            return False
        timestamp = now_iso()
        cursor = self.conn.execute(
            """
            insert into backlog(url, status, queued_at) values (?, 'queued', ?)
            on conflict(url) do update set status = 'queued', queued_at = excluded.queued_at
            where backlog.status != 'queued'
            """,
            (url, timestamp),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def pop_backlog(self) -> str | None:
        order_clause = "queued_at asc" if self.config.run.backlog_order == "fifo" else "random()"
        row = self.conn.execute(
            f"select url from backlog where status = 'queued' order by {order_clause} limit 1"
        ).fetchone()
        if row is None:
            return None
        url = str(row["url"])
        self.conn.execute("update backlog set status = 'active' where url = ?", (url,))
        self.conn.commit()
        return url

    def mark_backlog(self, url: str, status: str) -> None:
        self.conn.execute("update backlog set status = ? where url = ?", (status, url))
        self.conn.commit()

    def queued_count(self) -> int:
        row = self.conn.execute("select count(*) from backlog where status = 'queued'").fetchone()
        return int(row[0])

    def reset_backlog(self) -> int:
        cursor = self.conn.execute("delete from backlog")
        self.conn.commit()
        return cursor.rowcount

    def record_page(self, url: str, final_url: str, status: str) -> None:
        self.conn.execute(
            """
            insert into pages(url, final_url, status) values (?, ?, ?)
            on conflict(url) do update set final_url = excluded.final_url, status = excluded.status
            """,
            (url, final_url, status),
        )
        self.conn.commit()

    def save_jobs(self, jobs: Iterable[JobMatch], source_key: str) -> int:
        saved = 0
        timestamp = now_iso()
        try:
            for job in jobs:
                self.conn.execute(
                    """
                    insert into jobs(
                        url, title, company, location, fit_score, reason, evidence,
                        source_key, first_seen_at, last_seen_at, original_url
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(url) do update set
                        title = excluded.title,
                        company = excluded.company,
                        location = excluded.location,
                        fit_score = excluded.fit_score,
                        reason = excluded.reason,
                        evidence = excluded.evidence,
                        source_key = excluded.source_key,
                        last_seen_at = excluded.last_seen_at,
                        original_url = excluded.original_url
                    """,
                    (
                        job.url,
                        job.title,
                        job.company,
                        job.location,
                        job.fit_score,
                        job.reason,
                        job.evidence,
                        source_key,
                        timestamp,
                        timestamp,
                        job.original_url,
                    ),
                )
                saved += 1
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        if saved:
            if self.csv_export_path:
                self.export_csv(Path(self.csv_export_path))
        return saved

    def _export_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            select fit_score, title, company, location, url, reason, evidence,
                   source_key, first_seen_at, last_seen_at, original_url
            from jobs
            order by fit_score desc, last_seen_at desc
            """
        ).fetchall()

    def export_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = self._export_rows()
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            with temporary.open("w", newline="", encoding="utf-8") as file:
                writer = csv.writer(file)
                writer.writerow(
                    [
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
                )
                writer.writerows(rows)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def count_rows(self, table: str) -> int:
        if table not in _V2_COLUMNS:
            raise ValueError(f"unknown table: {table}")
        row = self.conn.execute(f"select count(*) from {table}").fetchone()
        return int(row[0])
