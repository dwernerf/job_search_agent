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
    """Sample distinct role/city/suffix/company queries in shuffled role rounds."""
    city = config.target.local_area.split(",")[0].strip()
    roles = list(dict.fromkeys(config.target.roles))
    suffixes = list(dict.fromkeys(config.seeding.bootstrapped_search.job_suffixes)) or [""]
    companies = list(dict.fromkeys(config.seeding.bootstrapped_search.company_whitelist))
    max_samples = config.seeding.bootstrapped_search.max_samples

    available = {
        role: [(suffix, company) for suffix in suffixes for company in ["", *companies]]
        for role in roles
    }
    queries: list[str] = []
    seen: set[str] = set()

    while len(queries) < max_samples:
        active_roles = [role for role in roles if available[role]]
        if not active_roles:
            break
        random.shuffle(active_roles)

        for role in active_roles:
            options = available[role]
            with_company = [option for option in options if option[1]]
            without_company = [option for option in options if not option[1]]
            prefer_company = bool(companies) and random.random() < 0.5
            candidates = with_company if prefer_company else without_company
            if not candidates:
                candidates = without_company if prefer_company else with_company

            suffix, company = random.choice(candidates)
            options.remove((suffix, company))
            query = " ".join(part for part in (role, city, suffix, company) if part)
            if query not in seen:
                seen.add(query)
                queries.append(query)
            if len(queries) >= max_samples:
                break

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
