from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import JobAgentConfig


def unique_terms(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        term = str(value).strip()
        key = term.casefold()
        if term and key not in seen:
            seen.add(key)
            out.append(term)
    return out


def multilingual_role_terms(config: JobAgentConfig) -> list[str]:
    return unique_terms(config.target.roles)


def multilingual_job_terms(config: JobAgentConfig) -> list[str]:
    if not config.multilingual.enabled:
        return unique_terms(config.crawler.job_link_hints)
    return unique_terms(
        config.crawler.job_link_hints
        + config.multilingual.german_job_terms
        + config.multilingual.english_job_terms
        + config.multilingual.german_career_terms
        + config.multilingual.english_career_terms
    )


def multilingual_relevance_terms(config: JobAgentConfig) -> list[str]:
    terms = (
        multilingual_role_terms(config)
        + multilingual_job_terms(config)
        + config.matching.location_aliases
        + config.matching.preferred_terms
        + config.matching.avoid_terms
        + config.exploration.local_area_terms
        + config.exploration.source_discovery_terms
    )
    return unique_terms(terms)


def language_policy_summary(config: JobAgentConfig) -> str:
    if not config.multilingual.enabled:
        return "Multilingual mode disabled. Use the target languages from config only."

    return "\n".join(
        [
            f"Primary market language: {config.multilingual.primary_market_language}",
            f"Output language for reasons/notes: {config.multilingual.output_language}",
            f"Keep original job titles/company names/evidence: {config.multilingual.keep_original_job_titles}",
            f"Treat mixed German-English pages as normal: {config.multilingual.treat_mixed_language_as_normal}",
        ]
    )
