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
    # Do not exclude linkedin.com globally: public LinkedIn job search/view URLs
    # may be useful seeds. Login/feed pages are still blocked by safety rules.
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


def default_safety_url_patterns() -> list[str]:
    return [
        r"(?i)/career/(zh|ja|ko|fr|it|es|pt|pl|cs|hu|ru|tr|nl|sv|fi|da|no)([_/]|$)",
        # LinkedIn often redirects public job searches to signup/legal/session pages.
        # They are never useful for a read-only job-detail crawler.
        r"(?i)linkedin\.com/(signup|legal|feed|checkpoint|authwall|uas/login)",
        r"(?i)/signup",
        r"(?i)/login", r"(?i)/signin", r"(?i)/sign-in", r"(?i)/account", r"(?i)/checkout", r"(?i)/cart",
        r"(?i)/apply/submit", r"(?i)/application/submit", r"(?i)/anmelden", r"(?i)/einloggen", r"(?i)/konto",
        r"(?i)/registrieren", r"(?i)/bewerbung/absenden", r"(?i)/bewerben/absenden",
        r"(?i)initiativbewerbung", r"(?i)initativbewerbung", r"(?i)initiative[-_ ]?application",
        r"(?i)speculative[-_ ]?application", r"(?i)unsolicited[-_ ]?application", r"(?i)spontanbewerbung",
        r"(?i)spontaneous[-_ ]?application", r"(?i)talent[-_ ]?pool", r"(?i)talent[-_ ]?community",
        r"(?i)general[-_ ]?application",
    ]


def default_forbidden_link_text_patterns() -> list[str]:
    return [
        r"(?i)^apply now$", r"(?i)^submit application$", r"(?i)^log in$", r"(?i)^sign in$", r"(?i)^register$",
        r"(?i)^jetzt bewerben$", r"(?i)^bewerben$", r"(?i)^bewerbung absenden$", r"(?i)^anmelden$",
        r"(?i)^einloggen$", r"(?i)^registrieren$", r"(?i)initiativbewerbung", r"(?i)initativbewerbung",
        r"(?i)initiative application", r"(?i)speculative application", r"(?i)unsolicited application",
        r"(?i)spontanbewerbung", r"(?i)spontaneous application", r"(?i)talent pool", r"(?i)talent community",
        r"(?i)general application",
    ]


def default_index_url_patterns() -> list[str]:
    return [
        r"(?i)/stellensuche(\.html)?($|[?#])", r"(?i)/job-search(\.html)?($|[?#])",
        r"(?i)/jobs/search($|[/?#])", r"(?i)/jobs/?($|[?#])", r"(?i)/stellenangebote/?($|[?#])",
        r"(?i)/stellenanzeigen/?($|[?#])", r"(?i)/jobs/in-", r"(?i)/jobs/[^/?#]+/in-",
        r"(?i)/career/?$", r"(?i)/careers/?$", r"(?i)/career/.*/home(\.html)?$", r"(?i)/career/.*/standorte/",
        r"(?i)/career/.*/locations/", r"(?i)/standorte/", r"(?i)/locations/",
        # Common job-board result URL shapes such as /jobs/procurement/muenchen.
        # These are useful for exploration but must not be exported as job postings.
        r"(?i)/(jobs|stellenangebote|stellenanzeigen)/[^/?#]+/(münchen|muenchen|munich|erlangen|nuernberg|nürnberg|ingolstadt|augsburg|stuttgart|berlin|jena|oberkochen)(/|$|[?#])",
    ]


def default_index_title_patterns() -> list[str]:
    return [
        r"(?i)jobsuche", r"(?i)job search", r"(?i)karriere(?!.*manager|.*einkauf|.*procurement|.*supplier|.*quality)",
        r"(?i)career(?!.*manager|.*buyer|.*procurement|.*supplier|.*quality)", r"(?i)standort", r"(?i)location",
    ]


