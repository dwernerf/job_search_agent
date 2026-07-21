from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

@dataclass(slots=True)
class RunStats:
    pages: int = 0
    jobs_saved: int = 0
    errors: int = 0
    actions: int = 0


class ActionReporter:
    """Structured progress logging for long autonomous runs.

    There are no time/page interval summaries anymore. The reporter emits
    process-specific STEP/RESULT lines and one run summary at completion. The
    only user-facing verbosity switch is logging.level: "info" or "debug".
    """

    RESULT_EVENTS = {
        "seed_backlog",
        "page_fetched",
        "batch_complete",
        "page_complete",
        "backlog_empty_stop",
        "page_failed",
        "run_complete",
    }

    def __init__(
        self,
        logger: logging.Logger,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.logger = logger
        self.clock = clock or time.monotonic
        self.started_at = self.clock()
        self.stats = RunStats()

    def action(self, name: str, **fields: Any) -> None:
        self.stats.actions += 1
        prefix = "RESULT" if name in self.RESULT_EVENTS else "STEP"
        self.logger.info("%s %s %s", prefix, name, self._format_fields(fields))

    def record_page(
        self,
        *,
        status: str,
        jobs_saved: int = 0,
    ) -> None:
        self.stats.pages += 1
        self.stats.jobs_saved += max(0, jobs_saved)

        if status != "ok":
            self.stats.errors += 1

    def run_summary(self, *, queued: int) -> None:
        elapsed = self.clock() - self.started_at
        self.logger.info(
            "RESULT run_summary pages=%s jobs_saved=%s errors=%s "
            "queued=%s elapsed_seconds=%.1f actions=%s",
            self.stats.pages,
            self.stats.jobs_saved,
            self.stats.errors,
            queued,
            elapsed,
            self.stats.actions,
        )

    def _format_fields(self, fields: dict[str, Any]) -> str:
        cleaned: list[str] = []
        for key, value in fields.items():
            if value is None:
                continue
            text = str(value).replace("\n", " ").strip()
            cleaned.append(f"{key}={text!r}")
        return " ".join(cleaned)
