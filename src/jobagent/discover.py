from __future__ import annotations

import random
import re
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from .config import JobAgentConfig
from .company_filters import company_aliases, compact_text, match_whitelist_company, whitelist_scope_active, whitelist_scope_allows
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
        url = clean_url(line, None, config)
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



def allow_whitelist_searches(config: JobAgentConfig) -> bool:
    return config.exploration.mode in {"both", "whitelist_only"}


def allow_exploratory_searches(config: JobAgentConfig) -> bool:
    return config.exploration.mode in {"both", "exploratory_only"}

def company_whitelist_queries(config: JobAgentConfig) -> list[str]:
    """Legacy generic web-search queries for company discovery.

    This is intentionally disabled by default through
    companies.max_search_queries_per_company=0 because generic search engines
    often return 403/CAPTCHA pages in unattended local crawls. It remains
    configurable for users who provide an allowed search API/template.
    """
    if not config.companies.whitelist_search_when_seeding:
        return []
    if not config.companies.whitelist:
        return []
    if config.companies.max_search_queries_per_company <= 0:
        return []
    roles = " ".join(config.companies.portal_role_terms[:2]) or "jobs"
    location = config.target.local_area
    queries: list[str] = []
    for company in config.companies.whitelist:
        templates = [
            f"{company} careers Karriere Stellenangebote",
            f"{company} {roles} {location}",
        ]
        queries.extend(templates[: config.companies.max_search_queries_per_company])
    return list(dict.fromkeys(q.strip() for q in queries if q.strip()))


def company_whitelist_portal_queries(config: JobAgentConfig) -> list[str]:
    """Simple company+role queries for job portals.

    Job portals generally do not implement Google-style OR syntax. A query like
    '"HENSOLDT" (career OR careers OR Karriere)' is treated mostly as literal
    text and leads to broad or zero-result pages. Use short atomic queries.
    """
    if not config.companies.whitelist_search_when_seeding or not config.companies.whitelist:
        return []
    role_terms = [term.strip() for term in config.companies.portal_role_terms if term.strip()]
    if not role_terms:
        role_terms = [role.strip() for role in config.target.roles if role.strip()]
    role_terms = list(dict.fromkeys(role_terms))[: config.companies.max_portal_role_terms_per_company]
    queries: list[str] = []
    for company in config.companies.whitelist:
        for term in role_terms:
            queries.append(f"{company} {term}")
    return queries