def default_detail_url_positive_patterns() -> list[str]:
    return [
        r"(?i)linkedin\.com/jobs/view/",
        r"(?i)indeed\.[^/]+/viewjob\?",
        r"(?i)/jobs/view/",
        r"(?i)/jobs/detail/",
        r"(?i)/job-detail",
        r"(?i)/jobdetail",
        r"(?i)/jobad",
        r"(?i)/stellenangebot/",
        r"(?i)/stellenanzeige/",
        r"(?i)/stellenangebote--",
        r"(?i)/(job|jobs|position|positions|vacancy|opening|stellenangebot|stellenanzeige|stelle)[-/][0-9a-z_-]{8,}",
        r"(?i)/(job|jobs|positions?)/[^/?#]*[0-9][^/?#]*",
        r"(?i)(jobid|job_id|requisition|reqid|jobposting|posting|stellenid|stellennummer|jobnumber|jk)=",
        r"(?i)(greenhouse\.io|lever\.co|workdayjobs\.com|smartrecruiters\.com|personio\.de|join\.com|teamtailor\.com)",
    ]


def default_detail_url_negative_patterns() -> list[str]:
    return default_index_url_patterns() + [
        r"(?i)initiativbewerbung", r"(?i)initativbewerbung", r"(?i)spontaneous[-_ ]application",
        r"(?i)speculative[-_ ]application", r"(?i)unsolicited[-_ ]application", r"(?i)talent[-_ ]pool",
        r"(?i)talent[-_ ]community", r"(?i)spontanbewerbung",
    ]


def default_drop_job_patterns() -> list[str]:
    return [
        r"(?i)initiativbewerbung", r"(?i)initativbewerbung", r"(?i)initiative application",
        r"(?i)speculative application", r"(?i)unsolicited application", r"(?i)spontanbewerbung",
        r"(?i)spontaneous application", r"(?i)talent[-_ ]?pool", r"(?i)talent[-_ ]?community",
        r"(?i)general[-_ ]?application", r"(?i)^apply now$", r"(?i)^jetzt bewerben$", r"(?i)^bewerben$",
        r"(?i)^login$", r"(?i)^sign in$",
    ]





class AppConfig(StrictModel):
    name: str = "jobagent-local"
    data_dir: str = "data"
    database_path: str = "data/jobs.sqlite"
    csv_export_path: str = "data/jobs.csv"
    jsonl_export_path: str = "data/jobs.jsonl"
    log_path: str = "data/jobagent.log"
    user_agent: str = "JobMatchAgent/0.1 (+local personal job search; read-only)"


class TargetConfig(StrictModel):
    # local_area is filled from config/intent.yaml on startup.
    local_area: str = ""
    roles: list[str] = Field(default_factory=list)
    include_remote: bool = True
    # languages is filled from config/intent.yaml on startup.
    languages: list[str] = Field(default_factory=list)

    @field_validator("roles")
    @classmethod
    def clean_list(cls, value: list[str]) -> list[str]:
        return [x.strip() for x in value if str(x).strip()]


