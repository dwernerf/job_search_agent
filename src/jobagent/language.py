from __future__ import annotations

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
    if not config.multilingual.enabled:
        return unique_terms(config.target.roles)
    return unique_terms(
        config.target.roles
        + config.multilingual.german_role_terms
        + config.multilingual.english_role_terms
        + config.multilingual.mixed_role_terms
    )


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
            f"Accepted posting languages: {', '.join(config.multilingual.accepted_languages)}",
            f"Output language for reasons/notes: {config.multilingual.output_language}",
            f"Keep original job titles/company names/evidence: {config.multilingual.keep_original_job_titles}",
            f"Treat mixed German-English pages as normal: {config.multilingual.treat_mixed_language_as_normal}",
            "German role/search terms: " + ", ".join(config.multilingual.german_role_terms),
            "English role/search terms: " + ", ".join(config.multilingual.english_role_terms),
            "Mixed role/search terms: " + ", ".join(config.multilingual.mixed_role_terms),
        ]
    )


def role_query_text(config: JobAgentConfig, terms: list[str] | None = None, max_terms: int = 12) -> str:
    selected = unique_terms(terms or multilingual_role_terms(config))[:max_terms]
    return " OR ".join(selected)


def bootstrap_template_values(config: JobAgentConfig) -> dict[str, str]:
    # Values are derived from profile.md via load_config(), not from role-specific YAML lists.
    german_roles = role_query_text(config, config.multilingual.german_role_terms or config.target.roles)
    english_roles = role_query_text(config, config.multilingual.english_role_terms or config.target.roles)
    mixed_roles = role_query_text(config, multilingual_role_terms(config))
    role_terms = role_query_text(config, config.score_consistency.target_role_terms or multilingual_role_terms(config), max_terms=12)
    expertise_terms = role_query_text(config, config.matching.preferred_terms, max_terms=8)
    job_terms = role_query_text(config, multilingual_job_terms(config), max_terms=10)
    location_terms = role_query_text(
        config,
        unique_terms([config.target.local_area] + config.matching.location_aliases + config.exploration.local_area_terms),
        max_terms=8,
    )
    roles = role_query_text(config, config.target.roles or config.score_consistency.target_role_terms, max_terms=8)
    return {
        "roles": roles,
        "role": (config.target.roles or config.score_consistency.target_role_terms or ["job"])[0],
        "role_terms": role_terms,
        "expertise_terms": expertise_terms,
        "location": config.target.local_area,
        "languages": ", ".join(config.target.languages),
        "accepted_languages": ", ".join(config.multilingual.accepted_languages) if config.multilingual.enabled else ", ".join(config.target.languages),
        "primary_market_language": config.multilingual.primary_market_language if config.multilingual.enabled else config.target.languages[0],
        "german_roles": german_roles,
        "english_roles": english_roles,
        "mixed_roles": mixed_roles,
        "job_terms": job_terms,
        "location_terms": location_terms,
    }
