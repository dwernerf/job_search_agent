from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import JobAgentConfig


@dataclass(slots=True)
class RunStats:
    pages: int = 0
    jobs_seen: int = 0
    jobs_saved: int = 0
    high_fit_jobs: int = 0
    errors: int = 0
    blocked: int = 0
    enqueued_urls: int = 0
    quality_total: int = 0
    quality_count: int = 0
    actions: int = 0


class ActionReporter:
    """Structured progress logging for long autonomous runs.

    There are no time/page interval summaries anymore. The reporter emits
    process-specific STEP/RESULT lines and one run summary at completion. The
    only user-facing verbosity switch is logging.level: "info" or "debug".
    """

    DEBUG_ONLY_EVENTS = {
        "skip_depth_limit",
        "skip_visited",
        "skip_source_limit",
    }

    RESULT_EVENTS = {
        "seed_backlog",
        "page_fetched",
        "page_analyzed",
        "page_complete",
        "job_validation_guard",
        "score_guard_dropped",
        "export_results",
        "backlog_empty_stop",
        "page_failed",
        "run_complete",
        "run_summary",
    }

    def __init__(
        self,
        config: JobAgentConfig,
        logger: logging.Logger,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self.clock = clock or time.monotonic
        self.started_at = self.clock()
        self.stats = RunStats()

    def action(self, name: str, **fields: Any) -> None:
        self.stats.actions += 1
        prefix = "RESULT" if name in self.RESULT_EVENTS else "STEP"
        level = logging.DEBUG if name in self.DEBUG_ONLY_EVENTS else logging.INFO
        self.logger.log(level, "%s %s %s", prefix, name, self._format_fields(fields))

    def record_page(
        self,
        *,
        status: str,
        jobs_seen: int = 0,
        jobs_saved: int = 0,
        high_fit_jobs: int = 0,
        source_quality: int = 0,
        queued: int = 0,
        force_summary: bool = False,
    ) -> None:
        self.stats.pages += 1
        self.stats.jobs_seen += max(0, jobs_seen)
        self.stats.jobs_saved += max(0, jobs_saved)
        self.stats.high_fit_jobs += max(0, high_fit_jobs)

        if status != "ok":
            self.stats.errors += 1

        if source_quality > 0:
            self.stats.quality_total += max(0, min(100, source_quality))
            self.stats.quality_count += 1

    def record_enqueued(self, count: int) -> None:
        self.stats.enqueued_urls += max(0, count)

    def maybe_summary(self, *, queued: int, force: bool = False) -> None:
        if not force:
            return
        self.run_summary(queued=queued)

    def run_summary(self, *, queued: int) -> None:
        elapsed = self.clock() - self.started_at
        avg_quality = (
            self.stats.quality_total / self.stats.quality_count
            if self.stats.quality_count
            else 0.0
        )
        self.logger.info(
            "RESULT run_summary pages=%s jobs_seen=%s jobs_saved=%s high_fit_jobs=%s "
            "enqueued_urls=%s blocked=%s errors=%s "
            "avg_source_quality=%.1f queued=%s elapsed_seconds=%.1f actions=%s",
            self.stats.pages,
            self.stats.jobs_seen,
            self.stats.jobs_saved,
            self.stats.high_fit_jobs,
            self.stats.enqueued_urls,
            self.stats.blocked,
            self.stats.errors,
            avg_quality,
            queued,
            elapsed,
            self.stats.actions,
        )

    def display_url(self, url: str) -> str:
        if not self.config.logging.show_urls:
            return "[url hidden]"
        return self._truncate(url, self.config.logging.max_url_chars)

    def display_title(self, title: str) -> str:
        return self._truncate(title, self.config.logging.max_title_chars)

    def display_notes(self, notes: str) -> str:
        return self._truncate(notes, self.config.logging.max_notes_chars)

    def _format_fields(self, fields: dict[str, Any]) -> str:
        cleaned: list[str] = []
        for key, value in fields.items():
            if value is None:
                continue
            text = str(value).replace("\n", " ").strip()
            cleaned.append(f"{key}={text!r}")
        return " ".join(cleaned)

    @staticmethod
    def _truncate(value: str, max_chars: int) -> str:
        if max_chars <= 0:
            return ""
        if len(value) <= max_chars:
            return value
        if max_chars <= 1:
            return value[:max_chars]
        return value[: max_chars - 1] + "…"
