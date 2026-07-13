from __future__ import annotations

import random
import re
import time
from pathlib import Path
from typing import Callable, Protocol

from .browser import BrowserSession
from .company_filters import match_blacklist_company
from .config import LoadedConfig, ensure_data_dirs, load_config
from .db import Database
from .discover import (
    build_run_summary,
    enqueue_career_candidates,
    seed_frontier,
)
from .extract import compact_text, rank_candidate_links, page_decision_from_dict
from .llm import ContextWindowExceeded, LLMResponseError, LocalLLMClient
from .logging_utils import setup_logging
from .models import JobMatch, LinkCandidate, LinkClassification, PageDecision, PageSnapshot
from .prompts import PromptBook
from .reporting import ActionReporter
from .urltools import clean_url, denied_by_safety, domain_from_url, source_key


class BrowserLike(Protocol):
    def __enter__(self) -> "BrowserLike": ...
    def __exit__(self, exc_type, exc, tb) -> None: ...
    def fetch(self, url: str) -> PageSnapshot: ...


class LLMLike(Protocol):
    def analyze_page(
        self,
        snapshot: PageSnapshot,
        links_with_context: list[dict[str, str]],
        memory_summary: str,
    ) -> PageDecision: ...
    def classify_links_batch(
        self,
        snapshot: PageSnapshot,
        links_with_context: list[dict[str, str]],
        memory_summary: str,
    ) -> PageDecision: ...



