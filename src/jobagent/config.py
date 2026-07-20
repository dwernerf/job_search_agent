from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .language import unique_terms
from .profile_knowledge import extract_profile_knowledge


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Defaults live in Python so config.yaml can stay short and readable.  The
# profile-specific vocabulary is injected from profile.md in load_config().
def default_allowed_schemes() -> list[str]:
    return ["http", "https"]


def default_excluded_domains() -> list[str]:
    # Public LinkedIn job search/view URLs may be useful seeds, so LinkedIn is
    # not excluded globally.
    return [
        "facebook.com", "instagram.com", "youtube.com", "youtu.be", "twitter.com",
        "x.com", "tiktok.com", "pinterest.",
    ]


def default_file_extensions() -> list[str]:
    return [
        ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".zip", ".tar",
        ".gz", ".mp4", ".mp3", ".avi", ".webp", ".doc", ".docx", ".xls", ".xlsx",
    ]


def default_tracking_params() -> list[str]:
    return ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "ref", "source", "fbclid", "gclid"]


def default_job_link_hints() -> list[str]:
    return [
        "job", "jobs", "career", "careers", "vacancy", "vacancies", "opening", "openings",
        "position", "positions", "linkedin.com/jobs", "linkedin.com/jobs/view", "join-us", "join us", "recruit", "greenhouse", "lever",
        "workday", "ashby", "smartrecruiters", "teamtailor", "personio", "bamboohr", "icims",
        "successfactors", "karriere", "stellen", "stelle", "stellenangebot", "stellenangebote",
        "offene-stellen", "offene stellen", "jobsuche", "bewerbungsportal", "arbeiten-bei-uns",
    ]


class AppConfig(StrictModel):
    database_path: str = "data/jobs.sqlite"
    csv_export_path: str = "data/jobs.csv"
    jsonl_export_path: str = "data/jobs.jsonl"
    log_path: str = "data/jobagent.log"
    user_agent: str = "JobMatchAgent/0.1 (+local personal job search; read-only)"


class TargetConfig(StrictModel):
    # local_area is filled from config/intent.yaml on startup.
    local_area: str = ""
    roles: list[str] = Field(default_factory=list)
    # languages is filled from config/intent.yaml on startup.
    languages: list[str] = Field(default_factory=list)

    @field_validator("roles")
    @classmethod
    def clean_list(cls, value: list[str]) -> list[str]:
        return [x.strip() for x in value if str(x).strip()]


class MultilingualConfig(StrictModel):
    enabled: bool = True
    primary_market_language: str = "German"
    output_language: str = "English"
    keep_original_job_titles: bool = True
    treat_mixed_language_as_normal: bool = True
    german_job_terms: list[str] = Field(default_factory=lambda: ["Stelle", "Stellenangebote", "Offene Stellen", "Jobsuche", "Karriere", "Bewerbungsportal", "Festanstellung", "Vollzeit", "Homeoffice"])
    english_job_terms: list[str] = Field(default_factory=lambda: ["job", "jobs", "open role", "opening", "vacancy", "full-time", "part-time", "remote", "hybrid"])
    german_career_terms: list[str] = Field(default_factory=lambda: ["Karriere", "Karriereseite", "Arbeiten bei uns", "Jobsuche", "Bewerbungsportal", "Einstieg"])
    english_career_terms: list[str] = Field(default_factory=lambda: ["career", "careers", "career site", "join us", "work with us", "recruiting", "hiring"])

    @field_validator("german_job_terms", "english_job_terms", "german_career_terms", "english_career_terms")
    @classmethod
    def clean_optional_lists(cls, value: list[str]) -> list[str]:
        return [x.strip() for x in value if str(x).strip()]


class ProfileConfig(StrictModel):
    path: str = "config/profile.md"


class SeedsConfig(StrictModel):
    path: str = "config/seeds.txt"


class PromptConfig(StrictModel):
    path: str = "config/prompts.yaml"


class LLMConfig(StrictModel):
    base_url: str = "http://127.0.0.1:8080/v1"
    model: str = "local"
    api_key_env: str = "JOBAGENT_LLM_API_KEY"
    api_key_fallback: str = "no-key"
    chat_endpoint: str = "/chat/completions"
    models_endpoint: str = "/models"
    timeout_seconds: int = Field(default=400, gt=0)
    health_check_timeout_seconds: int = Field(default=5, gt=0)
    require_available_on_start: bool = True
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    response_format_type: str = "json_object"
    thinking_enabled: bool = True
    context_window_tokens: int = Field(default=150000, gt=3000)


class BrowserConfig(StrictModel):
    engine: Literal["chromium", "firefox", "webkit"] = "chromium"
    headless: bool = True
    viewport_width: int = Field(default=1365, gt=0)
    viewport_height: int = Field(default=900, gt=0)
    navigation_timeout_ms: int = Field(default=30000, gt=0)
    body_text_timeout_ms: int = Field(default=7000, gt=0)
    network_idle_timeout_ms: int = Field(default=3000, ge=0)
    wait_until: str = "domcontentloaded"
    fail_on_http_error_statuses: bool = True
    http_error_status_min: int = Field(default=400, ge=300, le=599)