def company_domain_candidates(company: str, config: JobAgentConfig) -> list[str]:
    domains: list[str] = []
    for key, configured in config.companies.known_domains.items():
        if compact_text(key) == compact_text(company):
            domains.extend(configured)
            break
    if config.companies.infer_domains_from_company_names:
        for alias in company_aliases(company):
            compact = compact_text(alias)
            # Skip very short single-token aliases that are likely too broad.
            if len(compact) < 4:
                continue
            for suffix in config.companies.inferred_domain_suffixes:
                domains.append(compact + suffix)
    out: list[str] = []
    seen: set[str] = set()
    for domain in domains:
        d = str(domain).strip().lower()
        d = d.replace("https://", "").replace("http://", "").strip("/")
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def company_direct_career_urls(config: JobAgentConfig) -> list[tuple[str, str]]:
    """Return (url, company) direct company entrypoints.

    Do not brute-force /career, /jobs, /karriere, etc. by default. Those
    guesses caused many 404s. In root_only mode the crawler opens the company
    root/ATS root and follows visible career/job links found there. Users who
    explicitly want path probing can set companies.direct_career_discovery to
    root_plus_configured_paths.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    if not config.companies.whitelist_search_when_seeding:
        return []

    for company in config.companies.whitelist:
        added = 0
        for domain in company_domain_candidates(company, config):
            root = f"https://{domain}".rstrip("/")
            candidates = [root]
            if config.companies.direct_career_discovery == "root_plus_configured_paths":
                candidates.extend(root + "/" + path.strip().lstrip("/") for path in config.crawler.career_path_candidates)

            for raw in candidates:
                url = clean_url(raw, None, config)
                if not url or url in seen:
                    continue
                if not exploration_url_allowed(url, f"{company} company entrypoint", config):
                    continue
                seen.add(url)
                out.append((url, company))
                added += 1
                if added >= config.companies.max_direct_career_urls_per_company:
                    break
            if added >= config.companies.max_direct_career_urls_per_company:
                break
    return out


def company_career_page_queries(config: JobAgentConfig) -> list[str]:
    """Queries intended to find official company career pages via a configured search endpoint."""
    if not config.companies.whitelist_search_when_seeding or not config.companies.whitelist:
        return []
    if not config.companies.career_page_search_templates or config.companies.max_career_page_searches_per_company <= 0:
        return []

    queries: list[str] = []
    for company in config.companies.whitelist:
        company_queries = [
            f"{company} careers",
            f"{company} Karriere",
            f"{company} Stellenangebote",
        ]
        queries.extend(company_queries[: config.companies.max_career_page_searches_per_company])
    return list(dict.fromkeys(q.strip() for q in queries if q.strip()))


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
    has_whitelist_company = match_whitelist_company(config, combined) is not None
    has_avoid = _contains_any_term(combined, config.matching.avoid_terms)

    # Avoid/exclusion terms from profile.md override company whitelisting. A
    # whitelist company can still advertise internships, software roles, sales,
    # warehouse jobs, etc.; those should not consume LLM calls unless the same
    # URL/text also contains an explicit target-role signal.
    if config.exploration.drop_avoid_only_job_detail_urls and has_avoid and not has_role_signal:
        return False

    # Public job portals and human-readable company job slugs are cheap to
    # pre-filter. Whitelist company identity alone is not enough here because it
    # otherwise causes broad company career pages to enqueue every engineer,
    # student, trainer, service, and warehouse posting.
    if config.exploration.require_role_signal_for_job_detail_urls and _is_job_portal_url(url, config):
        if not has_role_signal:
            return False

    if (
        config.exploration.require_role_signal_for_human_readable_company_job_urls
        and _human_readable_job_slug(url)
        and not has_role_signal
    ):
        return False

    if whitelist_scope_active(config) and not has_whitelist_company:
        # In whitelist-only mode, detail pages must still carry a company signal
        # somewhere in the URL/link context.
        return False

    return True


def exploration_scope_allowed(url: str, context: str, config: JobAgentConfig) -> bool:
    if not whitelist_scope_allows(config, url, context):
        return False
    if whitelist_scope_active(config) and _unfocused_listing_url(url, context, config):
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

    if not seeds and config.exploration.enabled and allow_exploratory_searches(config) and config.exploration.seed_search_when_empty:
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

    if config.exploration.enabled and allow_whitelist_searches(config) and config.companies.whitelist_search_when_seeding:
        # 1) Direct company entrypoints from configured company domains. In the
        #    default root_only mode this does not guess /careers or /jobs paths.
        #    The crawler follows visible career links from the loaded company root.
        for url, company in company_direct_career_urls(config):
            item = make_frontier_item(
                url=url,
                depth=0,
                discovered_from="company-direct-career",
                reason=f"whitelist company entrypoint: {company}",
                config=config,
                db=db,
                link_hint=1.25,
            )
            if db.enqueue(item):
                count += 1

        # 2) Optional official-career-page search. This is only active when the
        #    user configures a permitted search endpoint in
        #    companies.career_page_search_templates.
        career_search_depth = 1 if seeds else 0
        for query in company_career_page_queries(config):
            db.save_query(query, "company career-page search query from config", "company-career-search")
            for url in search_urls_for_query(query, config, config.companies.career_page_search_templates):
                if not exploration_url_allowed(url, query, config):
                    continue
                item = make_frontier_item(
                    url=url,
                    depth=career_search_depth,
                    discovered_from="company-career-search-query",
                    reason=query,
                    config=config,
                    db=db,
                    link_hint=0.9,
                )
                if db.enqueue(item):
                    count += 1

        # 3) Focused company+role searches on job portals. Keep the query syntax
        #    simple; no OR/parentheses.
        portal_depth = 1 if seeds else 0
        for query in company_whitelist_portal_queries(config):
            db.save_query(query, "company whitelist job-portal query from config", "company-whitelist-portal")
            for url in search_urls_for_query(query, config, config.exploration.whitelist_job_portal_search_templates):
                if not exploration_url_allowed(url, query, config):
                    continue
                item = make_frontier_item(
                    url=url,
                    depth=portal_depth,
                    discovered_from="company-whitelist-portal-query",
                    reason=query,
                    config=config,
                    db=db,
                    link_hint=0.75,
                )
                if db.enqueue(item):
                    count += 1

        # 4) Optional generic web-search query route for users who configure a
        #    compliant search endpoint. Disabled by default.
        whitelist_depth = 1 if seeds else 0
        for query in company_whitelist_queries(config):
            db.save_query(query, "company whitelist query from config", "company-whitelist")
            for url in search_urls_for_query(query, config):
                if not exploration_url_allowed(url, query, config):
                    continue
                item = make_frontier_item(
                    url=url,
                    depth=whitelist_depth,
                    discovered_from="company-whitelist-query",
                    reason=query,
                    config=config,
                    db=db,
                    link_hint=0.5,
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
    if not allow_exploratory_searches(config):
        return 0
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
    if not config.exploration.enabled or not allow_exploratory_searches(config):
        return False
    if generated_queries >= config.exploration.max_generated_queries_per_run:
        return False
    return pages_done > 0 and pages_done % config.exploration.query_generation_every_pages == 0


def build_run_summary(pages_done: int, jobs_saved: int, queued: int) -> str:
    return f"pages_done={pages_done}, jobs_saved={jobs_saved}, queued_pages={queued}"


def encode_query_for_display(query: str) -> str:
    return quote_plus(query)
