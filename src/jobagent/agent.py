from __future__ import annotations

import random
import re
import time
from pathlib import Path
from typing import Callable, Protocol

from .browser import BrowserSession
from .company_filters import match_blacklist_company, match_whitelist_company, whitelist_scope_active
from .config import LoadedConfig, ensure_data_dirs, load_config
from .db import Database
from .discover import (
    build_run_summary,
    enqueue_career_candidates,
    enqueue_follow_urls,
    enqueue_links,
    enqueue_query_suggestions,
    exploration_url_allowed,
    exploration_scope_allowed,
    seed_frontier,
    should_generate_queries,
)
from .extract import rank_candidate_links
from .heuristics import heuristic_jobs_from_page
from .llm import LLMResponseError, LocalLLMClient
from .location import evaluate_exploration_url_location, is_location_only_title
from .logging_utils import setup_logging
from .models import JobMatch, PageDecision, PageSnapshot, QuerySuggestion
from .prompts import PromptBook
from .reporting import ActionReporter
from .scoring import normalize_job_score
from .robots import RobotsCache
from .urltools import clean_url, denied_by_safety, domain_from_url, source_key


class BrowserLike(Protocol):
    def __enter__(self) -> "BrowserLike": ...
    def __exit__(self, exc_type, exc, tb) -> None: ...
    def fetch(self, url: str) -> PageSnapshot: ...


class LLMLike(Protocol):
    def analyze_page(
        self,
        snapshot: PageSnapshot,
        candidate_links,
        memory_summary: str,
    ) -> PageDecision: ...

    def generate_queries(self, memory_summary: str, run_summary: str) -> list[QuerySuggestion]: ...


class RobotsLike(Protocol):
    def allowed(self, url: str) -> bool: ...


