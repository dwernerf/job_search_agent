from __future__ import annotations

import random
import re
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from .config import JobAgentConfig
from .company_filters import company_aliases, compact_text
from .db import Database
from .models import FrontierItem, LinkCandidate, QuerySuggestion
from .language import bootstrap_template_values
from .location import evaluate_exploration_url_location
from .urltools import career_candidate_urls, clean_url, domain_from_url, link_hint_score, render_query_url, source_key


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def read_seed_urls(path: Path, config: JobAgentConfig) -> list[str]:
    if not path.exists():
        return []

    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        url = clean_url(raw, None, config)
        if url:
            out.append(url)
    return list(dict.fromkeys(out))


def bootstrap_queries(config: JobAgentConfig) -> list[str]:
    values = bootstrap_template_values(config)
    queries = [template.format(**values) for template in config.exploration.bootstrap_query_templates]
    return list(dict.fromkeys(q.strip() for q in queries if q.strip()))


def search_urls_for_query(query: str, config: JobAgentConfig, templates: list[str] | None = None) -> list[str]:
    out: list[str] = []
    for template in templates or config.exploration.search_url_templates:
        raw = render_query_url(query, template)
        url = clean_url(raw, None, config)
        if url:
            out.append(url)
    return list(dict.fromkeys(out))


def allow_exploratory_searches(config: JobAgentConfig) -> bool:
    return True


def _is_job_portal_url(url: str, config: JobAgentConfig) -> bool:
    low = url.casefold()
    return any(part.casefold() in low for part in config.exploration.job_portal_domain_substrings)


def _looks_like_detail_url(url: str, config: JobAgentConfig) -> bool:
    import re

    for pattern in config.heuristic_extraction.detail_url_positive_patterns:
        try:
            if re.search(pattern, url):
                return True
        except re.error:
            continue
    return False


def _contains_any_term(values: str, terms: list[str]) -> bool:
    hay = values.casefold()
    return any(term.casefold().strip() and term.casefold().strip() in hay for term in terms)


def _human_readable_job_slug(url: str) -> bool:
    """Return true for detail URLs whose slug visibly contains a job title.

    ATS pages with opaque IDs should not be rejected solely because the URL has
    no role term. Natural-language slugs such as /embedded-software-developer or
    /werkstudent-space-testing, however, are safe to pre-filter before spending
    an LLM request.
    """
    parsed = urlparse(url)
    path = unquote(parsed.path or "").casefold()
    last = path.rstrip("/").split("/")[-1]
    if not last:
        return False
    tokens = [tok for tok in re.findall(r"[a-zA-ZäöüÄÖÜß]{3,}", last)]
    # Remove generic URL words and common suffixes so a slug like
    # karriere-stellenangebote_251573 is not treated as a specific job title.
    generic = {
        "job", "jobs", "career", "careers", "karriere", "stellen", "stellenangebote",
        "stellenangebot", "angebote", "html", "de", "en", "mwd", "wmd", "fmd",
        "all", "genders", "muenchen", "munich", "germany", "deutschland",
    }
    meaningful = [tok for tok in tokens if tok not in generic]
    return len(meaningful) >= 2


def _unfocused_listing_url(url: str, context: str, config: JobAgentConfig) -> bool:
    """Reject listing/sort/filter variants that are not narrowed by role.

    Company ATS pages often expose many URLs for country switching, sorting,
    favorites, and wildcard search. Those pages do not add discovery value and
    tend to fan out into the entire company job catalog.
    """
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if not query:
        return False

    combined = f"{url}\n{context}"
    has_role_signal = _contains_any_term(combined, config.score_consistency.target_role_terms + config.matching.preferred_terms)
    if has_role_signal:
        return False

    keys = {key.casefold() for key in query}
    values = [unquote(str(item)).strip() for items in query.values() for item in items]
    wildcard_terms = {"*", "%2a", ""}

    if "term" in keys and any(value.casefold() in wildcard_terms for value in values):
        return True
    if keys <= {"sort", "sortdir", "showfavorites", "change_c", "locale", "language", "lang"}:
        return True
    if "showfavorites" in keys or "change_c" in keys:
        return True
    return False


def _exploration_role_focus_allowed(url: str, context: str, config: JobAgentConfig) -> bool:
    looks_like_job_detail = _looks_like_detail_url(url, config) or _human_readable_job_slug(url)
    if not looks_like_job_detail:
        return True

    combined = f"{url}\n{context}"
    has_role_signal = _contains_any_term(combined, config.score_consistency.target_role_terms + config.matching.preferred_terms)
    has_avoid = _contains_any_term(combined, config.matching.avoid_terms)

    # Avoid/exclusion terms from profile.md take priority over company identity.
    if config.exploration.drop_avoid_only_job_detail_urls and has_avoid and not has_role_signal:
        return False

    # Public job portals and human-readable company job slugs are cheap to
    # pre-filter. Company identity alone is not enough because it otherwise
    # causes broad company career pages to enqueue every engineer, student,
    # trainer, service, and warehouse posting.
    if config.exploration.require_role_signal_for_job_detail_urls and _is_job_portal_url(url, config):
        if not has_role_signal:
            return False

    if (
        config.exploration.require_role_signal_for_human_readable_company_job_urls
        and _human_readable_job_slug(url)
        and not has_role_signal
    ):
        return False

    return True


def exploration_scope_allowed(url: str, context: str, config: JobAgentConfig) -> bool:
    if _unfocused_listing_url(url, context, config):
        return False
    if not _exploration_role_focus_allowed(url, context, config):
        return False
    return True