class RunConfig(StrictModel):
    # Ordering of the backlog queue when popping the next item to visit.
    # "fifo"  – first-in-first-out (insertion order, oldest first).
    # "shuffle" – random order via SQLite's ORDER BY random().
    # Shuffle prevents the agent from always following the same traversal path
    # when multiple URLs share the same insertion time (e.g. seeds).
    backlog_order: Literal["fifo", "shuffle"] = "fifo"
    reset_backlog_on_start: bool = True
    min_delay_seconds: float = Field(default=0.2, ge=0)
    max_delay_seconds: float = Field(default=0.8, ge=0)

    @model_validator(mode="after")
    def validate_delay_range(self) -> "RunConfig":
        if self.min_delay_seconds > self.max_delay_seconds:
            raise ValueError("run.min_delay_seconds must be <= run.max_delay_seconds")
        return self


class CrawlerConfig(StrictModel):
    source_key_mode: Literal["domain", "domain_path1", "domain_path2"] = "domain_path1"
    allowed_schemes: list[str] = Field(default_factory=default_allowed_schemes)
    excluded_domain_substrings: list[str] = Field(default_factory=default_excluded_domains)
    excluded_file_extensions: list[str] = Field(default_factory=default_file_extensions)
    dedupe_url_tracking_params: list[str] = Field(default_factory=default_tracking_params)
    job_link_hints: list[str] = Field(default_factory=default_job_link_hints)
    retry_error_pages: bool = True
    max_compact_lines: int = Field(default=180, gt=0)
    max_important_lines: int = Field(default=240, gt=0)
    max_page_text_chars: int = Field(default=14000, gt=0)
    batch_size_for_llm: int = Field(default=30, gt=0)
    max_page_context_chars: int = Field(default=5000, gt=0)


class MatchingConfig(StrictModel):
    location_aliases: list[str] = Field(default_factory=list)
    preferred_terms: list[str] = Field(default_factory=list)
    avoid_terms: list[str] = Field(default_factory=list)


class ScoringConfig(StrictModel):
    min_score_to_export: int = Field(default=55, ge=0, le=100,
        description="Minimum fit score (0-100) for a job to be exported/saved. Jobs below this are dropped.")
    high_fit_score_threshold: int = Field(default=80, ge=0, le=100,
        description="Fit score threshold (0-100) used for high-fit reporting.")


class ExplorationConfig(StrictModel):
    enabled: bool = True
    local_area_terms: list[str] = Field(default_factory=list)
    source_discovery_terms: list[str] = Field(default_factory=list)


class BootstrappedSearchConfig(StrictModel):
    max_samples: int = Field(default=50, gt=0)
    search_url_templates: list[str] = Field(default_factory=list)
    job_suffixes: list[str] = Field(default_factory=list)
    company_whitelist: list[str] = Field(default_factory=list)


class SeedingConfig(StrictModel):
    mode: Literal["seeds", "bootstrap", "both"] = "both"
    bootstrapped_search: BootstrappedSearchConfig = Field(default_factory=BootstrappedSearchConfig)


class CompanyFiltersConfig(StrictModel):
    # Names in blacklist drop matched jobs from exports. Put exact company names here;
    # defaults are filled from config/intent.yaml on startup.
    blacklist: list[str] = Field(default_factory=list)


class LoggingConfig(StrictModel):
    # Only two verbosity levels are supported for normal use.
    # info = structured progress and results. debug = info plus skipped URLs and fine detail.
    level: Literal["info", "debug"] = "info"
    console: bool = True
    file: bool = True


# ---------------------------------------------------------------------------
# IntentConfig — personal / job-seeker-specific configuration loaded from
# config/intent.yaml.  Values from this file override the empty defaults in
# the JobAgentConfig models so that config.yaml stays purely operational.
# ---------------------------------------------------------------------------


class IntentLocation(StrictModel):
    local_area: str = ""
    languages: list[str] = Field(default_factory=list)


class IntentCompanies(StrictModel):
    blacklist: list[str] = Field(default_factory=list)
    whitelist: list[str] = Field(default_factory=list)


class IntentConfig(StrictModel):
    location: IntentLocation = Field(default_factory=IntentLocation)
    companies: IntentCompanies = Field(default_factory=IntentCompanies)


