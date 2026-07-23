from __future__ import annotations

import logging
from typing import Callable, Protocol

from .browser import BrowserFetchError, BrowserSession
from .company_filters import (
    FUZZY_MATCH_THRESHOLD,
    company_similarity,
    matches_blacklisted_company,
    text_similarity,
)
from .config import LoadedConfig, ensure_data_dirs, load_config
from .db import Database
from .discover import seed_backlog
from .extract import compact_text
from .llm import LocalLLMClient
from .logging_utils import setup_logging
from .models import JobMatch, PageDecision, PageSnapshot
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
        if self.config.run.reset_pages_on_start:
            cleared = self.db.reset_pages()
            self.reporter.action("reset_pages", cleared=cleared)
        elif self.config.crawler.retry_error_pages:
            cleared = self.db.reset_retryable_page_errors()
            self.reporter.action("reset_page_errors", cleared=cleared)
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
                    candidate_idx = 0
                    batch_idx = 0

                    while candidate_idx < len(candidate_links):
                        batch_idx += 1
                        batch_enqueued_before = enqueued
                        self.reporter.action(
                            "batch_start",
                            batch=batch_idx,
                            total_links=len(candidate_links),
                            url=item_url,
                        )
                        # Rejected candidates do not consume an LLM batch slot.
                        links_with_context: list[dict[str, str]] = []
                        batch_fetch_failures_before = candidate_fetch_failures
                        while (
                            candidate_idx < len(candidate_links)
                            and len(links_with_context) < batch_size
                        ):
                            link = candidate_links[candidate_idx]
                            candidate_idx += 1
                            if self.db.page_status(link.url) is not None:
                                self.reporter.action(
                                    "candidate_url_dropped",
                                    url=link.url,
                                    reason="already present in pages",
                                )
                                continue
                            try:
                                ctx_snapshot = browser.fetch(link.url)
                                page_context = compact_text(ctx_snapshot.text, self.config)
                            except BrowserFetchError as exc:
                                candidate_fetch_failures += 1
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
                                self.db.record_page(
                                    url=link.url,
                                    final_url=link.url,
                                    status=f"error:{type(exc).__name__}",
                                )
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
                            if candidate_final_url != link.url:
                                if self.db.page_status(candidate_final_url) is not None:
                                    self.reporter.action(
                                        "candidate_url_dropped",
                                        url=candidate_final_url,
                                        reason="final URL already present in pages",
                                    )
                                    continue
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
                                batch=batch_idx,
                                saved=0,
                                enqueued=0,
                                fetch_failed=batch_fetch_failures,
                                queued=self.db.queued_count(),
                            )
                            continue

                        # Classify batch
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
                            if c.type in {"job_listing", "skip"}:
                                context = context_by_index[c.index]
                                self.db.record_page(
                                    url=context["original_url"],
                                    final_url=c.url or context["url"],
                                    status=c.type,
                                )
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
                            elif (
                                c.type == "explore"
                                and self.config.exploration.enabled
                                and c.fit_score >= self.config.scoring.min_score_to_explore
                            ):
                                if c.url and self.db.enqueue(c.url, rating=c.fit_score):
                                    enqueued += 1

                            # Info-level log: url + type + fit
                            self.reporter.action(
                                "link_classified",
                                url=c.url,
                                type=c.type,
                                fit=c.fit_score,
                                reason=c.reason or "",
                            )

                        page_saved = self._clean_and_save_jobs(
                            batch_candidates,
                            current_source_key,
                        )
                        saved += page_saved
                        jobs_saved_total += page_saved

                        self.reporter.action(
                            "batch_complete",
                            batch=batch_idx,
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

    def _clean_and_save_jobs(
        self,
        jobs: list[JobMatch],
        current_source_key: str,
    ) -> int:
        existing = self.db.jobs_for_dedup()
        existing_by_url = {str(row["url"]): row for row in existing}
        existing_order = {
            str(row["url"]): index for index, row in enumerate(existing)
        }
        prepared: list[tuple[JobMatch, set[str]]] = []

        for job in jobs:
            if self._company_blacklisted(job):
                self.reporter.action("job_dropped_blacklist", company=job.company[:80], url=job.url[:220])
                continue

            existing_matches: set[str] = set()
            for row in existing:
                row_url = str(row["url"])
                title_score = text_similarity(job.title, str(row["title"]))
                company_score = company_similarity(job.company, str(row["company"]))
                location_score = text_similarity(job.location, str(row["location"]))
                if row_url == job.url or (
                    title_score >= FUZZY_MATCH_THRESHOLD
                    and company_score >= FUZZY_MATCH_THRESHOLD
                    and location_score >= FUZZY_MATCH_THRESHOLD
                ):
                    existing_matches.add(row_url)

            if existing_matches:
                canonical_url = min(
                    existing_matches,
                    key=existing_order.__getitem__,
                )
                canonical = existing_by_url[canonical_url]
                self.reporter.action(
                    "job_matched_existing",
                    match="url" if job.url in existing_matches else "fuzzy",
                    url=job.original_url[:220],
                    existing_url=canonical_url[:220],
                    matched_rows=len(existing_matches),
                    title_similarity=f"{text_similarity(job.title, str(canonical['title'])):.2f}",
                    company_similarity=f"{company_similarity(job.company, str(canonical['company'])):.2f}",
                    location_similarity=f"{text_similarity(job.location, str(canonical['location'])):.2f}",
                )

            batch_matches: list[int] = []
            batch_scores = (0.0, 0.0, 0.0)
            batch_exact_match = False
            for index, (candidate, candidate_existing) in enumerate(prepared):
                candidate_title_score = text_similarity(job.title, candidate.title)
                candidate_company_score = company_similarity(job.company, candidate.company)
                candidate_location_score = text_similarity(job.location, candidate.location)
                if job.url == candidate.url or existing_matches.intersection(candidate_existing) or (
                    candidate_title_score >= FUZZY_MATCH_THRESHOLD
                    and candidate_company_score >= FUZZY_MATCH_THRESHOLD
                    and candidate_location_score >= FUZZY_MATCH_THRESHOLD
                ):
                    batch_matches.append(index)
                    batch_exact_match = batch_exact_match or job.url == candidate.url
                    batch_scores = (
                        candidate_title_score,
                        candidate_company_score,
                        candidate_location_score,
                    )

            if batch_matches:
                target_index = batch_matches[0]
                for index in reversed(batch_matches):
                    existing_matches.update(prepared[index][1])
                    if index != target_index:
                        prepared.pop(index)
                prepared[target_index] = (job, existing_matches)
                title_score, company_score, location_score = batch_scores
                self.reporter.action(
                    "job_matched_batch",
                    match="url" if batch_exact_match else "fuzzy",
                    url=job.original_url[:220],
                    title_similarity=f"{title_score:.2f}",
                    company_similarity=f"{company_score:.2f}",
                    location_similarity=f"{location_score:.2f}",
                )
                continue

            prepared.append((job, existing_matches))

        cleaned = [job for job, _ in prepared]
        dedup_matches = {
            job.url: tuple(sorted(urls, key=existing_order.__getitem__))
            for job, urls in prepared
            if urls
        }
        return self.db.save_jobs(
            cleaned,
            current_source_key,
            dedup_matches=dedup_matches,
        )

    def _company_blacklisted(self, job: JobMatch) -> bool:
        return matches_blacklisted_company(self.config, job.company, job.title, job.url, job.reason, job.evidence)

def main() -> int:
    loaded = load_config()
    return JobAgent(loaded).run()


if __name__ == "__main__":
    raise SystemExit(main())
