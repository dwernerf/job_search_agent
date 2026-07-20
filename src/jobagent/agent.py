from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Callable

from .browser import BrowserSession
from .company_filters import match_blacklist_company
from .config import LoadedConfig, ensure_data_dirs, load_config
from .db import Database
from .discover import (
    build_run_summary,
    seed_backlog,
)
from .extract import compact_text, page_decision_from_dict
from .llm import ContextWindowExceeded, LLMResponseError, LocalLLMClient
from .logging_utils import setup_logging
from .models import BacklogItem, JobMatch, LinkCandidate, LinkClassification, PageDecision, PageSnapshot
from .prompts import PromptBook
from .reporting import ActionReporter
from .urltools import domain_from_url, source_key



class JobAgent:
    def __init__(
        self,
        loaded: LoadedConfig,
        db: Database | None = None,
        browser_factory: Callable[[], BrowserSession] | None = None,
        llm_client: LocalLLMClient | None = None,
    ) -> None:
        self.loaded = loaded
        self.config = loaded.config
        self.paths = loaded.paths
        ensure_data_dirs(self.paths)
        self.logger = setup_logging(self.config, self.paths.log_path)
        self.db = db or Database(self.paths.database_path, self.config, self.paths.csv_export_path, self.paths.jsonl_export_path)
        self.browser_factory = browser_factory or (lambda: BrowserSession(self.config))
        self.reporter = ActionReporter(self.config, self.logger)

        profile_text = self.paths.profile_path.read_text(encoding="utf-8").strip()
        prompt_book = PromptBook.from_file(self.paths.prompts_path)
        self.llm_client = llm_client or LocalLLMClient(self.config, prompt_book, profile_text)

    def run(self) -> int:
        self.reporter.action(
            "run_start",
            local_area=self.config.target.local_area,
            roles=", ".join(self.config.target.roles),
        )

        if self.config.llm.require_available_on_start:
            ok, detail = self._llm_health_check()
            if not ok:
                self.reporter.action(
                    "llm_unavailable_stop",
                    base_url=self.config.llm.base_url,
                    reason=detail,
                )
                return 2
            self.reporter.action("llm_available", base_url=self.config.llm.base_url)

        if self.config.run.reset_backlog_on_start:
            cleared = self.db.reset_backlog()
            self.reporter.action("reset_backlog", cleared=cleared)
        seeded = seed_backlog(self.config, self.db, self.paths.seeds_path)
        self.reporter.action("seeded_backlog", seeded=seeded, queued=self.db.queued_count())
        self.reporter.action("seed_backlog", added=seeded, queued=self.db.queued_count())

        jobs_saved_total = 0

        with self.browser_factory() as browser:
            while True:
                item = self.db.pop_backlog()

                if item is None:
                    self.reporter.action("backlog_empty_stop", pages_done=self._pages_done_count(), jobs_saved=jobs_saved_total)
                    break

                if self.db.was_visited(item.url):
                    self.db.mark_backlog(item.url, "skipped_visited")
                    self.reporter.action("skip_visited", url=item.url)
                    continue

                source_limit = self.config.crawler.max_pages_per_source_key
                if self.db.source_visit_count(item.source_key) >= source_limit:
                    self.db.mark_backlog(item.url, "skipped_source_limit")
                    self.reporter.action("skip_source_limit", source_key=item.source_key, url=item.url)
                    continue

                source_domain = domain_from_url(item.url)
                self.db.ensure_source(item.source_key, source_domain)


                page_status = "ok"
                snapshot = PageSnapshot(url=item.url, final_url=item.url, title="", text="")
                source_quality = 0
                source_notes = ""

                try:
                    snapshot = browser.fetch(item.url)
                    final_url = snapshot.final_url or snapshot.url
                    candidate_links = snapshot.links if snapshot.links else []
                    self.reporter.action(
                        "page_fetched",
                        title=snapshot.title,
                        candidate_links=len(candidate_links),
                        final_url=snapshot.final_url,
                    )

                    # Single-stage: classify all candidate links with fetched page_context
                    saved = 0
                    enqueued = 0
                    first_source_quality = 0
                    first_source_notes = ""
                    high_fit_count = 0
                    next_depth = item.depth + 1
                    batch_size = self.config.crawler.batch_size_for_llm

                    # Build batches
                    batches: list[list[LinkCandidate]] = []
                    for i in range(0, len(candidate_links), batch_size):
                        batches.append(candidate_links[i:i + batch_size])

                    for batch_idx, batch in enumerate(batches):
                        batch_enqueued_before = enqueued
                        self.reporter.action(
                            "batch_start",
                            batch=batch_idx + 1,
                            total_batches=len(batches),
                            total_links=len(candidate_links),
                            url=item.url,
                        )
                        # Fetch page_context for each link in the batch
                        links_with_context: list[dict[str, str]] = []
                        for batch_idx_inner, link in enumerate(batch):
                            try:
                                ctx_snapshot = browser.fetch(link.url)
                                page_context = compact_text(ctx_snapshot.text, self.config)
                            except Exception as exc:
                                page_context = f"[fetch error: {type(exc).__name__}]"
                            links_with_context.append({
                                "index": str(batch_idx_inner),
                                "text": link.text,
                                "url": link.url,
                                "page_context": page_context,
                            })

                        # Classify batch
                        classification = None
                        try:
                            classification = self.llm_client.classify_links_batch(
                                snapshot=snapshot,
                                links_with_context=links_with_context,
                                memory_summary="",
                            )
                        except ContextWindowExceeded as e:
                            self.reporter.action("context_window_exceeded", batch=batch_idx, links=len(links_with_context), reason=str(e)[:500])
                            links_with_context.pop()
                            if not links_with_context:
                                self.reporter.action("batch_dropped_context_window", reason="batch emptied after dropping last link")
                                continue
                            classification = self.llm_client.classify_links_batch(
                                snapshot=snapshot,
                                links_with_context=links_with_context,
                                memory_summary="",
                            )

                        if classification is None:
                            continue

                        if batch_idx == 0:
                            first_source_quality = classification.source_quality
                            first_source_notes = classification.source_notes

                        # The LLM prompt (prompts.yaml:58) tells the model to omit URLs.
                        # Inject them from links_with_context using the classification index.
                        ctx_by_index = {int(item["index"]): item["url"] for item in links_with_context}
                        ctx_by_link_index = {item["index"]: item for item in links_with_context}
                        for c in classification.link_classifications:
                            if not c.url and c.index in ctx_by_index:
                                c.url = ctx_by_index[c.index]

                        # Process each classification
                        batch_candidates: list[JobMatch] = []
                        for c in classification.link_classifications:
                            if c.type == "job_listing" and c.fit_score >= self.config.scoring.min_score_to_export:
                                batch_candidates.append(JobMatch(
                                    title=c.title,
                                    company=c.company,
                                    location=c.location,
                                    url=c.url,
                                    fit_score=c.fit_score,
                                    reason=c.reason,
                                    evidence=c.evidence,
                                    posting_language="",
                                ))
                            elif c.type == "explore" and self.config.exploration.enabled:
                                backlog_item = self.db._make_backlog_item(
                                    url=c.url,
                                    depth=next_depth,
                                    discovered_from=item.url,
                                    reason=f"LLM explore (type=explore)",
                                    config=self.config,
                                )
                                if self.db.enqueue(backlog_item):
                                    enqueued += 1

                            # Info-level log: url + type + fit
                            self.reporter.action(
                                "link_classified",
                                url=c.url,
                                type=c.type,
                                fit=c.fit_score,
                                reason=c.reason or "",
                                chars_truncated=max(0, len((ctx_by_link_index.get(str(c.index)) or {}).get("page_context") or "") - self.config.crawler.max_page_context_chars),
                            )

                        # Clean candidates (blacklist + dedup) and save
                        cleaned = self._clean_jobs(batch_candidates)
                        page_saved = self.db.save_jobs(cleaned, item.url, item.source_key)
                        saved += page_saved
                        batch_high_fit = sum(1 for c in cleaned if c.fit_score >= self.config.scoring.high_fit_score_threshold)
                        high_fit_count += batch_high_fit
                        for job in cleaned:
                            self.db.record_page(
                                url=job.url,
                                final_url=job.url,
                                title=job.title,
                                source_key=item.source_key,
                                depth=next_depth,
                                status="ok",
                                jobs_found=page_saved,
                                high_fit_jobs=1 if job.fit_score >= self.config.scoring.high_fit_score_threshold else 0,
                                source_quality=classification.source_quality,
                                discovered_from=job.url,
                            )

                        self.reporter.action(
                            "batch_complete",
                            batch=f"{batch_idx + 1}/{len(batches)}",
                            saved=page_saved,
                            high_fit=batch_high_fit,
                            enqueued=enqueued - batch_enqueued_before,
                            queued=self.db.queued_count(),
                            source_quality=classification.source_quality,
                            source_notes=classification.source_notes,
                        )

                        if self.config.run.debug_mode:
                            for lc in links_with_context:
                                self.logger.debug("--- link %s: %s ---", lc["index"], lc["url"])
                                self.logger.debug("%s", lc.get("page_context") or "")
                                self.logger.debug("--- end link %s ---", lc["index"])

                        # Debug-level log: target page full text and LLM reasoning per link
                        if self.config.run.debug_mode:
                            self.logger.debug("=== target page: %s (url=%s) ===\n%s\n=== end target page ===",
                                              snapshot.title[:120], item.url, snapshot.text[:5000])
                            for c in classification.link_classifications:
                                lc = ctx_by_link_index.get(str(c.index))
                                page_ctx = (lc.get("page_context") or "") if lc else ""
                                self.logger.debug("classify index=%d url=%s type=%s fit=%d reason=%s\n--- page_context ---\n%s\n--- end page_context ---",
                                                  c.index, c.url, c.type, c.fit_score, (c.reason or ""), page_ctx)

                    jobs_saved_total += saved

                    self.db.record_page(
                        url=item.url,
                        final_url=final_url,
                        title=snapshot.title,
                        source_key=item.source_key,
                        depth=item.depth,
                        status="ok",
                        jobs_found=saved,
                        high_fit_jobs=high_fit_count,
                        source_quality=first_source_quality,
                        discovered_from=item.discovered_from,
                    )

                    self.db.mark_backlog(item.url, "done")
                    self.reporter.record_page(
                        status="ok",
                        jobs_seen=saved,
                        jobs_saved=saved,
                        high_fit_jobs=high_fit_count,
                        source_quality=first_source_quality,
                        queued=self.db.queued_count(),
                    )
                    self.reporter.action(
                        "page_complete",
                        saved=saved,
                        high_fit=high_fit_count,
                        enqueued=enqueued,
                        queued=self.db.queued_count(),
                        source_quality=first_source_quality,
                        source_notes=first_source_notes,
                        title=snapshot.title,
                    )
                    self.logger.debug(
                        "done jobs=%s source_quality=%s queued=%s title=%r",
                        saved,
                        first_source_quality,
                        self.db.queued_count(),
                        snapshot.title[:120],
                    )

                except Exception as exc:
                    page_status = f"error:{type(exc).__name__}"
                    self.db.record_page(
                        url=item.url,
                        final_url=snapshot.final_url if hasattr(snapshot, 'final_url') and snapshot.final_url else "",
                        title=snapshot.title if hasattr(snapshot, 'title') and snapshot.title else "",
                        source_key=item.source_key,
                        depth=item.depth,
                        status=page_status,
                        jobs_found=0,
                        high_fit_jobs=0,
                        source_quality=0,
                        discovered_from=item.discovered_from,
                    )
                    self.db.mark_backlog(item.url, "error")
                    self.reporter.action("page_failed", status=page_status, url=item.url, reason=str(exc)[:500])
                    self.reporter.record_page(status=page_status, queued=self.db.queued_count())

                self._delay()

        self.reporter.action(
            "run_complete",
            jobs_saved_total=jobs_saved_total,
            queued=self.db.queued_count(),
        )
        self.reporter.maybe_summary(queued=self.db.queued_count(), force=True)
        return 0

    def _pages_done_count(self) -> int:
        """Count completed (non-queued) backlog items."""
        return self.db.count_rows("backlog") - self.db.queued_count()

    def _llm_health_check(self) -> tuple[bool, str]:
        health = getattr(self.llm_client, "health_check", None)
        if health is None:
            return True, "custom LLM client has no health_check method"
        try:
            result = health()
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if isinstance(result, tuple) and len(result) == 2:
            return bool(result[0]), str(result[1])
        return bool(result), "ok" if result else "health_check returned false"

    def _clean_jobs(self, jobs: list[JobMatch]) -> list[JobMatch]:
        cleaned: list[JobMatch] = []
        seen: set[str] = set()
        for job in jobs:
            if self.config.job_validation.drop_if_company_blacklisted and self._company_blacklisted(job):
                self.reporter.action("job_dropped_blacklist", company=job.company[:80], url=job.url[:220])
                continue
            if job.url in seen:
                self.reporter.action("job_dropped_dedup", url=job.url[:220])
                continue
            seen.add(job.url)
            cleaned.append(job)
        return cleaned

    def _company_blacklisted(self, job: JobMatch) -> bool:
        return match_blacklist_company(self.config, job.company, job.title, job.url, job.reason, job.evidence) is not None

    def _delay(self) -> None:
        delay = random.uniform(
            self.config.run.min_delay_seconds,
            self.config.run.max_delay_seconds,
        )
        if delay > 0:
            time.sleep(delay)


def main() -> int:
    loaded = load_config()
    return JobAgent(loaded).run()


if __name__ == "__main__":
    raise SystemExit(main())
