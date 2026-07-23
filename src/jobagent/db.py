from __future__ import annotations

import csv
import datetime as dt
import sqlite3
from pathlib import Path
from typing import Iterable

from .config import JobAgentConfig
from .models import JobMatch


SCHEMA_VERSION = 3

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

_V3_COLUMNS = {
    **_V2_COLUMNS,
    "backlog": ("url", "status", "queued_at", "rating", "queue_position"),
}

_INTEGER_COLUMNS = {"fit_score", "rating", "queue_position"}


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
            self._validate_v3_schema()
            return
        if version == 2:
            self._validate_v2_schema()
            self._migrate_v2_to_v3()
            return
        if tables:
            raise RuntimeError(
                "legacy database schemas are not supported; delete the database "
                "and restart to create schema v3"
            )
        self._create_fresh_v3()

    def _create_fresh_v3(self) -> None:
        try:
            self.conn.execute("begin immediate")
            self._create_v3_tables()
            self._validate_v3_schema()
            self.conn.execute(f"pragma user_version = {SCHEMA_VERSION}")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _create_v3_tables(self) -> None:
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
                queued_at text not null,
                rating integer not null,
                queue_position integer not null
            )
            """
        )

    def _validate_v2_schema(self) -> None:
        self._validate_schema(_V2_COLUMNS, 2)

    def _validate_v3_schema(self) -> None:
        self._validate_schema(_V3_COLUMNS, 3)

    def _validate_schema(
        self,
        expected_schema: dict[str, tuple[str, ...]],
        version: int,
    ) -> None:
        tables = self._table_names()
        if tables != set(expected_schema):
            raise RuntimeError(f"invalid v{version} database tables: {sorted(tables)}")
        for table, expected_columns in expected_schema.items():
            info = self.conn.execute(f"pragma table_info({table})").fetchall()
            columns = tuple(str(row["name"]) for row in info)
            if columns != expected_columns:
                raise RuntimeError(
                    f"invalid v{version} schema for table {table}: {list(columns)}"
                )
            for row in info:
                name = str(row["name"])
                expected_type = "INTEGER" if name in _INTEGER_COLUMNS else "TEXT"
                if str(row["type"]).upper() != expected_type:
                    raise RuntimeError(
                        f"invalid v{version} column type for {table}.{name}"
                    )
                if name == "url":
                    if int(row["pk"]) != 1 or int(row["notnull"]) != 1:
                        raise RuntimeError(
                            f"invalid v{version} primary key for {table}.url"
                        )
                elif int(row["pk"]) != 0 or int(row["notnull"]) != 1:
                    raise RuntimeError(
                        f"invalid v{version} nullability for {table}.{name}"
                    )

    def _migrate_v2_to_v3(self) -> None:
        try:
            self.conn.execute("begin immediate")
            self.conn.execute(
                "alter table backlog add column rating integer not null default 80"
            )
            self.conn.execute(
                "alter table backlog add column queue_position integer not null default 0"
            )
            self.conn.execute(
                """
                with ordered as (
                    select rowid as backlog_rowid,
                           row_number() over (order by queued_at asc, rowid asc) as position
                    from backlog
                )
                update backlog
                set queue_position = (
                    select position from ordered
                    where ordered.backlog_rowid = backlog.rowid
                )
                """
            )
            self._validate_v3_schema()
            self.conn.execute(f"pragma user_version = {SCHEMA_VERSION}")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _table_names(self) -> set[str]:
        return {
            str(row["name"])
            for row in self.conn.execute(
                "select name from sqlite_master where type = 'table' and name not like 'sqlite_%'"
            ).fetchall()
        }

    def _recover_backlog(self) -> None:
        self.conn.execute(
            "delete from backlog where status in ('done', 'skipped_visited')"
        )
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

    @staticmethod
    def _is_retryable_http_error(status: str) -> bool:
        prefix = "error:http_"
        if not status.startswith(prefix):
            return False
        try:
            status_code = int(status.removeprefix(prefix))
        except ValueError:
            return False
        return status_code in {408, 425, 429} or 500 <= status_code <= 599

    def reset_retryable_page_errors(self) -> int:
        rows = self.conn.execute("select url, status from pages").fetchall()
        retryable_urls = [
            str(row["url"])
            for row in rows
            if self._is_retryable_http_error(str(row["status"]))
        ]
        if not retryable_urls:
            return 0
        try:
            self.conn.execute("begin immediate")
            self.conn.executemany(
                "delete from pages where url = ?",
                ((url,) for url in retryable_urls),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return len(retryable_urls)

    def enqueue(self, url: str, *, rating: int) -> bool:
        if (
            isinstance(rating, bool)
            or not isinstance(rating, int)
            or not 0 <= rating <= 100
        ):
            raise ValueError("backlog rating must be an integer from 0 to 100")
        timestamp = now_iso()
        row = self.conn.execute(
            "select coalesce(max(queue_position), 0) + 1 from backlog"
        ).fetchone()
        queue_position = int(row[0])
        cursor = self.conn.execute(
            """
            insert into backlog(url, status, queued_at, rating, queue_position)
            values (?, 'queued', ?, ?, ?)
            on conflict(url) do update set
                status = 'queued',
                queued_at = case
                    when backlog.status = 'queued' then backlog.queued_at
                    else excluded.queued_at
                end,
                rating = max(backlog.rating, excluded.rating),
                queue_position = case
                    when backlog.status = 'queued' then backlog.queue_position
                    else excluded.queue_position
                end
            where backlog.status != 'queued' or excluded.rating > backlog.rating
            """,
            (url, timestamp, rating, queue_position),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def pop_backlog(self) -> str | None:
        if self.config.run.backlog_order == "fifo":
            order_clause = "queue_position asc"
        elif self.config.run.backlog_order == "shuffle":
            order_clause = "random()"
        elif self.config.run.backlog_order == "rating":
            order_clause = "rating desc, queue_position asc"
        else:
            raise ValueError(f"unknown backlog order: {self.config.run.backlog_order}")
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

    def complete_backlog(self, url: str, final_url: str) -> None:
        try:
            self.conn.execute("begin immediate")
            self.conn.execute(
                """
                delete from pages
                where status like 'error:%'
                  and (url in (?, ?) or final_url in (?, ?))
                """,
                (url, final_url, url, final_url),
            )
            self.conn.execute("delete from backlog where url = ?", (url,))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def queued_count(self) -> int:
        row = self.conn.execute("select count(*) from backlog where status = 'queued'").fetchone()
        return int(row[0])

    def reset_backlog(self) -> int:
        cursor = self.conn.execute("delete from backlog")
        self.conn.commit()
        return cursor.rowcount

    def reset_pages(self) -> int:
        cursor = self.conn.execute("delete from pages")
        self.conn.commit()
        return cursor.rowcount

    def record_page(self, url: str, final_url: str, status: str) -> None:
        cursor = self.conn.execute(
            """
            update pages set final_url = ?, status = ?
            where url = ? or final_url = ?
            """,
            (final_url, status, url, url),
        )
        if cursor.rowcount == 0:
            self.conn.execute(
                "insert into pages(url, final_url, status) values (?, ?, ?)",
                (url, final_url, status),
            )
        self.conn.commit()

    def jobs_for_dedup(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            select * from jobs
            order by first_seen_at, url
            """
        ).fetchall()

    def save_jobs(
        self,
        jobs: Iterable[JobMatch],
        source_key: str,
        *,
        dedup_matches: dict[str, tuple[str, ...]] | None = None,
    ) -> int:
        saved = 0
        timestamp = now_iso()
        matched_urls_by_job = dedup_matches or {}
        try:
            for job in jobs:
                matched_urls = tuple(dict.fromkeys(matched_urls_by_job.get(job.url, ())))
                if matched_urls:
                    canonical_url = matched_urls[0]
                    self.conn.executemany(
                        "delete from jobs where url = ?",
                        ((url,) for url in matched_urls[1:]),
                    )
                    cursor = self.conn.execute(
                        """
                        update jobs set
                            url = ?, title = ?, company = ?, location = ?,
                            fit_score = ?, reason = ?, evidence = ?, source_key = ?,
                            last_seen_at = ?, original_url = ?
                        where url = ?
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
                            job.original_url,
                            canonical_url,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            f"canonical job disappeared during deduplication: {canonical_url}"
                        )
                    saved += 1
                    continue

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
        if table not in _V3_COLUMNS:
            raise ValueError(f"unknown table: {table}")
        row = self.conn.execute(f"select count(*) from {table}").fetchone()
        return int(row[0])
