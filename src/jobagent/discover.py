from __future__ import annotations

import random
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import JobAgentConfig
from .db import Database
from .models import BacklogItem, LinkCandidate

from .urltools import career_candidate_urls, clean_url, render_query_url, source_key


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
    """Generate one simple query per target role: 'Role City' + one random job suffix + optional whitelist company."""
    city = config.target.local_area.split(",")[0].strip()
    queries: list[str] = []
    suffixes = config.seeding.bootstrapped_search.job_suffixes
    whitelist = config.seeding.bootstrapped_search.company_whitelist
    for role in config.target.roles:
        # Pick one random suffix to append
        suffix = random.choice(suffixes) if suffixes else ""
        query = f"{role} {city}"
        if suffix:
            query = f"{query} {suffix}"
        if whitelist and random.random() < 0.5:
            query = f"{query} {random.choice(whitelist)}"
        if query not in queries:
            queries.append(query)
    return queries


def search_urls_for_query(query: str, config: JobAgentConfig, templates: list[str] | None = None) -> list[str]:
    out: list[str] = []
    for template in templates or config.seeding.bootstrapped_search.search_url_templates:
        raw = render_query_url(query, template)
        url = clean_url(raw, None, config)
        if url:
            out.append(url)
    return list(dict.fromkeys(out))


def _looks_like_detail_url(url: str, config: JobAgentConfig) -> bool:
    import re

    for pattern in config.crawler.job_link_hints:
        try:
            if re.search(pattern, url):
                return True
        except re.error:
            continue
    return False


def seed_backlog(config: JobAgentConfig, db: Database, seed_path: Path) -> int:
    count = 0
    mode = config.seeding.mode

    if mode in ("seeds", "both"):
        seeds = read_seed_urls(seed_path, config)
        for url in seeds:
            item = BacklogItem(
                url=url,
                depth=0,
                discovered_from="seed-file",
                reason="configured seed URL",
                source_key=source_key(url, config),
            )
            if db.enqueue(item):
                count += 1

    if mode in ("bootstrap", "both") and config.exploration.enabled:
        for query in bootstrap_queries(config):
            for url in search_urls_for_query(query, config):
                item = BacklogItem(
                    url=url,
                    depth=0,
                    discovered_from="bootstrap-query",
                    reason=query,
                    source_key=source_key(url, config),
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
        item = BacklogItem(
            url=url,
            depth=next_depth,
            discovered_from=source_url,
            reason=link.text,
            source_key=source_key(url, config),
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
) -> int:
    links: list[LinkCandidate] = []
    for url in urls:
        cleaned = clean_url(url, source_url, config)
        links.append(LinkCandidate(text="llm-follow", url=url))
    return enqueue_links(links, source_url, next_depth, config, db)


def enqueue_career_candidates(
    url: str,
    source_url: str,
    next_depth: int,
    config: JobAgentConfig,
    db: Database,
) -> int:
    links = [
        LinkCandidate(text="career candidate", url=candidate)
        for candidate in career_candidate_urls(url, config)
    ]
    return enqueue_links(links, source_url, next_depth, config, db)


def build_run_summary(pages_done: int, jobs_saved: int, queued: int) -> str:
    return f"pages_done={pages_done}, jobs_saved={jobs_saved}, queued_pages={queued}"
