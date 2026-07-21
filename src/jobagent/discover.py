from __future__ import annotations

import random
from pathlib import Path

from .config import JobAgentConfig
from .db import Database

from .urltools import filter_url, render_query_url


def read_seed_urls(path: Path, config: JobAgentConfig) -> list[str]:
    if not path.exists():
        return []

    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        url = filter_url(raw, None, config)
        if url:
            out.append(url)
    return list(dict.fromkeys(out))


def bootstrap_queries(config: JobAgentConfig) -> list[str]:
    """Generate one role/city query per distinct target role."""
    city = config.target.local_area.split(",")[0].strip()
    roles = list(dict.fromkeys(config.target.roles))
    suffixes = config.seeding.bootstrapped_search.job_suffixes
    companies = config.seeding.bootstrapped_search.company_whitelist
    queries: list[str] = []

    for role in roles:
        suffix = random.choice(suffixes) if suffixes else ""
        company = random.choice(companies) if companies and random.random() < 0.5 else ""
        query = " ".join(part for part in (role, city, suffix, company) if part)
        if query not in queries:
            queries.append(query)

    return queries


def search_urls_for_query(query: str, config: JobAgentConfig) -> list[str]:
    out: list[str] = []
    for template in config.seeding.bootstrapped_search.search_url_templates:
        raw = render_query_url(query, template)
        url = filter_url(raw, None, config)
        if url:
            out.append(url)
    return list(dict.fromkeys(out))


def seed_backlog(config: JobAgentConfig, db: Database, seed_path: Path) -> int:
    count = 0
    mode = config.seeding.mode

    if mode in ("seeds", "both"):
        seeds = read_seed_urls(seed_path, config)
        for url in seeds:
            if db.enqueue(url):
                count += 1

    if mode in ("bootstrap", "both"):
        for query in bootstrap_queries(config):
            for url in search_urls_for_query(query, config):
                if db.enqueue(url):
                    count += 1

    return count