class JobAgent:
    def __init__(
        self,
        loaded: LoadedConfig,
        db: Database | None = None,
        browser_factory: Callable[[], BrowserLike] | None = None,
        llm_client: LLMLike | None = None,
        robots: RobotsLike | None = None,
    ) -> None:
        self.loaded = loaded
        self.config = loaded.config
        self.paths = loaded.paths
        ensure_data_dirs(self.paths)
        self.logger = setup_logging(self.config, self.paths.log_path)
        self.db = db or Database(self.paths.database_path, self.config)
        self.browser_factory = browser_factory or (lambda: BrowserSession(self.config))
        self.robots = robots or RobotsCache(self.config)
        self.reporter = ActionReporter(self.config, self.logger)

        profile_text = self.paths.profile_path.read_text(encoding="utf-8").strip()
        prompt_book = PromptBook.from_file(self.paths.prompts_path)
        self.llm_client = llm_client or LocalLLMClient(self.config, prompt_book, profile_text)

    def run(self) -> int:
        self.reporter.action(
            "run_start",
            local_area=self.config.target.local_area,
            roles=", ".join(self.config.target.roles),
            max_pages=self.config.run.max_pages,
            max_depth=self.config.run.max_depth,
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

        self.db.apply_decay()
        self.reporter.action("apply_memory_decay")
        recalibrated = self.db.recalibrate_existing_jobs()
        if recalibrated["checked"] or recalibrated["adjusted"] or recalibrated["dropped"]:
            self.reporter.action(
                "recalibrate_existing_jobs",
                checked=recalibrated["checked"],
                adjusted=recalibrated["adjusted"],
                dropped=recalibrated["dropped"],
            )
        if self.config.run.reset_frontier_on_start:
            cleared = self.db.reset_frontier()
            self.reporter.action("reset_frontier", cleared=cleared)
        seeded = seed_frontier(self.config, self.db, self.paths.seeds_path)
        self.logger.info("seeded_frontier=%s queued=%s", seeded, self.db.queued_count())
        self.reporter.action("seed_frontier", added=seeded, queued=self.db.queued_count())

        pages_done = 0
        jobs_saved_total = 0
        generated_queries = 0

        with self.browser_factory() as browser:
            while pages_done < self.config.run.max_pages:
                item = self.db.pop_frontier()

                if item is None:
                    self.reporter.action("frontier_empty_try_query_generation", pages_done=pages_done, jobs_saved=jobs_saved_total)
                    added_queries = self._maybe_generate_queries(pages_done, jobs_saved_total, generated_queries)
                    generated_queries += added_queries
                    if added_queries > 0:
                        item = self.db.pop_frontier()
                    if item is None:
                        self.logger.info("frontier_empty pages_done=%s jobs_saved=%s", pages_done, jobs_saved_total)
                        self.reporter.action("frontier_empty_stop", pages_done=pages_done, jobs_saved=jobs_saved_total)
                        break

                if item.depth > self.config.run.max_depth:
                    self.db.mark_frontier(item.url, "skipped_depth")
                    self.reporter.action("skip_depth_limit", depth=item.depth, url=item.url)
                    continue

                if not self._frontier_item_allowed_by_mode(item):
                    self.db.mark_frontier(item.url, "skipped_scope")
                    self.reporter.action("skip_search_scope", mode=self.config.exploration.mode, url=item.url)
                    continue

                location_verdict = evaluate_exploration_url_location(item.url, item.reason, self.config)
                if not location_verdict.allowed:
                    self.db.mark_frontier(item.url, "skipped_location")
                    detail = location_verdict.reason
                    if location_verdict.matched_place and location_verdict.distance_km is not None:
                        detail += f": {location_verdict.matched_place} ({location_verdict.distance_km:.1f} km)"
                    self.reporter.action("skip_location_scope", reason=detail, url=item.url)
                    continue

                if self.db.was_visited(item.url):
                    self.db.mark_frontier(item.url, "skipped_visited")
                    self.reporter.action("skip_visited", url=item.url)
                    continue

                if self.db.source_visit_count(item.source_key) >= self.config.crawler.max_pages_per_source_key:
                    self.db.mark_frontier(item.url, "skipped_source_limit")
                    self.reporter.action("skip_source_limit", source_key=item.source_key, url=item.url)
                    continue

                source_domain = domain_from_url(item.url)
                self.db.ensure_source(item.source_key, source_domain)

                if not self.robots.allowed(item.url):
                    self._record_blocked(item)
                    pages_done += 1
                    self.db.mark_frontier(item.url, "blocked")
                    self.reporter.record_page(status="blocked_by_robots", queued=self.db.queued_count())
                    self._delay()
                    continue

                self.logger.debug("open depth=%s priority=%.2f url=%s", item.depth, item.priority, item.url)
                self.reporter.action(
                    "open_page",
                    depth=item.depth,
                    priority=f"{item.priority:.2f}",
                    source_key=item.source_key,
                    url=item.url,
                )
                page_status = "ok"
                snapshot = PageSnapshot(url=item.url, final_url=item.url, title="", text="", links=[])
                filtered_jobs: list[JobMatch] = []
                source_quality = 0
                source_notes = ""

                try:
                    snapshot = browser.fetch(item.url)
                    candidate_links = rank_candidate_links(snapshot, self.config)
                    self.reporter.action(
                        "page_fetched",
                        title=snapshot.title,
                        candidate_links=len(candidate_links),
                        final_url=snapshot.final_url,
                    )

                    heuristic_jobs: list[JobMatch] = []
                    early_decision = self._early_page_decision(snapshot)
                    llm_failed = False
                    llm_connection_failed = False

                    if early_decision is not None:
                        decision = early_decision
                        self.reporter.action("skip_llm_page", reason=decision.source_notes, title=snapshot.title)
                    else:
                        heuristic_jobs = heuristic_jobs_from_page(snapshot, candidate_links, self.config)
                        if heuristic_jobs:
                            self.reporter.action("heuristic_jobs", jobs=len(heuristic_jobs))

                        try:
                            decision = self.llm_client.analyze_page(
                                snapshot=snapshot,
                                candidate_links=candidate_links,
                                memory_summary=self.db.memory_summary(),
                            )
                        except Exception as llm_exc:
                            llm_failed = True
                            error_detail = (
                                llm_exc.compact()
                                if isinstance(llm_exc, LLMResponseError)
                                else f"{type(llm_exc).__name__}: {llm_exc}"
                            )
                            llm_connection_failed = self._looks_like_llm_connection_error(llm_exc, error_detail)
                            decision = PageDecision(
                                jobs=[],
                                follow_urls=[],
                                source_quality=45 if heuristic_jobs or candidate_links else 15,
                                source_notes=f"LLM page analysis failed; continued with configured fallback extraction: {error_detail[:700]}",
                            )
                            self.reporter.action(
                                "llm_page_analysis_failed_using_configured_fallback",
                                error=error_detail,
                                heuristic_jobs=len(heuristic_jobs),
                                candidate_links=len(candidate_links),
                            )

                            if llm_connection_failed and self.config.llm.stop_run_on_connection_error:
                                self.db.mark_frontier(item.url, "error")
                                self.db.update_source_memory(
                                    source_key=item.source_key,
                                    domain=source_domain,
                                    status="error:llm_connection",
                                    jobs_found=0,
                                    high_fit_jobs=0,
                                    source_quality=0,
                                    notes=error_detail[:1000],
                                )
                                self.db.record_page(
                                    url=item.url,
                                    final_url=snapshot.final_url,
                                    title=snapshot.title,
                                    source_key=item.source_key,
                                    depth=item.depth,
                                    status="error:llm_connection",
                                    jobs_found=0,
                                    high_fit_jobs=0,
                                    source_quality=0,
                                    discovered_from=item.discovered_from,
                                )
                                self.reporter.action(
                                    "llm_connection_error_stop",
                                    base_url=self.config.llm.base_url,
                                    url=item.url,
                                    reason=error_detail,
                                )
                                self._checkpoint_export("error")
                                pages_done += 1
                                break

                    source_quality = decision.source_quality
                    source_notes = decision.source_notes
                    allowed_job_urls = self._allowed_job_urls(snapshot, candidate_links)
                    filtered_jobs = self._clean_jobs(decision.jobs + heuristic_jobs, snapshot.final_url, allowed_job_urls)
                    high_fit_jobs = sum(
                        1
                        for job in filtered_jobs
                        if job.fit_score >= self.config.matching.high_fit_score
                    )
                    saved = self.db.save_jobs(filtered_jobs, snapshot.final_url, item.source_key)
                    jobs_saved_total += saved
                    self.reporter.action(
                        "page_analyzed",
                        jobs=len(filtered_jobs),
                        saved=saved,
                        high_fit=high_fit_jobs,
                        source_quality=source_quality,
                        source_notes=source_notes,
                    )

                    self.db.update_source_memory(
                        source_key=item.source_key,
                        domain=source_domain,
                        status="ok",
                        jobs_found=len(filtered_jobs),
                        high_fit_jobs=high_fit_jobs,
                        source_quality=source_quality,
                        notes=source_notes,
                    )
                    self.db.record_page(
                        url=item.url,
                        final_url=snapshot.final_url,
                        title=snapshot.title,
                        source_key=item.source_key,
                        depth=item.depth,
                        status="ok",
                        jobs_found=len(filtered_jobs),
                        high_fit_jobs=high_fit_jobs,
                        source_quality=source_quality,
                        discovered_from=item.discovered_from,
                    )

                    enqueued = 0
                    if item.depth < self.config.run.max_depth:
                        next_depth = item.depth + 1
                        enqueued = self._enqueue_exploration(
                            snapshot,
                            decision,
                            candidate_links,
                            next_depth,
                            llm_failed=llm_failed,
                        )
                        self.reporter.record_enqueued(enqueued)
                        self.reporter.action("enqueue_exploration", added=enqueued, next_depth=next_depth, queued=self.db.queued_count())

                    self.db.mark_frontier(item.url, "done")
                    self.reporter.record_page(
                        status="ok",
                        jobs_seen=len(filtered_jobs),
                        jobs_saved=saved,
                        high_fit_jobs=high_fit_jobs,
                        source_quality=source_quality,
                        queued=self.db.queued_count(),
                    )
                    self._checkpoint_export("page")
                    self.reporter.action(
                        "page_complete",
                        saved=saved,
                        kept_jobs=len(filtered_jobs),
                        enqueued=enqueued,
                        queued=self.db.queued_count(),
                        source_quality=source_quality,
                        title=snapshot.title,
                    )
                    self.logger.debug(
                        "done jobs=%s source_quality=%s queued=%s title=%r",
                        len(filtered_jobs),
                        source_quality,
                        self.db.queued_count(),
                        snapshot.title[:120],
                    )

                except Exception as exc:
                    page_status = f"error:{type(exc).__name__}"
                    self.db.update_source_memory(
                        source_key=item.source_key,
                        domain=source_domain,
                        status=page_status,
                        jobs_found=0,
                        high_fit_jobs=0,
                        source_quality=0,
                        notes=str(exc)[:1000],
                    )
                    self.db.record_page(
                        url=item.url,
                        final_url=snapshot.final_url,
                        title=snapshot.title,
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

                pages_done += 1

                if should_generate_queries(pages_done, generated_queries, self.config):
                    remaining = self.config.exploration.max_generated_queries_per_run - generated_queries
                    added = self._generate_queries(pages_done, jobs_saved_total, remaining)
                    generated_queries += added

                self._delay()

        self._checkpoint_export("run_complete", force=True)

        self.reporter.action(
            "run_complete",
            pages_done=pages_done,
            jobs_saved_total=jobs_saved_total,
            queued=self.db.queued_count(),
        )
        self.reporter.maybe_summary(queued=self.db.queued_count(), force=True)
        return 0

    def _early_page_decision(self, snapshot: PageSnapshot) -> PageDecision | None:
        """Skip expensive LLM calls on pages that cannot contain savable jobs.

        This is deliberately conservative. It handles obvious zero-result search
        pages and empty/blocked ATS shells; normal career listings still go to
        the LLM so it can choose follow URLs.
        """
        final_url = snapshot.final_url or snapshot.url
        title = (snapshot.title or "").strip()
        title_low = title.casefold()
        url_low = final_url.casefold()
        text_len = len((snapshot.text or "").strip())

        if "linkedin.com/jobs/search" in url_low and re.match(r"^\s*0(\s|$)", title_low):
            return PageDecision(
                jobs=[],
                follow_urls=[],
                source_quality=0,
                source_notes="zero-result LinkedIn job-search page; skipped LLM and follow-up expansion",
            )

        if text_len < self.config.crawler.min_body_chars_to_analyze and not title and not snapshot.links and not snapshot.structured_jobs:
            return PageDecision(
                jobs=[],
                follow_urls=[],
                source_quality=5,
                source_notes="empty or script-only page with no links; skipped LLM",
            )

        return None


    def _llm_health_check(self) -> tuple[bool, str]:
        health = getattr(self.llm_client, "health_check", None)
        if health is None:
            # Tests and custom clients may not expose a health check. Treat them
            # as available rather than forcing a specific protocol.
            return True, "custom LLM client has no health_check method"
        try:
            result = health()
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if isinstance(result, tuple) and len(result) == 2:
            return bool(result[0]), str(result[1])
        return bool(result), "ok" if result else "health_check returned false"

    def _looks_like_llm_connection_error(self, exc: Exception, detail: str) -> bool:
        name = type(exc).__name__.casefold()
        text = f"{name}\n{detail}".casefold()
        return any(
            marker in text
            for marker in (
                "connectionerror",
                "newconnectionerror",
                "failed to establish a new connection",
                "connection refused",
                "connection refu",
                "max retries exceeded",
            )
        )

    def _record_blocked(self, item) -> None:
        domain = domain_from_url(item.url)
        self.db.update_source_memory(
            source_key=item.source_key,
            domain=domain,
            status="blocked_by_robots",
            jobs_found=0,
            high_fit_jobs=0,
            source_quality=0,
            notes="blocked by robots.txt",
        )
        self.db.record_page(
            url=item.url,
            final_url=item.url,
            title="",
            source_key=item.source_key,
            depth=item.depth,
            status="blocked_by_robots",
            jobs_found=0,
            high_fit_jobs=0,
            source_quality=0,
            discovered_from=item.discovered_from,
        )
        self.logger.info("blocked_by_robots url=%s", item.url)
        self.reporter.action("blocked_by_robots", source_key=item.source_key, url=item.url)

    def _clean_jobs(self, jobs: list[JobMatch], base_url: str, allowed_job_urls: set[str] | None = None) -> list[JobMatch]:
        cleaned: list[JobMatch] = []
        seen: set[str] = set()
        score_guard_dropped = 0
        score_guard_adjusted = 0
        validation_dropped = 0
        validation_reasons: dict[str, int] = {}

        for job in jobs:
            url = clean_url(job.url, base_url, self.config)
            if not url or url in seen:
                continue
            if denied_by_safety(url, job.title, self.config):
                validation_dropped += 1
                validation_reasons["safety"] = validation_reasons.get("safety", 0) + 1
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
                score_source=(job.score_source or "llm").strip(),
                score_basis=job.score_basis.strip(),
            )

            invalid_reason = self._job_validation_failure(prepared, base_url, allowed_job_urls or set())
            if invalid_reason:
                validation_dropped += 1
                validation_reasons[invalid_reason] = validation_reasons.get(invalid_reason, 0) + 1
                continue

            normalized = normalize_job_score(prepared, self.config)
            if normalized is None:
                score_guard_dropped += 1
                continue
            if normalized.fit_score != prepared.fit_score or normalized.score_source != prepared.score_source:
                score_guard_adjusted += 1
            seen.add(url)
            cleaned.append(normalized)

        if validation_dropped:
            self.reporter.action(
                "job_validation_guard",
                dropped=validation_dropped,
                reasons=", ".join(f"{k}:{v}" for k, v in sorted(validation_reasons.items())),
                remaining=len(cleaned),
            )

        if score_guard_dropped or score_guard_adjusted:
            self.reporter.action(
                "score_guard",
                dropped=score_guard_dropped,
                adjusted=score_guard_adjusted,
                remaining=len(cleaned),
            )

        return cleaned

    def _allowed_job_urls(self, snapshot: PageSnapshot, candidate_links) -> set[str]:
        allowed: set[str] = set()
        final_clean = clean_url(snapshot.final_url, snapshot.final_url, self.config)
        requested_clean = clean_url(snapshot.url, snapshot.final_url, self.config)

        if self.config.job_validation.require_loaded_job_detail_page:
            # CSV rows must refer to the page that was actually loaded and analyzed.
            # Candidate links from overview pages are only exploration targets.
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

    def _company_whitelisted(self, job: JobMatch) -> bool:
        return match_whitelist_company(self.config, job.company, job.title, job.url, job.reason, job.evidence) is not None

    def _frontier_item_allowed_by_mode(self, item) -> bool:
        # Apply both whitelist scope and role-focus rules at pop time as a
        # second line of defense. This prevents stale queue rows from older
        # versions/modes from being opened after a restart.
        return exploration_scope_allowed(item.url, item.reason, self.config)

    def _current_page_job_url_looks_specific(self, job: JobMatch) -> bool:
        if self._matches_job_validation_pattern(self.config.job_validation.index_url_patterns, job.url):
            return False
        if self._matches_job_validation_pattern(self.config.heuristic_extraction.detail_url_positive_patterns, job.url):
            return True

        # Some career systems use clean slugs without numeric IDs. Allow those only
        # when the URL slug itself contains enough of the title. This rejects
        # generic current-page listings such as /jobs/procurement/muenchen.
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
        # Put back generic but meaningful role tokens if the title is short.
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

        if cfg.drop_if_title_is_location and is_location_only_title(job.title, self.config):
            return "title_is_location"

        if cfg.drop_if_company_blacklisted and self._company_blacklisted(job):
            return "company_blacklist"

        if whitelist_scope_active(self.config) and not self._company_whitelisted(job):
            return "company_not_whitelisted"

        if cfg.require_loaded_job_detail_page:
            if not is_current_page_url:
                return "not_loaded_detail_page"
            if not self._current_page_job_url_looks_specific(job):
                return "current_page_url_not_specific"

        if cfg.current_page_url_must_look_like_detail and is_current_page_url:
            if not self._current_page_job_url_looks_specific(job):
                return "current_page_url_not_specific"

        if cfg.drop_if_url_is_index_page:
            if self._matches_job_validation_pattern(cfg.index_url_patterns, job.url):
                return "index_url"
            if self._matches_job_validation_pattern(cfg.index_title_patterns, job.title) and is_current_page_url:
                return "index_title"

        if cfg.enforce_llm_urls_from_page and job.score_source.startswith("llm"):
            if job.url not in allowed_job_urls:
                return "url_not_from_page"
            if not cfg.allow_current_page_as_job_url and is_current_page_url:
                return "current_page_url_not_allowed"

        return None

    def _enqueue_exploration(
        self,
        snapshot: PageSnapshot,
        decision: PageDecision,
        candidate_links,
        next_depth: int,
        *,
        llm_failed: bool = False,
    ) -> int:
        count = 0
        if llm_failed:
            # Without LLM judgement, do not expand every visible link from a
            # company ATS page. This prevents one temporary model outage from
            # filling the queue with irrelevant jobs.
            limit = self.config.exploration.max_follow_urls_without_llm
            if limit <= 0:
                return 0
            candidate_links = candidate_links[:limit]
            follow_urls = decision.follow_urls[:limit]
        else:
            follow_urls = decision.follow_urls

        count += enqueue_follow_urls(follow_urls, snapshot.final_url, next_depth, self.config, self.db, candidate_links)

        limit = self.config.exploration.candidate_url_limit_per_search_page
        count += enqueue_links(candidate_links[:limit], snapshot.final_url, next_depth, self.config, self.db)

        expansion_limit = self.config.crawler.max_career_domain_expansions_per_page
        if expansion_limit <= 0:
            return count

        expanded_domains: set[str] = set()
        for link in candidate_links:
            domain = domain_from_url(link.url)
            if domain in expanded_domains:
                continue
            if len(expanded_domains) >= expansion_limit:
                break
            expanded_domains.add(domain)
            count += enqueue_career_candidates(link.url, snapshot.final_url, next_depth, self.config, self.db)
        return count

    def _generate_queries(self, pages_done: int, jobs_saved: int, remaining: int) -> int:
        if remaining <= 0:
            return 0
        memory_summary = self.db.memory_summary()
        run_summary = build_run_summary(pages_done, jobs_saved, self.db.queued_count())
        suggestions = self.llm_client.generate_queries(memory_summary, run_summary)[:remaining]
        added = enqueue_query_suggestions(suggestions, self.config, self.db)
        self.reporter.record_generated_queries(len(suggestions))
        self.reporter.record_enqueued(added)
        self.reporter.action("generate_queries", suggestions=len(suggestions), enqueued=added, queued=self.db.queued_count())
        self.logger.info("generated_queries=%s queued=%s", added, self.db.queued_count())
        return len(suggestions)

    def _maybe_generate_queries(self, pages_done: int, jobs_saved: int, generated_queries: int) -> int:
        if not self.config.exploration.enabled:
            return 0
        remaining = self.config.exploration.max_generated_queries_per_run - generated_queries
        if remaining <= 0:
            return 0
        before = self.db.queued_count()
        added = self._generate_queries(pages_done, jobs_saved, remaining)
        if self.db.queued_count() <= before:
            return 0
        return added


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