class JobAgent:
    def __init__(
        self,
        loaded: LoadedConfig,
        db: Database | None = None,
        browser_factory: Callable[[], BrowserLike] | None = None,
        llm_client: LLMLike | None = None,
    ) -> None:
        self.loaded = loaded
        self.config = loaded.config
        self.paths = loaded.paths
        ensure_data_dirs(self.paths)
        self.logger = setup_logging(self.config, self.paths.log_path)
        self.db = db or Database(self.paths.database_path, self.config)
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
                self._checkpoint_export("run_complete", force=True)
                return 2
            self.reporter.action("llm_available", base_url=self.config.llm.base_url)

        if self.config.run.reset_frontier_on_start:
            cleared = self.db.reset_frontier()
            self.reporter.action("reset_frontier", cleared=cleared)
        seeded = seed_frontier(self.config, self.db, self.paths.seeds_path)
        self.logger.info("seeded_frontier=%s queued=%s", seeded, self.db.queued_count())
        self.reporter.action("seed_frontier", added=seeded, queued=self.db.queued_count())

        jobs_saved_total = 0

        with self.browser_factory() as browser:
            while True:
                item = self.db.pop_frontier()

                if item is None:
                    self.reporter.action("frontier_empty_stop", pages_done=self._pages_done_count(), jobs_saved=jobs_saved_total)
                    self.logger.info("frontier_empty jobs_saved=%s", jobs_saved_total)
                    break

                if self.db.was_visited(item.url):
                    self.db.mark_frontier(item.url, "skipped_visited")
                    self.reporter.action("skip_visited", url=item.url)
                    continue

                source_limit = self.config.crawler.max_pages_per_source_key
                if self.db.source_visit_count(item.source_key) >= source_limit:
                    self.db.mark_frontier(item.url, "skipped_source_limit")
                    self.reporter.action("skip_source_limit", source_key=item.source_key, url=item.url)
                    continue

                source_domain = domain_from_url(item.url)
                self.db.ensure_source(item.source_key, source_domain)

                self.logger.debug("open depth=%s url=%s", item.depth, item.url)
                self.reporter.action(
                    "open_page",
                    depth=item.depth,
                    source_key=item.source_key,
                    url=item.url,
                )
                page_status = "ok"
                snapshot = PageSnapshot(url=item.url, final_url=item.url, title="", text="", links=[])
                source_quality = 0
                source_notes = ""

                try:
                    snapshot = browser.fetch(item.url)
                    final_url = snapshot.final_url or snapshot.url
                    candidate_links = rank_candidate_links(snapshot, self.config)
                    self.reporter.action(
                        "page_fetched",
                        title=snapshot.title,
                        candidate_links=len(candidate_links),
                        final_url=snapshot.final_url,
                    )

                    # Single-stage: classify all candidate links with fetched page_context
                    saved = 0
                    enqueued = 0
                    source_quality = 0
                    source_notes = ""
                    last_filtered_count = 0
                    last_high_fit = 0
                    next_depth = item.depth + 1
                    batch_size = self.config.crawler.batch_size_for_llm

                    # Build batches
                    batches: list[list[LinkCandidate]] = []
                    for i in range(0, len(candidate_links), batch_size):
                        batches.append(candidate_links[i:i + batch_size])

                    for batch_idx, batch in enumerate(batches):
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
                            self.logger.warning("context_window_exceeded batch=%s links=%s: %s", batch_idx, len(links_with_context), e)
                            links_with_context.pop()
                            if not links_with_context:
                                self.logger.warning("batch dropped entirely due to context window, skipping")
                                continue
                            classification = self.llm_client.classify_links_batch(
                                snapshot=snapshot,
                                links_with_context=links_with_context,
                                memory_summary="",
                            )

                        if classification is None:
                            continue

                        source_quality = classification.source_quality
                        source_notes = classification.source_notes

                        # The LLM prompt (prompts.yaml:58) tells the model to omit URLs.
                        # Inject them from links_with_context using the classification index.
                        ctx_by_index = {int(item["index"]): item["url"] for item in links_with_context}
                        for c in classification.link_classifications:
                            if not c.url and c.index in ctx_by_index:
                                c.url = ctx_by_index[c.index]

                        # Process each classification
                        for c in classification.link_classifications:
                            if c.type == "job_listing" and c.fit_score >= self.config.scoring.min_score_to_export:
                                job = JobMatch(
                                    title=c.title,
                                    company=c.company,
                                    location=c.location,
                                    url=c.url,
                                    fit_score=c.fit_score,
                                    reason=c.reason,
                                    evidence=c.evidence,
                                    posting_language="",
                                )
                                page_saved = self.db.save_jobs([job], c.url, item.source_key)
                                saved += page_saved
                                last_filtered_count = max(last_filtered_count, page_saved)
                                high_fit = 1 if c.fit_score >= self.config.scoring.high_fit_score_threshold else 0
                                last_high_fit = max(last_high_fit, high_fit)
                                self.db.record_page(
                                    url=c.url,
                                    final_url=c.url,
                                    title=c.title,
                                    source_key=item.source_key,
                                    depth=next_depth,
                                    status="ok",
                                    jobs_found=page_saved,
                                    high_fit_jobs=high_fit,
                                    source_quality=classification.source_quality,
                                    discovered_from=c.url,
                                )
                            elif c.type == "explore":
                                frontier_item = self.db._make_frontier_item(
                                    url=c.url,
                                    depth=next_depth,
                                    discovered_from=item.url,
                                    reason=f"LLM explore (type=explore)",
                                    config=self.config,
                                )
                                if self.db.enqueue(frontier_item):
                                    enqueued += 1

                            # Info-level log: url + type + fit
                            self.logger.info(
                                "url=%s type=%s fit=%d",
                                c.url[:220],
                                c.type,
                                c.fit_score,
                            )

                        # Debug-level log: page_context preview
                        if self.config.run.debug_mode:
                            for lc in links_with_context:
                                preview = (lc["page_context"] or "")[:500]
                                self.logger.debug("link %d url=%s context_preview=%s", lc["index"], lc["url"][:120], preview[:200])

                        # Debug-level log: target page full text and LLM reasoning per link
                        if self.config.run.debug_mode:
                            self.logger.debug("=== target page: %s (url=%s) ===\n%s\n=== end target page ===",
                                              snapshot.title[:120], item.url, snapshot.text[:5000])
                            for c in classification.link_classifications:
                                self.logger.debug("classify index=%d url=%s type=%s fit=%d reason=%s",
                                                  c.index, c.url[:120], c.type, c.fit_score, (c.reason or "")[:300])

                    jobs_saved_total += saved
                    self.reporter.action(
                        "page_analyzed",
                        jobs=last_filtered_count,
                        saved=saved,
                        high_fit=last_high_fit,
                        source_quality=source_quality,
                        source_notes=source_notes,
                    )

                    self.db.record_page(
                        url=item.url,
                        final_url=final_url,
                        title=snapshot.title,
                        source_key=item.source_key,
                        depth=item.depth,
                        status="ok",
                        jobs_found=last_filtered_count,
                        high_fit_jobs=last_high_fit,
                        source_quality=source_quality,
                        discovered_from=item.discovered_from,
                    )

                    # Career domain expansion
                    expansion_limit = self.config.crawler.max_career_domain_expansions_per_page
                    if expansion_limit > 0:
                        expanded_domains: set[str] = set()
                        for link in candidate_links:
                            domain = domain_from_url(link.url)
                            if domain in expanded_domains:
                                continue
                            if len(expanded_domains) >= expansion_limit:
                                break
                            expanded_domains.add(domain)
                            enqueued += enqueue_career_candidates(link.url, snapshot.final_url, next_depth, self.config, self.db)

                    self.db.mark_frontier(item.url, "done")
                    self.reporter.record_page(
                        status="ok",
                        jobs_seen=last_filtered_count,
                        jobs_saved=saved,
                        high_fit_jobs=last_high_fit,
                        source_quality=source_quality,
                        queued=self.db.queued_count(),
                    )
                    self._checkpoint_export("page")
                    self.reporter.action(
                        "page_complete",
                        saved=saved,
                        enqueued=enqueued,
                        queued=self.db.queued_count(),
                        source_quality=source_quality,
                        title=snapshot.title,
                    )
                    self.logger.debug(
                        "done jobs=%s source_quality=%s queued=%s title=%r",
                        last_filtered_count,
                        source_quality,
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
                    self.db.mark_frontier(item.url, "error")
                    self.reporter.action("page_failed", status=page_status, url=item.url, reason=str(exc)[:500])
                    self.reporter.record_page(status=page_status, queued=self.db.queued_count())
                    self._checkpoint_export("error")
                    self.logger.exception("page_failed url=%s status=%s", item.url, page_status)

                self._delay()

        self._checkpoint_export("run_complete", force=True)

        self.reporter.action(
            "run_complete",
            jobs_saved_total=jobs_saved_total,
            queued=self.db.queued_count(),
        )
        self.reporter.maybe_summary(queued=self.db.queued_count(), force=True)
        return 0

    def _pages_done_count(self) -> int:
        """Count completed (non-queued) frontier items."""
        return self.db.count_rows("frontier") - self.db.queued_count()

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

    def _clean_jobs(self, jobs: list[JobMatch], base_url: str, allowed_job_urls: set[str] | None = None) -> list[JobMatch]:
        cleaned: list[JobMatch] = []
        seen: set[str] = set()
        score_guard_dropped = 0
        validation_dropped = 0
        validation_reasons: dict[str, int] = {}

        for job in jobs:
            url = clean_url(job.url, base_url, self.config)
            if not url or url in seen:
                continue
            if denied_by_safety(url, job.title, self.config):
                validation_dropped += 1
                validation_reasons["safety"] = validation_dropped
                continue
            if not job.title.strip():
                continue

            prepared = JobMatch(
                title=job.title.strip(),
                company=job.company.strip(),
                location=job.location.strip(),
                url=url,
                fit_score=job.fit_score,
                reason=job.reason.strip(),
                evidence=job.evidence.strip(),
                posting_language=job.posting_language.strip(),
            )

            # Inlined min-score gate
            score = max(0, min(100, int(prepared.fit_score)))
            if score < self.config.scoring.min_score_to_export:
                score_guard_dropped += 1
                continue

            invalid_reason = self._job_validation_failure(prepared, base_url, allowed_job_urls or set())
            if invalid_reason:
                validation_dropped += 1
                validation_reasons[invalid_reason] = validation_reasons.get(invalid_reason, 0) + 1
                continue

            seen.add(url)
            cleaned.append(JobMatch(
                title=prepared.title,
                company=prepared.company,
                location=prepared.location,
                url=url,
                fit_score=score,
                reason=prepared.reason,
                evidence=prepared.evidence,
                posting_language=prepared.posting_language,
            ))

        if validation_dropped:
            self.reporter.action(
                "job_validation_guard",
                dropped=validation_dropped,
                reasons=", ".join(f"{k}:{v}" for k, v in sorted(validation_reasons.items())),
                remaining=len(cleaned),
            )

        if score_guard_dropped:
            self.reporter.action(
                "score_guard_dropped",
                dropped=score_guard_dropped,
                remaining=len(cleaned),
            )

        return cleaned

    def _allowed_job_urls(self, snapshot: PageSnapshot, candidate_links) -> set[str]:
        allowed: set[str] = set()
        final_clean = clean_url(snapshot.final_url, snapshot.final_url, self.config)
        requested_clean = clean_url(snapshot.url, snapshot.final_url, self.config)

        if self.config.job_validation.require_loaded_job_detail_page:
            return {u for u in [final_clean, requested_clean] if u}

        for raw in [snapshot.url, snapshot.final_url]:
            cleaned = clean_url(raw, snapshot.final_url, self.config)
            if cleaned:
                allowed.add(cleaned)
        for link in candidate_links:
            cleaned = clean_url(link.url, snapshot.final_url, self.config)
            if cleaned:
                allowed.add(cleaned)
        for item in snapshot.structured_jobs:
            if not isinstance(item, dict):
                continue
            cleaned = clean_url(str(item.get("url") or ""), snapshot.final_url, self.config)
            if cleaned:
                allowed.add(cleaned)
        return allowed

    def _matches_job_validation_pattern(self, patterns: list[str], *values: str) -> bool:
        for pattern in patterns:
            try:
                if any(re.search(pattern, value or "") for value in values):
                    return True
            except re.error:
                continue
        return False

    def _contains_company_name(self, value: str, company: str) -> bool:
        if not value or not company:
            return False
        pattern = r"(?<![\w])" + re.escape(company.strip()) + r"(?![\w])"
        return re.search(pattern, value, flags=re.I) is not None

    def _company_blacklisted(self, job: JobMatch) -> bool:
        return match_blacklist_company(self.config, job.company, job.title, job.url, job.reason, job.evidence) is not None

    def _current_page_job_url_looks_specific(self, job: JobMatch) -> bool:
        if self._matches_job_validation_pattern(self.config.job_validation.index_url_patterns, job.url):
            return False

        def normalize(value: str) -> str:
            value = value.casefold()
            value = value.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
            return value

        url_text = normalize(job.url)
        title_tokens = [
            token
            for token in re.findall(r"[a-zA-ZäöüÄÖÜß]{4,}", normalize(job.title))
            if token not in {"manager", "senior", "junior", "lead", "jobs", "stellen", "muenchen", "munich", "germany", "deutschland"}
        ]
        if len(title_tokens) < 2:
            title_tokens.extend(
                token
                for token in re.findall(r"[a-zA-ZäöüÄÖÜß]{4,}", normalize(job.title))
                if token not in {"jobs", "stellen", "muenchen", "munich", "germany", "deutschland"}
            )
        title_tokens = list(dict.fromkeys(title_tokens))
        matching_title_tokens = sum(1 for token in title_tokens if token and token in url_text)
        if len(title_tokens) >= 2 and matching_title_tokens >= 2:
            return True

        return False

    def _job_validation_failure(self, job: JobMatch, base_url: str, allowed_job_urls: set[str]) -> str | None:
        cfg = self.config.job_validation
        if not cfg.enabled:
            return None

        base_clean = clean_url(base_url, None, self.config) or base_url
        is_current_page_url = job.url == base_clean

        combined_text = "\n".join([job.title, job.company, job.location, job.reason, job.evidence, job.url])
        if self._matches_job_validation_pattern(cfg.drop_if_title_or_url_matches, combined_text):
            return "initiative_or_non_posting"

        if cfg.drop_if_company_blacklisted and self._company_blacklisted(job):
            return "company_blacklist"

        if cfg.drop_if_url_is_index_page:
            if self._matches_job_validation_pattern(cfg.index_url_patterns, job.url):
                return "index_url"
            if self._matches_job_validation_pattern(cfg.index_title_patterns, job.title) and is_current_page_url:
                return "index_title"

        if cfg.enforce_llm_urls_from_page:
            if job.url not in allowed_job_urls:
                return "url_not_from_page"
            if not cfg.allow_current_page_as_job_url and is_current_page_url:
                return "current_page_url_not_allowed"

        return None

    def _checkpoint_export(self, reason: str, force: bool = False) -> None:
        should_export = force and self.config.run.export_after_run
        should_export = should_export or (reason in {"page", "error"} and self.config.run.export_after_each_page)
        should_export = should_export or (reason == "interrupt" and self.config.run.export_on_interrupt)
        if not should_export:
            return
        self.db.export_csv(self.paths.csv_export_path)
        self.db.export_jsonl(self.paths.jsonl_export_path)
        self.logger.debug(
            "exported reason=%s csv=%s jsonl=%s",
            reason,
            self.paths.csv_export_path,
            self.paths.jsonl_export_path,
        )
        self.reporter.action("export_results", reason=reason, csv=self.paths.csv_export_path, jsonl=self.paths.jsonl_export_path)

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
