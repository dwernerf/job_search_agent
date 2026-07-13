from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .config import JobAgentConfig
from .models import BacklogItem, JobMatch
from .urltools import domain_from_url, source_key


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path, config: JobAgentConfig) -> None:
        self.path = path
        self.config = config
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            create table if not exists pages (
                url text primary key,
                final_url text not null,
                title text not null,
                source_key text not null,
                depth integer not null,
                status text not null,
                jobs_found integer not null,
                high_fit_jobs integer not null,
                source_quality integer not null,
                discovered_from text not null,
                visited_at text not null
            );

            create table if not exists jobs (
                url text primary key,
                title text not null,
                company text not null,
                location text not null,
                posting_language text not null default '',
                fit_score integer not null,
                score_source text not null default '',
                score_basis text not null default '',
                reason text not null,
                evidence text not null,
                source_page text not null,
                source_key text not null,
                first_seen_at text not null,
                last_seen_at text not null
            );

            create table if not exists backlog (
                url text primary key,
                depth integer not null,
                discovered_from text not null,
                reason text not null,
                source_key text not null,
                status text not null,
                queued_at text not null,
                updated_at text not null
            );

            create table if not exists source_memory (
                source_key text primary key,
                domain text not null,
                score real not null default 50.0,
                visits integer not null default 0,
                jobs_found integer not null default 0,
                high_fit_jobs integer not null default 0,
                errors integer not null default 0,
                blocked integer not null default 0,
                no_job_streak integer not null default 0,
                last_quality integer not null default 0,
                notes text not null default '',
                first_seen_at text not null,
                last_seen_at text not null
            );

            create table if not exists events (
                id integer primary key autoincrement,
                event_type text not null,
                payload_json text not null,
                at text not null
            );
            """
        )
        self.conn.commit()
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        job_columns = {row["name"] for row in self.conn.execute("pragma table_info(jobs)").fetchall()}
        if "posting_language" not in job_columns:
            self.conn.execute("alter table jobs add column posting_language text not null default ''")
        if "score_source" not in job_columns:
            self.conn.execute("alter table jobs add column score_source text not null default ''")
        if "score_basis" not in job_columns:
            self.conn.execute("alter table jobs add column score_basis text not null default ''")
        self.conn.commit()

    def log_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "insert into events(event_type, payload_json, at) values (?, ?, ?)",
            (event_type, json.dumps(payload, ensure_ascii=False), now_iso()),
        )
        self.conn.commit()

    def ensure_source(self, source_key: str, domain: str) -> None:
        timestamp = now_iso()
        self.conn.execute(
            """
            insert or ignore into source_memory(
                source_key, domain, score, visits, jobs_found, high_fit_jobs,
                errors, blocked, no_job_streak, last_quality, notes,
                first_seen_at, last_seen_at
            ) values (?, ?, ?, 0, 0, 0, 0, 0, 0, 0, '', ?, ?)
            """,
            (source_key, domain, self.config.memory.initial_score, timestamp, timestamp),
        )
        self.conn.commit()

    def source_visit_count(self, source_key: str) -> int:
        row = self.conn.execute(
            "select visits from source_memory where source_key = ?",
            (source_key,),
        ).fetchone()
        return int(row["visits"]) if row else 0

    def page_status(self, url: str) -> str | None:
        row = self.conn.execute("select status from pages where url = ? or final_url = ?", (url, url)).fetchone()
        return str(row["status"]) if row else None

    def can_revisit(self, url: str) -> bool:
        status = self.page_status(url)
        if status is None:
            return False
        if status.startswith("error:") and self.config.crawler.retry_error_pages:
            return True
        return False

    def clear_backlog_history(self) -> int:
        before = self.conn.total_changes
        self.conn.execute("delete from backlog where status != 'queued'")
        self.conn.commit()
        return self.conn.total_changes - before

    def reset_backlog(self) -> int:
        before = self.conn.total_changes
        self.conn.execute("delete from backlog")
        self.conn.commit()
        return self.conn.total_changes - before

    def _make_backlog_item(
        self,
        url: str,
        depth: int,
        discovered_from: str,
        reason: str,
        config: JobAgentConfig,
    ) -> BacklogItem:
        skey = source_key(url, config)
        return BacklogItem(
            url=url,
            depth=depth,
            discovered_from=discovered_from,
            reason=reason,
            source_key=skey,
        )

    def enqueue(self, item: BacklogItem) -> bool:
        revisiting = self.can_revisit(item.url)
        if self.was_visited(item.url) and not revisiting:
            return False

        current_visits = self.source_visit_count(item.source_key)
        if current_visits >= self.config.crawler.max_pages_per_source_key and not revisiting:
            return False

        timestamp = now_iso()
        existing = self.conn.execute(
            "select status from backlog where url = ?",
            (item.url,),
        ).fetchone()

        before = self.conn.total_changes
        if existing is not None and existing["status"] != "queued" and (revisiting or self.page_status(item.url) is None):
            self.conn.execute(
                """
                update backlog set
                    depth = ?,
                    discovered_from = ?,
                    reason = ?,
                    source_key = ?,
                    status = 'queued',
                    queued_at = ?,
                    updated_at = ?
                where url = ?
                """,
                (
                    item.depth,
                    item.discovered_from,
                    item.reason,
                    item.source_key,
                    timestamp,
                    timestamp,
                    item.url,
                ),
            )
        else:
            self.conn.execute(
                """
                insert into backlog(
                    url, depth, discovered_from, reason, source_key,
                    status, queued_at, updated_at
                ) values (?, ?, ?, ?, ?, 'queued', ?, ?)
                on conflict(url) do update set
                    depth = min(backlog.depth, excluded.depth),
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                where backlog.status = 'queued'
                """,
                (
                    item.url,
                    item.depth,
                    item.discovered_from,
                    item.reason,
                    item.source_key,
                    timestamp,
                    timestamp,
                ),
            )
        self.conn.commit()
        return self.conn.total_changes > before

    def pop_backlog(self) -> BacklogItem | None:
        row = self.conn.execute(
            """
            select * from backlog
            where status = 'queued'
            {order_clause}
            limit 1
            """.format(
                order_clause="ORDER BY queued_at ASC"
                if self.config.run.backlog_order == "fifo"
                else "ORDER BY random()"
            )
        ).fetchone()
        if row is None:
            return None

        self.conn.execute(
            "update backlog set status = 'active', updated_at = ? where url = ?",
            (now_iso(), row["url"]),
        )
        self.conn.commit()
        return BacklogItem(
            url=row["url"],
            depth=int(row["depth"]),
            discovered_from=row["discovered_from"],
            reason=row["reason"],
            source_key=row["source_key"],
        )

    def mark_backlog(self, url: str, status: str) -> None:
        self.conn.execute(
            "update backlog set status = ?, updated_at = ? where url = ?",
            (status, now_iso(), url),
        )
        self.conn.commit()

    def queued_count(self) -> int:
        row = self.conn.execute(
            "select count(*) as n from backlog where status = 'queued'"
        ).fetchone()
        return int(row["n"])

    def was_visited(self, url: str) -> bool:
        row = self.conn.execute("select status from pages where url = ? or final_url = ?", (url, url)).fetchone()
        if row is None:
            return False
        return not self.can_revisit(url)

    def record_page(
        self,
        url: str,
        final_url: str,
        title: str,
        source_key: str,
        depth: int,
        status: str,
        jobs_found: int,
        high_fit_jobs: int,
        source_quality: int,
        discovered_from: str,
    ) -> None:
        timestamp = now_iso()
        payload = (
            title,
            source_key,
            depth,
            status,
            jobs_found,
            high_fit_jobs,
            source_quality,
            discovered_from,
            timestamp,
        )
        for page_url in list(dict.fromkeys([url, final_url])):
            self.conn.execute(
                """
                insert or replace into pages(
                    url, final_url, title, source_key, depth, status, jobs_found,
                    high_fit_jobs, source_quality, discovered_from, visited_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    page_url,
                    final_url,
                    *payload,
                ),
            )
        self.conn.commit()

    def save_jobs(self, jobs: Iterable[JobMatch], source_page: str, source_key: str) -> int:
        saved = 0
        timestamp = now_iso()

        for job in jobs:
            existing = self.conn.execute(
                "select first_seen_at from jobs where url = ?",
                (job.url,),
            ).fetchone()
            first_seen = existing["first_seen_at"] if existing else timestamp
            self.conn.execute(
                """
                insert into jobs(
                    url, title, company, location, posting_language, fit_score, score_source, score_basis, reason, evidence,
                    source_page, source_key, first_seen_at, last_seen_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(url) do update set
                    title = excluded.title,
                    company = excluded.company,
                    location = excluded.location,
                    posting_language = excluded.posting_language,
                    fit_score = excluded.fit_score,
                    score_source = excluded.score_source,
                    score_basis = excluded.score_basis,
                    reason = excluded.reason,
                    evidence = excluded.evidence,
                    source_page = excluded.source_page,
                    source_key = excluded.source_key,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    job.url,
                    job.title,
                    job.company,
                    job.location,
                    job.posting_language,
                    job.fit_score,
                    "llm",
                    "",
                    job.reason,
                    job.evidence,
                    source_page,
                    source_key,
                    first_seen,
                    timestamp,
                ),
            )
            saved += 1

        self.conn.commit()
        return saved

    def top_sources(self, limit: int) -> list[Any]:
        rows = self.conn.execute(
            """
            select * from source_memory
            where visits > 0
            order by score desc, high_fit_jobs desc, jobs_found desc, visits desc
            limit ?
            """,
            (limit,),
        ).fetchall()
        return [self._row_to_source(r) for r in rows]

    def bad_sources(self, limit: int) -> list[Any]:
        rows = self.conn.execute(
            """
            select * from source_memory
            where visits > 0
            order by score asc, no_job_streak desc, errors desc
            limit ?
            """,
            (limit,),
        ).fetchall()
        return [self._row_to_source(r) for r in rows]

    def recent_jobs(self, limit: int) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            """
            select title, company, location, posting_language, fit_score, source_key, url, reason, evidence
            from jobs
            order by last_seen_at desc
            limit ?
            """,
            (max(limit * 4, limit),),
        ).fetchall()
        return rows[:limit]

    def top_source_summary(self, limit: int) -> str:
        sources = self.top_sources(limit)
        if not sources:
            return "no visited sources yet"
        return "; ".join(
            f"{s.source_key}:score={s.score:.1f},visits={s.visits},jobs={s.jobs_found},high_fit={s.high_fit_jobs}"
            for s in sources
        )

    def memory_summary(self) -> str:
        parts: list[str] = []

        top = self.top_sources(self.config.memory.top_sources_in_prompt)
        if top:
            parts.append("Good sources:")
            parts.extend(
                f"- {s.source_key}: score={s.score:.1f}, jobs={s.jobs_found}, high_fit={s.high_fit_jobs}, notes={s.notes}"
                for s in top
            )

        bad = self.bad_sources(self.config.memory.bad_sources_in_prompt)
        if bad:
            parts.append("Weak sources:")
            parts.extend(
                f"- {s.source_key}: score={s.score:.1f}, no_job_streak={s.no_job_streak}, errors={s.errors}, notes={s.notes}"
                for s in bad
            )

        jobs = self.recent_jobs(self.config.memory.recent_jobs_in_prompt)
        if jobs:
            parts.append("Recent matched jobs:")
            parts.extend(
                f"- {row['fit_score']} {row['title']} at {row['company']} ({row['location']}; language={row['posting_language'] or 'unknown'}) from {row['source_key']}"
                for row in jobs
            )

        if not parts:
            return "No source memory yet. Explore conservatively and prefer public career/job pages."

        return "\n".join(parts)

    def _export_rows(self) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            """
            select fit_score, title, company, location, posting_language, url, reason, evidence, source_key, first_seen_at, last_seen_at
            from jobs
            order by fit_score desc, last_seen_at desc
            """
        ).fetchall()
        return rows

    def export_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = self._export_rows()
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "fit_score",
                    "title",
                    "company",
                    "location",
                    "posting_language",
                    "url",
                    "reason",
                    "evidence",
                    "source_key",
                    "first_seen_at",
                    "last_seen_at",
                ]
            )
            writer.writerows(rows)

    def export_jsonl(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = self._export_rows()
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(dict(row), ensure_ascii=False) + "\n")

    def count_rows(self, table: str) -> int:
        row = self.conn.execute(f"select count(*) as n from {table}").fetchone()
        return int(row["n"])

    @staticmethod
    def _row_to_source(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "source_key": row["source_key"],
            "domain": row["domain"],
            "score": float(row["score"]),
            "visits": int(row["visits"]),
            "jobs_found": int(row["jobs_found"]),
            "high_fit_jobs": int(row["high_fit_jobs"]),
            "errors": int(row["errors"]),
            "blocked": int(row["blocked"]),
            "no_job_streak": int(row["no_job_streak"]),
            "last_quality": int(row["last_quality"]),
            "notes": row["notes"],
        }