def exploration_url_allowed(url: str, context: str, config: JobAgentConfig) -> bool:
    verdict = evaluate_exploration_url_location(url, context, config)
    if not verdict.allowed:
        return False
    return exploration_scope_allowed(url, context, config)


def priority_for_url(
    url: str,
    depth: int,
    reason: str,
    config: JobAgentConfig,
    db: Database,
    link_hint: float = 0.0,
) -> float:
    skey = source_key(url, config)
    domain = domain_from_url(url)
    memory_score = db.source_score(skey, domain)
    _, inferred_reason = link_hint_score(url, reason, config)
    hint = link_hint + (1.0 if inferred_reason else 0.0)
    jitter = random.random() * config.memory.priority_random_jitter
    return (
        memory_score * config.memory.priority_weight_memory
        - depth * config.memory.priority_weight_depth
        + hint * config.memory.priority_weight_hint
        + jitter
    )


def make_frontier_item(
    url: str,
    depth: int,
    discovered_from: str,
    reason: str,
    config: JobAgentConfig,
    db: Database,
    link_hint: float = 0.0,
) -> FrontierItem:
    skey = source_key(url, config)
    return FrontierItem(
        url=url,
        depth=depth,
        priority=priority_for_url(url, depth, reason, config, db, link_hint),
        discovered_from=discovered_from,
        reason=reason,
        source_key=skey,
    )


def seed_frontier(config: JobAgentConfig, db: Database, seed_path: Path) -> int:
    count = 0
    seeds = read_seed_urls(seed_path, config)

    for url in seeds:
        item = make_frontier_item(
            url=url,
            depth=0,
            discovered_from="seed-file",
            reason="configured seed URL",
            config=config,
            db=db,
            link_hint=1.0,
        )
        if db.enqueue(item):
            count += 1

    if not seeds and config.exploration.enabled and config.exploration.seed_search_when_empty:
        for query in bootstrap_queries(config):
            db.save_query(query, "bootstrap query from config", "bootstrap")
            for url in search_urls_for_query(query, config):
                if not exploration_url_allowed(url, query, config):
                    continue
                item = make_frontier_item(
                    url=url,
                    depth=0,
                    discovered_from="bootstrap-query",
                    reason=query,
                    config=config,
                    db=db,
                    link_hint=1.0,
                )
                if db.enqueue(item):
                    count += 1

    return count


def enqueue_links(
    links: list[LinkCandidate],
    source_url: str,
    next_depth: int,
    config: JobAgentConfig,
    db: Database,
) -> int:
    count = 0
    for link in links:
        url = clean_url(link.url, source_url, config)
        if not url:
            continue
        if not exploration_url_allowed(url, f"{link.text}\n{link.reason}", config):
            continue
        item = make_frontier_item(
            url=url,
            depth=next_depth,
            discovered_from=source_url,
            reason=link.reason or link.text,
            config=config,
            db=db,
            link_hint=link.score,
        )
        if db.enqueue(item):
            count += 1
    return count


def enqueue_follow_urls(
    urls: list[str],
    source_url: str,
    next_depth: int,
    config: JobAgentConfig,
    db: Database,
    candidate_links: list[LinkCandidate] | None = None,
) -> int:
    lookup: dict[str, LinkCandidate] = {}
    for link in candidate_links or []:
        cleaned = clean_url(link.url, source_url, config)
        if cleaned:
            lookup[cleaned] = link

    links: list[LinkCandidate] = []
    for url in urls:
        cleaned = clean_url(url, source_url, config)
        matched = lookup.get(cleaned or "")
        if matched:
            links.append(LinkCandidate(text=matched.text, url=url, score=max(2.0, matched.score), reason=f"LLM follow URL; {matched.reason}"))
        else:
            links.append(LinkCandidate(text="llm-follow", url=url, score=2.0, reason="LLM follow URL"))
    return enqueue_links(links, source_url, next_depth, config, db)


def enqueue_career_candidates(
    url: str,
    source_url: str,
    next_depth: int,
    config: JobAgentConfig,
    db: Database,
) -> int:
    links = [
        LinkCandidate(text="career candidate", url=candidate, score=1.5, reason="standard career path")
        for candidate in career_candidate_urls(url, config)
    ]
    return enqueue_links(links, source_url, next_depth, config, db)


def enqueue_query_suggestions(
    suggestions: list[QuerySuggestion],
    config: JobAgentConfig,
    db: Database,
) -> int:
    count = 0
    for suggestion in suggestions[: config.exploration.max_generated_queries_per_run]:
        db.save_query(suggestion.query, suggestion.reason, "llm")
        db.mark_query_used(suggestion.query)
        for url in search_urls_for_query(suggestion.query, config):
            if not exploration_url_allowed(url, suggestion.query, config):
                continue
            item = make_frontier_item(
                url=url,
                depth=0,
                discovered_from="llm-query",
                reason=suggestion.query,
                config=config,
                db=db,
                link_hint=1.0,
            )
            if db.enqueue(item):
                count += 1
    return count


def should_generate_queries(pages_done: int, generated_queries: int, config: JobAgentConfig) -> bool:
    if not config.exploration.enabled:
        return False
    if generated_queries >= config.exploration.max_generated_queries_per_run:
        return False
    return pages_done > 0 and pages_done % config.exploration.query_generation_every_pages == 0


def build_run_summary(pages_done: int, jobs_saved: int, queued: int) -> str:
    return f"pages_done={pages_done}, jobs_saved={jobs_saved}, queued_pages={queued}"


def encode_query_for_display(query: str) -> str:
    return quote_plus(query)
