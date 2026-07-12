from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .config import JobAgentConfig
from .models import FrontierItem, JobMatch, SourceMemoryRow
from .urltools import domain_from_url


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
            create table if not exists source_memory (
                source_key text primary key,
                domain text not null,
                score real not null,
                visits integer not null,
                jobs_found integer not null,
                high_fit_jobs integer not null,
                errors integer not null,
                blocked integer not null,
                no_job_streak integer not null,
                last_quality integer not null,
                notes text not null,
                first_seen_at text not null,
                last_seen_at text not null
            );

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

            create table if not exists frontier (
                url text primary key,
                depth integer not null,
                priority real not null,
                discovered_from text not null,
                reason text not null,
                source_key text not null,
                status text not null,
                queued_at text not null,
                updated_at text not null
            );

            create table if not exists queries (
                query text primary key,
                reason text not null,
                uses integer not null,
                jobs_found integer not null,
                score real not null,
                generated_by text not null,
                created_at text not null,
                last_used_at text not null
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

    def apply_decay(self) -> None:
        cfg = self.config.memory
        self.conn.execute(
            """
            update source_memory
            set score = ?, last_seen_at = last_seen_at
            where score is null
            """,
            (cfg.initial_score,),
        )
        self.conn.execute(
            """
            update source_memory
            set score = ? + ((score - ?) * ?)
            """,
            (cfg.initial_score, cfg.initial_score, cfg.decay_per_run),
        )
        self.conn.commit()

    def ensure_source(self, source_key: str, domain: str) -> SourceMemoryRow:
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
        return self.get_source(source_key)

    def get_source(self, source_key: str) -> SourceMemoryRow:
        row = self.conn.execute(
            "select * from source_memory where source_key = ?",
            (source_key,),
        ).fetchone()
        if row is None:
            raise KeyError(source_key)
        return SourceMemoryRow(
            source_key=row["source_key"],
            domain=row["domain"],
            score=float(row["score"]),
            visits=int(row["visits"]),
            jobs_found=int(row["jobs_found"]),
            high_fit_jobs=int(row["high_fit_jobs"]),
            errors=int(row["errors"]),
            blocked=int(row["blocked"]),
            no_job_streak=int(row["no_job_streak"]),
            last_quality=int(row["last_quality"]),
            notes=row["notes"],
        )

    def source_score(self, source_key: str, domain: str) -> float:
        row = self.conn.execute(
            "select score from source_memory where source_key = ?",
            (source_key,),
        ).fetchone()
        if row is None:
            self.ensure_source(source_key, domain)
            return self.config.memory.initial_score
        return float(row["score"])

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
        if (
            status == "blocked_by_robots"
            and not self.config.crawler.respect_robots_txt
            and self.config.crawler.retry_previously_blocked_when_robots_disabled
        ):
            return True
        if status.startswith("error:") and self.config.crawler.retry_error_pages:
            return True
        return False

    def clear_frontier_history(self) -> int:
        before = self.conn.total_changes
        self.conn.execute("delete from frontier where status != 'queued'")
        self.conn.commit()
        return self.conn.total_changes - before

    def reset_frontier(self) -> int:
        # Clears only the crawl queue. Learned source_memory and saved jobs remain intact.
        before = self.conn.total_changes
        self.conn.execute("delete from frontier")
        self.conn.commit()
        return self.conn.total_changes - before

    def update_source_visit_limit(self, source_key: str, visits: int) -> None:
        self.conn.execute(
            "update source_memory set visits = ? where source_key = ?",
            (visits, source_key),
        )
        self.conn.commit()

    def update_source_memory(
        self,
        source_key: str,
        domain: str,
        status: str,
        jobs_found: int,
        high_fit_jobs: int,
        source_quality: int,
        notes: str,
    ) -> SourceMemoryRow:
        current = self.ensure_source(source_key, domain)
        cfg = self.config.memory
        delta = 0.0
        errors = current.errors
        blocked = current.blocked
        no_job_streak = current.no_job_streak

        if status == "ok":
            delta += jobs_found * cfg.reward_job_found
            delta += high_fit_jobs * cfg.reward_high_fit_job
            delta += (max(0, min(100, source_quality)) / 100.0) * cfg.reward_source_quality
            if jobs_found == 0:
                no_job_streak += 1
                delta += cfg.penalty_no_job
                if no_job_streak >= cfg.no_job_streak_penalty_after:
                    delta += cfg.penalty_no_job
            else:
                no_job_streak = 0
            if source_quality <= 30:
                delta += cfg.penalty_bad_source_quality
        elif status == "blocked_by_robots":
            blocked += 1
            no_job_streak += 1
            delta += cfg.penalty_blocked_by_robots
        else:
            errors += 1
            no_job_streak += 1
            delta += cfg.penalty_error

        new_score = max(cfg.min_score, min(cfg.max_score, current.score + delta))
        merged_notes = notes.strip() or current.notes
        timestamp = now_iso()

        self.conn.execute(
            """
            update source_memory set
                score = ?,
                visits = visits + 1,
                jobs_found = jobs_found + ?,
                high_fit_jobs = high_fit_jobs + ?,
                errors = ?,
                blocked = ?,
                no_job_streak = ?,
                last_quality = ?,
                notes = ?,
                last_seen_at = ?
            where source_key = ?
            """,
            (
                new_score,
                jobs_found,
                high_fit_jobs,
                errors,
                blocked,
                no_job_streak,
                max(0, min(100, source_quality)),
                merged_notes[:1000],
                timestamp,
                source_key,
            ),
        )
        self.conn.commit()
        return self.get_source(source_key)

    def enqueue(self, item: FrontierItem) -> bool:
        revisiting = self.can_revisit(item.url)
        if self.was_visited(item.url) and not revisiting:
            return False

        current_visits = self.source_visit_count(item.source_key)
        if current_visits >= self.config.crawler.max_pages_per_source_key and not revisiting:
            return False

        if self.source_score(item.source_key, domain_from_url(item.url)) <= self.config.memory.blacklist_below_score and not revisiting:
            return False

        timestamp = now_iso()
        existing = self.conn.execute(
            "select status from frontier where url = ?",
            (item.url,),
        ).fetchone()

        before = self.conn.total_changes
        if existing is not None and existing["status"] != "queued" and (revisiting or self.page_status(item.url) is None):
            self.conn.execute(
                """
                update frontier set
                    depth = ?,
                    priority = ?,
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
                    item.priority,
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
                insert into frontier(
                    url, depth, priority, discovered_from, reason, source_key,
                    status, queued_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                on conflict(url) do update set
                    priority = max(frontier.priority, excluded.priority),
                    depth = min(frontier.depth, excluded.depth),
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                where frontier.status = 'queued'
                """,
                (
                    item.url,
                    item.depth,
                    item.priority,
                    item.discovered_from,
                    item.reason,
                    item.source_key,
                    timestamp,
                    timestamp,
                ),
            )
        self.conn.commit()
        return self.conn.total_changes > before

    def pop_frontier(self) -> FrontierItem | None:
        row = self.conn.execute(
            """
            select * from frontier
            where status = 'queued'
            order by priority desc, queued_at asc
            limit 1
            """
        ).fetchone()
        if row is None:
            return None

        self.conn.execute(
            "update frontier set status = 'active', updated_at = ? where url = ?",
            (now_iso(), row["url"]),
        )
        self.conn.commit()
        return FrontierItem(
            url=row["url"],
            depth=int(row["depth"]),
            priority=float(row["priority"]),
            discovered_from=row["discovered_from"],
            reason=row["reason"],
            source_key=row["source_key"],
        )

    def mark_frontier(self, url: str, status: str) -> None:
        self.conn.execute(
            "update frontier set status = ?, updated_at = ? where url = ?",
            (status, now_iso(), url),
        )
        self.conn.commit()

    def queued_count(self) -> int:
        row = self.conn.execute(
            "select count(*) as n from frontier where status = 'queued'"
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
                    job.score_source,
                    job.score_basis,
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


    def recalibrate_existing_jobs(self) -> dict[str, int]:
        """Re-apply current safety, location, and score guardrails to old rows.

        This is intentionally conservative. It can remove old wrong-city,
        initiative-application, index-page, and over-scored rows after config
        changes without requiring users to delete the whole SQLite database.
        """
        from .scoring import normalize_job_score
        from .urltools import denied_by_safety

        rows = self.conn.execute(
            """
            select url, title, company, location, posting_language, fit_score, score_source, score_basis, reason, evidence
            from jobs
            """
        ).fetchall()
        checked = adjusted = dropped = 0

        def matches(patterns: list[str], *values: str) -> bool:
            for pattern in patterns:
                try:
                    if any(re.search(pattern, value or "") for value in values):
                        return True
                except re.error:
                    continue
            return False

        for row in rows:
            checked += 1
            job = JobMatch(
                title=row["title"],
                company=row["company"],
                location=row["location"],
                url=row["url"],
                fit_score=int(row["fit_score"]),
                reason=row["reason"],
                evidence=row["evidence"],
                posting_language=row["posting_language"],
                score_source=row["score_source"] or "legacy",
                score_basis=row["score_basis"],
            )

            combined = "\n".join([job.title, job.company, job.location, job.reason, job.evidence, job.url])
            validation_drop = False
            if denied_by_safety(job.url, job.title, self.config):
                validation_drop = True
            elif self.config.job_validation.enabled:
                validation_drop = matches(self.config.job_validation.drop_if_title_or_url_matches, combined)
                validation_drop = validation_drop or (
                    self.config.job_validation.drop_if_url_is_index_page
                    and matches(self.config.job_validation.index_url_patterns, job.url)
                )

            normalized = None if validation_drop else normalize_job_score(job, self.config)
            if normalized is None:
                self.conn.execute("delete from jobs where url = ?", (job.url,))
                dropped += 1
                continue

            if (
                normalized.fit_score != job.fit_score
                or normalized.score_source != job.score_source
                or normalized.score_basis != job.score_basis
            ):
                self.conn.execute(
                    """
                    update jobs
                    set fit_score = ?, score_source = ?, score_basis = ?, last_seen_at = last_seen_at
                    where url = ?
                    """,
                    (normalized.fit_score, normalized.score_source, normalized.score_basis, job.url),
                )
                adjusted += 1

        self.conn.commit()
        return {"checked": checked, "adjusted": adjusted, "dropped": dropped}

    def top_sources(self, limit: int) -> list[SourceMemoryRow]:
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

    def bad_sources(self, limit: int) -> list[SourceMemoryRow]:
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
            select title, company, location, posting_language, fit_score, score_source, score_basis, source_key, url, reason, evidence
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
                f"- {row['fit_score']} {row['title']} at {row['company']} ({row['location']}; score_source={row['score_source'] or 'unknown'}; language={row['posting_language'] or 'unknown'}) from {row['source_key']}"
                for row in jobs
            )

        if not parts:
            return "No source memory yet. Explore conservatively and prefer public career/job pages."

        return "\n".join(parts)

    def save_query(self, query: str, reason: str, generated_by: str) -> bool:
        timestamp = now_iso()
        before = self.conn.total_changes
        self.conn.execute(
            """
            insert into queries(query, reason, uses, jobs_found, score, generated_by, created_at, last_used_at)
            values (?, ?, 0, 0, 0.0, ?, ?, ?)
            on conflict(query) do nothing
            """,
            (query, reason, generated_by, timestamp, timestamp),
        )
        self.conn.commit()
        return self.conn.total_changes > before

    def mark_query_used(self, query: str) -> None:
        self.conn.execute(
            """
            update queries
            set uses = uses + 1, last_used_at = ?
            where query = ?
            """,
            (now_iso(), query),
        )
        self.conn.commit()

    def _export_rows(self) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            """
            select fit_score, score_source, title, score_basis, company, location, posting_language, url, reason, evidence, source_key, first_seen_at, last_seen_at
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
                    "score_source",
                    "title",
                    "score_basis",
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
    def _row_to_source(row: sqlite3.Row) -> SourceMemoryRow:
        return SourceMemoryRow(
            source_key=row["source_key"],
            domain=row["domain"],
            score=float(row["score"]),
            visits=int(row["visits"]),
            jobs_found=int(row["jobs_found"]),
            high_fit_jobs=int(row["high_fit_jobs"]),
            errors=int(row["errors"]),
            blocked=int(row["blocked"]),
            no_job_streak=int(row["no_job_streak"]),
            last_quality=int(row["last_quality"]),
            notes=row["notes"],
        )
