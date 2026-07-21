from __future__ import annotations

import logging
from typing import Callable, Protocol

from .browser import BrowserFetchError, BrowserSession
from .company_filters import matches_blacklisted_company
from .config import LoadedConfig, ensure_data_dirs, load_config
from .db import Database
from .discover import seed_backlog
from .extract import compact_text
from .llm import ContextWindowExceeded, LocalLLMClient
from .logging_utils import setup_logging
from .models import JobMatch, LinkCandidate, PageDecision, PageSnapshot
from .prompts import PromptBook
from .reporting import ActionReporter
from .urltools import filter_links, filter_url, source_key


class BrowserClient(Protocol):
    def __enter__(self) -> "BrowserClient": ...

    def __exit__(self, exc_type, exc, tb) -> None: ...

    def fetch(self, url: str) -> PageSnapshot: ...


class LLMClient(Protocol):
    def classify_links_batch(
        self,
        snapshot: PageSnapshot,
        links_with_context: list[dict[str, str]],
    ) -> PageDecision: ...


class JobAgent:
    def __init__(
        self,
        loaded: LoadedConfig,
        db: Database | None = None,
        browser_factory: Callable[[], BrowserClient] | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.config = loaded.config
        self.paths = loaded.paths
        ensure_data_dirs(self.paths)
        self.logger = setup_logging(self.config, self.paths.log_path)
        self.db = db or Database(
            self.paths.database_path,
            self.config,
            self.paths.csv_export_path,
        )
        self.browser_factory = browser_factory or (lambda: BrowserSession(self.config))
        self.reporter = ActionReporter(self.logger)

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
        self.reporter.action("seed_backlog", added=seeded, queued=self.db.queued_count())

        jobs_saved_total = 0

        with self.browser_factory() as browser:
            while True:
                item_url = self.db.pop_backlog()

                if item_url is None:
                    self.reporter.action(
                        "backlog_empty_stop",
                        pages_done=self.reporter.stats.pages,
                        jobs_saved=jobs_saved_total,
                    )
                    break

                current_source_key = source_key(item_url)
                snapshot = PageSnapshot(url=item_url, final_url=item_url, title="", text="")
                saved = 0
                candidate_fetch_failures = 0

                try:
                    snapshot = browser.fetch(item_url)
                    final_url = snapshot.final_url or snapshot.url
                    candidate_links = filter_links(
                        snapshot.links,
                        final_url,
                        self.config,
                    )
                    self.reporter.action(
                        "page_fetched",
                        title=snapshot.title,
                        candidate_links=len(candidate_links),
                        final_url=snapshot.final_url,
                    )

                    # Single-stage: classify all candidate links with fetched page_context
                    enqueued = 0
                    batch_size = self.config.crawler.batch_size_for_llm
                    claimed_candidate_urls: set[str] = set()

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
                            url=item_url,
                        )
                        # Fetch page_context for each link in the batch
                        links_with_context: list[dict[str, str]] = []
                        fetched_links: list[LinkCandidate] = []
                        batch_fetch_failures_before = candidate_fetch_failures
                        for link in batch:
                            if link.url not in claimed_candidate_urls:
                                if not self.db.claim_candidate(link.url):
                                    self.reporter.action(
                                        "candidate_url_dropped",
                                        url=link.url,
                                        reason="already present in pages",
                                    )
                                    continue
                                claimed_candidate_urls.add(link.url)
                            try:
                                ctx_snapshot = browser.fetch(link.url)
                                self.db.record_page(
                                    url=link.url,
                                    final_url=ctx_snapshot.final_url or link.url,
                                    status="ok",
                                )
                                page_context = compact_text(ctx_snapshot.text, self.config)
                            except BrowserFetchError as exc:
                                candidate_fetch_failures += 1
                                if exc.kind == "http":
                                    self.db.record_page(
                                        url=link.url,
                                        final_url=exc.final_url or link.url,
                                        status=exc.page_status,
                                    )
                                self.reporter.action(
                                    "link_candidate_fetch_failed",
                                    status=exc.page_status,
                                    url=link.url,
                                )
                                continue
                            except Exception as exc:
                                candidate_fetch_failures += 1
                                self.reporter.action(
                                    "link_candidate_fetch_failed",
                                    status=f"error:{type(exc).__name__}",
                                    url=link.url,
                                )
                                continue
                            candidate_final_url = filter_url(
                                ctx_snapshot.final_url or link.url,
                                None,
                                self.config,
                            )
                            if not candidate_final_url:
                                self.reporter.action(
                                    "candidate_url_dropped",
                                    url=link.url,
                                    reason="final URL rejected by URL policy",
                                )
                                continue
                            fetched_links.append(link)
                            links_with_context.append({
                                "index": str(len(links_with_context)),
                                "text": link.text,
                                "original_url": link.url,
                                "url": candidate_final_url,
                                "page_title": ctx_snapshot.title,
                                "page_context": page_context,
                            })

                        batch_fetch_failures = (
                            candidate_fetch_failures - batch_fetch_failures_before
                        )
                        if not links_with_context:
                            self.reporter.action(
                                "batch_complete",
                                batch=f"{batch_idx + 1}/{len(batches)}",
                                saved=0,
                                enqueued=0,
                                fetch_failed=batch_fetch_failures,
                                queued=self.db.queued_count(),
                            )
                            continue

                        # Classify batch
                        try:
                            classification = self.llm_client.classify_links_batch(
                                snapshot=snapshot,
                                links_with_context=links_with_context,
                            )
                        except ContextWindowExceeded as e:
                            self.reporter.action("context_window_exceeded", batch=batch_idx, links=len(links_with_context), reason=str(e)[:500])
                            deferred_link = fetched_links.pop()
                            links_with_context.pop()
                            if not links_with_context:
                                self.reporter.action("batch_failed_context_window", reason="single link exceeds context window")
                                raise
                            batches.insert(batch_idx + 1, [deferred_link])
                            classification = self.llm_client.classify_links_batch(
                                snapshot=snapshot,
                                links_with_context=links_with_context,
                            )

                        # The model omits URLs; bind each result to its supplied context.
                        context_by_index = {
                            int(item["index"]): item for item in links_with_context
                        }
                        batch_candidates: list[JobMatch] = []
                        seen_classification_indexes: set[int] = set()
                        valid_classifications = []
                        for c in classification.link_classifications:
                            if (
                                c.index not in context_by_index
                                or c.index in seen_classification_indexes
                            ):
                                self.reporter.action(
                                    "link_classification_dropped",
                                    index=c.index,
                                    reason="invalid or duplicate index",
                                )
                                continue
                            seen_classification_indexes.add(c.index)
                            c.url = context_by_index[c.index]["url"]
                            valid_classifications.append(c)

                        if seen_classification_indexes != set(context_by_index):
                            raise ValueError(
                                "LLM classification did not cover every link in the batch"
                            )

                        for c in valid_classifications:
                            # Process each valid classification.
                            if c.type == "job_listing" and c.fit_score >= self.config.scoring.min_score_to_export:
                                if not all((c.title, c.company, c.location, c.url)):
                                    self.reporter.action(
                                        "link_classification_dropped",
                                        index=c.index,
                                        reason="job listing is missing required fields",
                                    )
                                    continue
                                batch_candidates.append(JobMatch(
                                    title=c.title,
                                    company=c.company,
                                    location=c.location,
                                    url=c.url,
                                    original_url=context_by_index[c.index]["original_url"],
                                    fit_score=c.fit_score,
                                    reason=c.reason,
                                    evidence=c.evidence,
                                ))
                            elif c.type == "explore" and self.config.exploration.enabled:
                                if c.url and self.db.enqueue(c.url, allow_visited=True):
                                    enqueued += 1

                            # Info-level log: url + type + fit
                            self.reporter.action(
                                "link_classified",
                                url=c.url,
                                type=c.type,
                                fit=c.fit_score,
                                reason=c.reason or "",
                                chars_truncated=max(0, len((context_by_index.get(c.index) or {}).get("page_context") or "") - self.config.crawler.max_page_context_chars),
                            )

                        # Clean candidates (blacklist + dedup) and save
                        cleaned = self._clean_jobs(batch_candidates)
                        page_saved = self.db.save_jobs(cleaned, current_source_key)
                        saved += page_saved
                        jobs_saved_total += page_saved

                        self.reporter.action(
                            "batch_complete",
                            batch=f"{batch_idx + 1}/{len(batches)}",
                            saved=page_saved,
                            enqueued=enqueued - batch_enqueued_before,
                            fetch_failed=batch_fetch_failures,
                            queued=self.db.queued_count(),
                        )

                        # Debug-level log: target page full text and LLM reasoning per link
                        if self.logger.isEnabledFor(logging.DEBUG):
                            self.logger.debug("=== target page: %s (url=%s) ===\n%s\n=== end target page ===",
                                              snapshot.title[:120], item_url, snapshot.text[:5000])
                            for c in classification.link_classifications:
                                lc = context_by_index.get(c.index)
                                page_ctx = (lc.get("page_context") or "") if lc else ""
                                self.logger.debug("classify index=%d url=%s type=%s fit=%d reason=%s\n--- page_context ---\n%s\n--- end page_context ---",
                                                  c.index, c.url, c.type, c.fit_score, (c.reason or ""), page_ctx)

                    page_status = "ok"
                    self.db.complete_backlog(url=item_url, final_url=final_url)
                    self.reporter.record_page(
                        status=page_status,
                        jobs_saved=saved,
                    )
                    self.reporter.action(
                        "page_complete",
                        saved=saved,
                        enqueued=enqueued,
                        fetch_failed=candidate_fetch_failures,
                        queued=self.db.queued_count(),
                        title=snapshot.title,
                    )
                    self.logger.debug(
                        "done jobs=%s queued=%s title=%r",
                        saved,
                        self.db.queued_count(),
                        snapshot.title[:120],
                    )

                except Exception as exc:
                    if isinstance(exc, BrowserFetchError):
                        page_status = exc.page_status
                        final_url = exc.final_url or snapshot.final_url or item_url
                    else:
                        page_status = f"error:{type(exc).__name__}"
                        final_url = snapshot.final_url or item_url
                    self.db.record_page(
                        url=item_url,
                        final_url=final_url,
                        status=page_status,
                    )
                    self.db.mark_backlog(item_url, "error")
                    self.reporter.action(
                        "page_failed",
                        status=page_status,
                        url=item_url,
                    )
                    self.reporter.record_page(
                        status=page_status,
                        jobs_saved=saved,
                    )

        self.reporter.action(
            "run_complete",
            jobs_saved_total=jobs_saved_total,
            queued=self.db.queued_count(),
        )
        self.reporter.run_summary(queued=self.db.queued_count())
        return 0

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
            if self._company_blacklisted(job):
                self.reporter.action("job_dropped_blacklist", company=job.company[:80], url=job.url[:220])
                continue
            if job.url in seen:
                self.reporter.action("job_dropped_dedup", url=job.url[:220])
                continue
            seen.add(job.url)
            cleaned.append(job)
        return cleaned

    def _company_blacklisted(self, job: JobMatch) -> bool:
        return matches_blacklisted_company(self.config, job.company, job.title, job.url, job.reason, job.evidence)

def main() -> int:
    loaded = load_config()
    return JobAgent(loaded).run()


if __name__ == "__main__":
    raise SystemExit(main())
