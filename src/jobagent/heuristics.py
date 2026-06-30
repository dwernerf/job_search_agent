from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from .config import JobAgentConfig
from .language import multilingual_role_terms, unique_terms
from .models import JobMatch, LinkCandidate, PageSnapshot
from .urltools import clean_url, denied_by_safety, domain_from_url


def _norm(value: str) -> str:
    return unquote(value or "").casefold()


def _terms_present(text: str, terms: list[str]) -> list[str]:
    hay = _norm(text)
    hits = []
    for term in terms:
        needle = _norm(term).strip()
        if needle and needle in hay:
            hits.append(term)
    return unique_terms(hits)


def _role_terms(config: JobAgentConfig) -> list[str]:
    return unique_terms(config.target.roles + multilingual_role_terms(config))


def _location_terms(config: JobAgentConfig) -> list[str]:
    return unique_terms([config.target.local_area] + config.matching.location_aliases + config.exploration.local_area_terms)


def _remote_terms(config: JobAgentConfig) -> list[str]:
    return ["remote", "homeoffice", "home office", "hybrid", "teilweise home-office", "mobiles arbeiten"]


def _score(text: str, base: int, config: JobAgentConfig) -> tuple[int, list[str]]:
    cfg = config.heuristic_extraction
    score = base
    reasons: list[str] = []

    role_hits = _terms_present(text, _role_terms(config))
    if role_hits:
        score += cfg.role_bonus
        reasons.append("role term: " + ", ".join(role_hits[:3]))

    location_hits = _terms_present(text, _location_terms(config))
    if location_hits:
        score += cfg.location_bonus
        reasons.append("location: " + ", ".join(location_hits[:3]))

    preferred_hits = _terms_present(text, config.matching.preferred_terms)
    if preferred_hits:
        score += cfg.preferred_term_bonus
        reasons.append("preferred term: " + ", ".join(preferred_hits[:3]))

    if config.target.include_remote and _terms_present(text, _remote_terms(config)):
        score += cfg.remote_bonus
        reasons.append("remote/hybrid signal")

    avoid_hits = _terms_present(text, config.matching.avoid_terms)
    if avoid_hits:
        score -= cfg.avoid_term_penalty
        reasons.append("avoid term: " + ", ".join(avoid_hits[:2]))

    score = max(0, min(cfg.max_score, score))
    return score, reasons



def _matches_any(patterns: list[str], text: str) -> bool:
    for pattern in patterns:
        try:
            if re.search(pattern, text or ""):
                return True
        except re.error:
            continue
    return False


def is_index_or_listing_page(snapshot: PageSnapshot, config: JobAgentConfig) -> bool:
    cfg = config.heuristic_extraction
    hay = f"{snapshot.title}\n{snapshot.url}\n{snapshot.final_url}"
    return _matches_any(cfg.index_page_title_patterns, hay) or _matches_any(cfg.index_page_url_patterns, hay)


def is_probable_detail_url(url: str, title: str, config: JobAgentConfig) -> bool:
    cfg = config.heuristic_extraction
    hay = f"{url}\n{title}"
    if _matches_any(cfg.detail_url_negative_patterns, hay):
        return False
    if cfg.detail_url_positive_patterns and _matches_any(cfg.detail_url_positive_patterns, hay):
        return True
    # Generic fallback: role-bearing slug plus a path deeper than a search/index page.
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.split("/") if p]
    if len(path_parts) >= 2 and _terms_present(url, _role_terms(config) + config.matching.preferred_terms):
        return not _matches_any(cfg.index_page_url_patterns, url)
    return False

def _title_from_link(link: LinkCandidate) -> str:
    text = re.sub(r"\s+", " ", link.text or "").strip()
    if text:
        return text[:300]
    path = unquote(urlparse(link.url).path).strip("/")
    tail = path.split("/")[-1] if path else ""
    return re.sub(r"[-_]+", " ", tail).strip().title()[:300]