class MultilingualConfig(StrictModel):
    enabled: bool = True
    primary_market_language: str = "German"
    accepted_languages: list[str] = Field(default_factory=lambda: ["German", "English"])
    output_language: str = "English"
    keep_original_job_titles: bool = True
    treat_mixed_language_as_normal: bool = True
    query_language_modes: list[str] = Field(default_factory=lambda: ["German", "English", "mixed German-English"])
    german_role_terms: list[str] = Field(default_factory=list)
    english_role_terms: list[str] = Field(default_factory=list)
    mixed_role_terms: list[str] = Field(default_factory=list)
    german_job_terms: list[str] = Field(default_factory=lambda: ["Stelle", "Stellenangebote", "Offene Stellen", "Jobsuche", "Karriere", "Bewerbungsportal", "Festanstellung", "Vollzeit", "Homeoffice"])
    english_job_terms: list[str] = Field(default_factory=lambda: ["job", "jobs", "open role", "opening", "vacancy", "full-time", "part-time", "remote", "hybrid"])
    german_career_terms: list[str] = Field(default_factory=lambda: ["Karriere", "Karriereseite", "Arbeiten bei uns", "Jobsuche", "Bewerbungsportal", "Einstieg"])
    english_career_terms: list[str] = Field(default_factory=lambda: ["career", "careers", "career site", "join us", "work with us", "recruiting", "hiring"])

    @field_validator("accepted_languages", "query_language_modes")
    @classmethod
    def non_empty_language_list(cls, value: list[str]) -> list[str]:
        cleaned = [x.strip() for x in value if str(x).strip()]
        if not cleaned:
            raise ValueError("list must contain at least one non-empty value")
        return cleaned

    @field_validator("german_role_terms", "english_role_terms", "mixed_role_terms", "german_job_terms", "english_job_terms", "german_career_terms", "english_career_terms")
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
    stop_run_on_connection_error: bool = True
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    response_format_type: str = "json_object"
    thinking_enabled: bool = True
    context_window_tokens: int = Field(default=150000, gt=0)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_budget_keys(cls, raw: object) -> object:
        if not isinstance(raw, dict):
            return raw
        data = dict(raw)
        if "output_tokens" not in data:
            if "max_response_tokens" in data:
                data["output_tokens"] = data["max_response_tokens"]
            elif "max_tokens" in data:
                data["output_tokens"] = data["max_tokens"]
        if "thinking_enabled" not in data and "disable_thinking" in data:
            data["thinking_enabled"] = not bool(data["disable_thinking"])
        for key in (
            "max_response_tokens", "max_tokens", "disable_thinking", "no_think_prefix",
            "max_prompt_tokens", "prompt_safety_margin_tokens", "token_estimate_chars_per_token",
            "max_profile_chars", "max_memory_chars", "max_page_text_chars_for_prompt",
            "min_page_text_chars_for_prompt", "max_candidate_links_for_prompt",
            "min_candidate_links_for_prompt", "max_candidate_link_text_chars", "max_candidate_link_url_chars",
            "output_tokens",
        ):
            data.pop(key, None)
        return data

    @property
    def disable_thinking(self) -> bool:
        return not self.thinking_enabled

    @property
    def no_think_prefix(self) -> str:
        return "/no_think" if not self.thinking_enabled else ""

    @property
    def token_estimate_chars_per_token(self) -> float:
        return 4.0

    @property
    def prompt_safety_margin_tokens(self) -> int:
        return max(350, min(1200, int(self.context_window_tokens * 0.05)))

    @property
    def max_prompt_tokens(self) -> int:
        return self.context_window_tokens - self.prompt_safety_margin_tokens

    @model_validator(mode="after")
    def validate_prompt_budget(self) -> "LLMConfig":
        if self.max_prompt_tokens <= 500:
            raise ValueError("llm.context_window_tokens is too small")
        return self


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
    debug_mode: bool = False

    @model_validator(mode="after")
    def validate_delay_range(self) -> "RunConfig":
        if self.min_delay_seconds > self.max_delay_seconds:
            raise ValueError("run.min_delay_seconds must be <= run.max_delay_seconds")
        return self


class CrawlerConfig(StrictModel):
    allowed_schemes: list[str] = Field(default_factory=default_allowed_schemes)
    excluded_domain_substrings: list[str] = Field(default_factory=default_excluded_domains)
    excluded_file_extensions: list[str] = Field(default_factory=default_file_extensions)
    dedupe_url_tracking_params: list[str] = Field(default_factory=default_tracking_params)
    job_link_hints: list[str] = Field(default_factory=default_job_link_hints)
    source_discovery_terms: list[str] = Field(default_factory=list)
    retry_error_pages: bool = True
    max_compact_lines: int = Field(default=180, gt=0)
    max_important_lines: int = Field(default=240, gt=0)
    max_page_text_chars: int = Field(default=14000, gt=0)
    min_body_chars_to_analyze: int = Field(default=150, ge=0)
    max_pages_per_source_key: int = Field(default=25, gt=0)
    batch_size_for_llm: int = Field(default=30, gt=0)
    max_page_context_chars: int = Field(default=5000, gt=0)


class SafetyConfig(StrictModel):
    crawl_only_public_pages: bool = True
    allow_login_pages: bool = False
    allow_form_submit: bool = False
    deny_url_patterns: list[str] = Field(default_factory=default_safety_url_patterns)
    forbidden_link_text_patterns: list[str] = Field(default_factory=default_forbidden_link_text_patterns)


class JobValidationConfig(StrictModel):
    enabled: bool = True
    # true means CSV rows are saved only after the agent has loaded the concrete
    # job-detail page itself. Overview/listing pages may only contribute follow URLs.
    require_loaded_job_detail_page: bool = True
    enforce_llm_urls_from_page: bool = True
    allow_current_page_as_job_url: bool = True
    current_page_url_must_look_like_detail: bool = True
    drop_if_url_is_index_page: bool = True
    drop_if_title_is_location: bool = True
    drop_if_company_blacklisted: bool = True
    drop_if_title_or_url_matches: list[str] = Field(default_factory=default_drop_job_patterns)
    index_url_patterns: list[str] = Field(default_factory=default_index_url_patterns)
    index_title_patterns: list[str] = Field(default_factory=default_index_title_patterns)