class JobAgentConfig(StrictModel):
    app: AppConfig = Field(default_factory=AppConfig)
    target: TargetConfig = Field(default_factory=TargetConfig)
    multilingual: MultilingualConfig = Field(default_factory=MultilingualConfig)
    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    seeds: SeedsConfig = Field(default_factory=SeedsConfig)
    prompts: PromptConfig = Field(default_factory=PromptConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    run: RunConfig = Field(default_factory=RunConfig)
    crawler: CrawlerConfig = Field(default_factory=CrawlerConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    companies: CompanyFiltersConfig = Field(default_factory=CompanyFiltersConfig)
    exploration: ExplorationConfig = Field(default_factory=ExplorationConfig)
    seeding: SeedingConfig = Field(default_factory=SeedingConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    database_path: Path
    csv_export_path: Path
    jsonl_export_path: Path
    log_path: Path
    profile_path: Path
    seeds_path: Path
    prompts_path: Path


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    config: JobAgentConfig
    paths: RuntimePaths


def _repo_root_from_config_path(config_path: Path) -> Path:
    resolved = config_path.resolve()
    if resolved.parent.name == "config":
        return resolved.parent.parent
    return resolved.parent


def _resolve(base: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (base / path).resolve()


def build_config_from_profile(config: JobAgentConfig, profile_text: str) -> JobAgentConfig:
    """Derive operational config from profile.md and return a new config.

    Assignment map (5 fields derived from profile.md):
        target.roles                    <- target_roles (or fallback)
        matching.preferred_terms        <- positive_terms + target_roles + config.preferred_terms
        matching.avoid_terms            <- avoid_terms + config.avoid_terms
        crawler.job_link_hints          <- defaults + role_signals + search_terms[:60]
        exploration.source_discovery_terms <- config.source_discovery_terms + search_terms[:80]
    """
    config = config.model_copy(deep=True)
    knowledge = extract_profile_knowledge(profile_text)

    target_roles = unique_terms(knowledge.target_roles or config.target.roles)
    role_signals = unique_terms(knowledge.role_signals + target_roles)
    preferred = unique_terms(knowledge.positive_terms + target_roles + config.matching.preferred_terms)
    avoid = unique_terms(knowledge.avoid_terms + config.matching.avoid_terms)
    search_terms = unique_terms(knowledge.search_terms + preferred + role_signals)
    location_terms = unique_terms(config.matching.location_aliases)

    config.target.roles = target_roles
    config.matching.preferred_terms = preferred
    config.matching.avoid_terms = avoid
    config.matching.location_aliases = location_terms
    config.crawler.job_link_hints = unique_terms(default_job_link_hints() + role_signals + search_terms[:60])
    config.exploration.source_discovery_terms = unique_terms(config.exploration.source_discovery_terms + search_terms[:80])
    config.exploration.local_area_terms = unique_terms(config.exploration.local_area_terms + location_terms)
    return config


def load_config(path: str | os.PathLike[str] | None = None) -> LoadedConfig:
    raw_path = path or os.environ.get("JOBAGENT_CONFIG") or "config/config.yaml"
    config_path = Path(raw_path).expanduser().resolve()

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    config = JobAgentConfig.model_validate(raw)
    project_root = _repo_root_from_config_path(config_path)
    intent_path = _resolve(project_root, "config/intent.yaml")

    paths = RuntimePaths(
        database_path=_resolve(project_root, config.app.database_path),
        csv_export_path=_resolve(project_root, config.app.csv_export_path),
        jsonl_export_path=_resolve(project_root, config.app.jsonl_export_path),
        log_path=_resolve(project_root, config.app.log_path),
        profile_path=_resolve(project_root, config.profile.path),
        seeds_path=_resolve(project_root, config.seeds.path),
        prompts_path=_resolve(project_root, config.prompts.path),
    )

    # Load and merge intent.yaml (personal / job-seeker-specific config).
    if intent_path.exists():
        intent_text = intent_path.read_text(encoding="utf-8")
        intent_raw = yaml.safe_load(intent_text) or {}
        intent = IntentConfig.model_validate(intent_raw)
        _merge_intent(config, intent)

    if not paths.profile_path.exists():
        raise FileNotFoundError(f"profile file does not exist: {paths.profile_path}")
    profile_text = paths.profile_path.read_text(encoding="utf-8").strip()
    if not profile_text:
        raise ValueError(f"profile file is empty: {paths.profile_path}")
    config = build_config_from_profile(config, profile_text)
    if not config.target.roles:
        raise ValueError("profile must define at least one target role")
    return LoadedConfig(config=config, paths=paths)


def _merge_intent(config: JobAgentConfig, intent: IntentConfig) -> None:
    """Merge personal intent values into the operational config.

    Only non-empty intent values override the config defaults.
    """
    loc = intent.location
    if loc.local_area:
        config.target.local_area = loc.local_area
    if loc.languages:
        config.target.languages = list(loc.languages)

    comp = intent.companies
    if comp.blacklist:
        config.companies.blacklist = list(comp.blacklist)

    # Wire company whitelist from intent.yaml to bootstrapped search.
    if comp.whitelist:
        config.seeding.bootstrapped_search.company_whitelist = list(comp.whitelist)


def ensure_data_dirs(paths: RuntimePaths) -> None:
    paths.database_path.parent.mkdir(parents=True, exist_ok=True)
    paths.csv_export_path.parent.mkdir(parents=True, exist_ok=True)
    paths.jsonl_export_path.parent.mkdir(parents=True, exist_ok=True)
    paths.log_path.parent.mkdir(parents=True, exist_ok=True)