def _location_for_link(snapshot: PageSnapshot, config: JobAgentConfig) -> str:
    hay = f"{snapshot.title}\n{snapshot.url}\n{snapshot.final_url}\n{snapshot.text[:3000]}"
    hits = _terms_present(hay, _location_terms(config))
    return hits[0] if hits else ""


def structured_jobs_from_page(snapshot: PageSnapshot, config: JobAgentConfig) -> list[JobMatch]:
    cfg = config.heuristic_extraction
    if not cfg.enabled or not cfg.use_structured_data:
        return []

    out: list[JobMatch] = []
    for item in snapshot.structured_jobs:
        title = str(item.get("title") or "").strip()
        url = clean_url(str(item.get("url") or snapshot.final_url), snapshot.final_url, config)
        if not title or not url or denied_by_safety(url, title, config):
            continue
        company = str(item.get("company") or "").strip()
        location = str(item.get("location") or "").strip()
        description = str(item.get("description") or "")
        score, reasons = _score(
            f"{title}\n{company}\n{location}\n{description}\n{snapshot.title}\n{snapshot.url}",
            cfg.structured_base_score,
            config,
        )
        if score < config.matching.min_fit_score_to_save:
            continue
        out.append(
            JobMatch(
                title=title,
                company=company,
                location=location,
                url=url,
                fit_score=score,
                reason="Heuristic structured-data match; " + "; ".join(reasons[:3]),
                evidence="schema.org JobPosting",
                posting_language="Unknown",
                score_source="heuristic_structured",
                score_basis="; ".join(reasons[:5]),
            )
        )
        if len(out) >= cfg.max_jobs_per_page:
            break
    return out


def link_jobs_from_page(snapshot: PageSnapshot, candidate_links: list[LinkCandidate], config: JobAgentConfig) -> list[JobMatch]:
    cfg = config.heuristic_extraction
    if not cfg.enabled or not cfg.use_candidate_links:
        return []

    if config.heuristic_extraction.suppress_link_jobs_on_index_pages and is_index_or_listing_page(snapshot, config):
        return []

    source_location = _location_for_link(snapshot, config)
    source_context = f"{snapshot.title}\n{snapshot.url}\n{snapshot.final_url}\n{snapshot.text[:3000]}"
    out: list[JobMatch] = []
    seen: set[str] = set()

    for link in candidate_links:
        url = clean_url(link.url, snapshot.final_url, config)
        title = _title_from_link(link)
        if not url or url in seen or not title:
            continue
        if denied_by_safety(url, title, config):
            continue
        if config.heuristic_extraction.require_detail_url_for_link_jobs and not is_probable_detail_url(url, title, config):
            continue

        link_text = f"{title}\n{url}\n{source_context}"
        role_hits = _terms_present(f"{title}\n{url}", _role_terms(config) + config.matching.preferred_terms)
        if not role_hits:
            continue

        score, reasons = _score(link_text, cfg.link_base_score, config)
        if score < config.matching.min_fit_score_to_save:
            continue

        company = domain_from_url(url)
        out.append(
            JobMatch(
                title=title,
                company=company,
                location=source_location,
                url=url,
                fit_score=score,
                reason="Heuristic job-link match; " + "; ".join(reasons[:3]),
                evidence=f"candidate link text: {title[:160]}",
                posting_language="Unknown",
                score_source="heuristic_link",
                score_basis="; ".join(reasons[:5]),
            )
        )
        seen.add(url)
        if len(out) >= cfg.max_jobs_per_page:
            break
    return out


def heuristic_jobs_from_page(snapshot: PageSnapshot, candidate_links: list[LinkCandidate], config: JobAgentConfig) -> list[JobMatch]:
    if not config.heuristic_extraction.enabled:
        return []
    jobs = structured_jobs_from_page(snapshot, config) + link_jobs_from_page(snapshot, candidate_links, config)
    deduped: list[JobMatch] = []
    seen: set[str] = set()
    for job in jobs:
        if job.url in seen:
            continue
        seen.add(job.url)
        deduped.append(job)
        if len(deduped) >= config.heuristic_extraction.max_jobs_per_page:
            break
    return deduped