class MatchingConfig(StrictModel):
    location_aliases: list[str] = Field(default_factory=list)
    preferred_terms: list[str] = Field(default_factory=list)
    avoid_terms: list[str] = Field(default_factory=list)


class ScoringConfig(StrictModel):
    min_score_to_export: int = Field(default=55, ge=0, le=100,
        description="Minimum fit score (0-100) for a job to be exported/saved. Jobs below this are dropped.")
    high_fit_score_threshold: int = Field(default=80, ge=0, le=100,
        description="Fit score threshold (0-100) that triggers a high-fit bonus when a job meets or exceeds it. Used to boost source quality.")


class MemoryConfig(StrictModel):
    source_key_mode: Literal["domain", "domain_path1", "domain_path2"] = "domain_path1"
    initial_score: float = 50.0
    min_score: float = 0.0
    max_score: float = 100.0
    reward_job_found: float = 6.0
    reward_high_fit_job: float = 8.0
    reward_source_quality: float = 4.0
    penalty_no_job: float = -1.5
    penalty_error: float = -5.0
    penalty_bad_source_quality: float = -2.5
    no_job_streak_penalty_after: int = Field(default=4, ge=0)
    top_sources_in_prompt: int = Field(default=8, ge=0)
    bad_sources_in_prompt: int = Field(default=8, ge=0)
    recent_jobs_in_prompt: int = Field(default=8, ge=0)
    decay_per_run: float = Field(default=0.98, gt=0.0, le=1.0)
    blacklist_below_score: float = 8.0
    trusted_above_score: float = 75.0

    @model_validator(mode="after")
    def validate_score_range(self) -> "MemoryConfig":
        if self.min_score > self.max_score:
            raise ValueError("memory.min_score must be <= memory.max_score")
        if not (self.min_score <= self.initial_score <= self.max_score):
            raise ValueError("memory.initial_score must be inside min/max range")
        return self


class ExplorationConfig(StrictModel):
    enabled: bool = True
    candidate_url_limit_per_search_page: int = Field(default=30, gt=0)
    job_portal_domain_substrings: list[str] = Field(default_factory=lambda: ["linkedin.com/jobs", "indeed.", "stepstone.", "stellenanzeigen.", "xing.com/jobs", "monster.", "kimeta.", "jobs.de", "jobvector.", "yourfirm.", "glassdoor."])
    local_area_terms: list[str] = Field(default_factory=list)
    source_discovery_terms: list[str] = Field(default_factory=list)


class BootstrappedSearchConfig(StrictModel):
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
    show_urls: bool = True
    max_url_chars: int = Field(default=180, gt=0)
    max_title_chars: int = Field(default=120, gt=0)
    max_notes_chars: int = Field(default=220, gt=0)


# ---------------------------------------------------------------------------
# IntentConfig — personal / job-seeker-specific configuration loaded from
# config/intent.yaml.  Values from this file override the empty defaults in
# the JobAgentConfig models so that config.yaml stays purely operational.
# ---------------------------------------------------------------------------


class IntentLocation(StrictModel):
    local_area: str = ""
    target_city: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    radius_km: float = 0.0
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
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    job_validation: JobValidationConfig = Field(default_factory=JobValidationConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    companies: CompanyFiltersConfig = Field(default_factory=CompanyFiltersConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    exploration: ExplorationConfig = Field(default_factory=ExplorationConfig)
    seeding: SeedingConfig = Field(default_factory=SeedingConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    intent: IntentConfig = Field(default_factory=IntentConfig)


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    project_root: Path
    config_path: Path
    data_dir: Path
    database_path: Path
    csv_export_path: Path
    jsonl_export_path: Path
    log_path: Path
    profile_path: Path
    seeds_path: Path
    prompts_path: Path
    intent_path: Path


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

    Assignment map (14 fields derived from profile.md):
        target.roles                    <- target_roles (or fallback)
        matching.preferred_terms        <- positive_terms + target_roles + config.preferred_terms
        matching.avoid_terms            <- avoid_terms + config.avoid_terms
        matching.location_aliases       <- config.location_aliases
        crawler.job_link_hints          <- defaults + role_signals + search_terms[:60]
        crawler.source_discovery_terms  <- config.source_discovery_terms + search_terms[:80]
        exploration.source_discovery_terms <- config.source_discovery_terms + search_terms[:80]
        exploration.local_area_terms    <- config.local_area_terms + location_terms
        multilingual.german_role_terms  <- config.german_role_terms + German role_signals
        multilingual.english_role_terms <- config.english_role_terms + non-German role_signals
        multilingual.mixed_role_terms   <- config.mixed_role_terms + target_roles
    """
    config = config.model_copy(deep=True)
    knowledge = extract_profile_knowledge(profile_text)

    target_roles = unique_terms(knowledge.target_roles or config.target.roles)
    role_signals = unique_terms(knowledge.role_signals + target_roles)
    preferred = unique_terms(knowledge.positive_terms + target_roles + config.matching.preferred_terms)
    avoid = unique_terms(knowledge.avoid_terms + config.matching.avoid_terms)
    search_terms = unique_terms(knowledge.search_terms + preferred + role_signals)
    location_terms = unique_terms(config.matching.location_aliases)

    config.target.roles = target_roles or ["profile-defined target role"]
    config.matching.preferred_terms = preferred
    config.matching.avoid_terms = avoid
    config.matching.location_aliases = location_terms
    config.crawler.job_link_hints = unique_terms(default_job_link_hints() + role_signals + search_terms[:60])
    config.crawler.source_discovery_terms = unique_terms(config.crawler.source_discovery_terms + search_terms[:80])
    config.exploration.source_discovery_terms = unique_terms(config.exploration.source_discovery_terms + search_terms[:80])
    config.exploration.local_area_terms = unique_terms(config.exploration.local_area_terms + location_terms)
    config.multilingual.german_role_terms = unique_terms(config.multilingual.german_role_terms + [t for t in role_signals if any(ch in t for ch in "äöüÄÖÜß") or any(w in t.casefold() for w in ("einkauf", "beschaffung", "lieferanten", "qualität", "qualitaet"))])
    config.multilingual.english_role_terms = unique_terms(config.multilingual.english_role_terms + [t for t in role_signals if t not in config.multilingual.german_role_terms])
    config.multilingual.mixed_role_terms = unique_terms(config.multilingual.mixed_role_terms + target_roles)
    return config


def load_config(path: str | os.PathLike[str] | None = None) -> LoadedConfig:
    raw_path = path or os.environ.get("JOBAGENT_CONFIG") or "config/config.yaml"
    config_path = Path(raw_path).expanduser().resolve()

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    config = JobAgentConfig.model_validate(raw)
    project_root = _repo_root_from_config_path(config_path)

    paths = RuntimePaths(
        project_root=project_root,
        config_path=config_path,
        data_dir=_resolve(project_root, config.app.data_dir),
        database_path=_resolve(project_root, config.app.database_path),
        csv_export_path=_resolve(project_root, config.app.csv_export_path),
        jsonl_export_path=_resolve(project_root, config.app.jsonl_export_path),
        log_path=_resolve(project_root, config.app.log_path),
        profile_path=_resolve(project_root, config.profile.path),
        seeds_path=_resolve(project_root, config.seeds.path),
        prompts_path=_resolve(project_root, config.prompts.path),
        intent_path=_resolve(project_root, "config/intent.yaml"),
    )

    # Load and merge intent.yaml (personal / job-seeker-specific config).
    if paths.intent_path.exists():
        intent_text = paths.intent_path.read_text(encoding="utf-8")
        intent_raw = yaml.safe_load(intent_text) or {}
        intent = IntentConfig.model_validate(intent_raw)
        _merge_intent(config, intent)

    profile_text = paths.profile_path.read_text(encoding="utf-8").strip() if paths.profile_path.exists() else ""
    config = build_config_from_profile(config, profile_text)
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
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.database_path.parent.mkdir(parents=True, exist_ok=True)
    paths.csv_export_path.parent.mkdir(parents=True, exist_ok=True)
    paths.jsonl_export_path.parent.mkdir(parents=True, exist_ok=True)
    paths.log_path.parent.mkdir(parents=True, exist_ok=True)
