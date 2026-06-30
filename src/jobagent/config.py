from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .profile_knowledge import extract_profile_knowledge, regexes_from_terms, unique_terms


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


def default_career_paths() -> list[str]:
    return [
        "/careers", "/career", "/jobs", "/en/careers", "/en/jobs", "/join-us",
        "/work-with-us", "/karriere", "/de/karriere", "/de/jobs", "/de/stellenangebote",
        "/stellenangebote", "/offene-stellen", "/jobsuche", "/arbeiten-bei-uns",
        "/de/arbeiten-bei-uns", "/de/karriere/stellenangebote", "/karriere/stellenangebote",
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


def default_weak_location_terms() -> list[str]:
    return ["worldwide", "global", "various", "multiple locations", "mehrere standorte", "worldwide locations"]


def default_remote_terms() -> list[str]:
    return [
        "remote", "homeoffice", "home office", "mobiles arbeiten", "hybrid", "work from home",
        "remote deutschland", "germany remote", "deutschland remote", "bundesweit remote", "remote innerhalb deutschlands",
    ]


def default_broad_location_terms() -> list[str]:
    return ["Bayern", "Bavaria", "Oberbayern", "Germany", "Deutschland", "DACH", "Europe", "Europa", "EMEA"]


def default_search_url_templates() -> list[str]:
    # Avoid generic search engines by default. In local crawling they frequently
    # return 403/CAPTCHA pages and waste pages. Job portals are still allowed.
    return [
        "https://www.linkedin.com/jobs/search/?keywords={query}&location=Munich%2C%20Bavaria%2C%20Germany",
        "https://www.stepstone.de/jobs/{query_slug}/in-m%C3%BCnchen",
        "https://www.stellenanzeigen.de/jobs/{query_slug}/muenchen",
    ]


def default_whitelist_job_portal_search_templates() -> list[str]:
    return [
        "https://www.linkedin.com/jobs/search/?keywords={query}&location=Munich%2C%20Bavaria%2C%20Germany",
        "https://www.stepstone.de/jobs/{query_slug}/in-m%C3%BCnchen",
    ]


def default_career_page_search_templates() -> list[str]:
    # Empty by default because public search engines often return 403/CAPTCHA pages
    # in unattended crawls. Add an allowed search endpoint here, for example a
    # self-hosted SearxNG instance or a paid search API wrapper that returns HTML.
    return []


def default_company_domain_suffixes() -> list[str]:
    return [".com", ".de", ".net"]


def default_known_company_domains() -> dict[str, list[str]]:
    # This is not a seed list. It is company metadata used to derive career-page
    # candidates generically from the whitelist. Users can add/remove entries
    # without touching config/seeds.txt.
    return {
        "ZEISS": ["zeiss.com"],
        "TRUMPF": ["trumpf.com", "trumpf.wd3.myworkdayjobs.com"],
        "Rohde & Schwarz": ["rohde-schwarz.com"],
        "HENSOLDT": ["hensoldt.net", "hensoldt.com"],
        "OSRAM": ["ams-osram.com", "osram.com"],
        "Airbus Defence & Space": ["airbus.com"],
        "Coherent": ["coherent.com"],
        "SUSS MicroTec": ["suss.com", "sussmicrotec.com"],
        "TOPTICA Photonics": ["toptica.com"],
        "Blickfeld": ["blickfeld.com"],
        "Marvel Fusion": ["marvelfusion.com"],
        "BMW": ["bmwgroup.jobs", "bmw.com", "bmwgroup.com"],
        "Isar Aerospace": ["isaraerospace.com"],
        "Rheinmetall": ["rheinmetall.com"],
    }


def default_company_portal_role_terms() -> list[str]:
    return [
        "Procurement Manager",
        "Purchasing Manager",
        "Strategic Buyer",
        "Technical Buyer",
        "Supply Chain Manager",
        "Supplier Quality Manager",
        "Einkauf",
        "Strategischer Einkauf",
        "Technischer Einkäufer",
        "Lieferantenqualität",
    ]


def default_city_coordinates() -> dict[str, tuple[float, float]]:
    # Includes Munich-area towns plus common German cities that should be recognized as outside the 30 km default radius.
    return {
        "München": (48.137154, 11.576124), "Munich": (48.137154, 11.576124), "Muenchen": (48.137154, 11.576124),
        "Garching": (48.248872, 11.65198), "Garching bei München": (48.248872, 11.65198), "Unterföhring": (48.192999, 11.642889),
        "Unterfoehring": (48.192999, 11.642889), "Ismaning": (48.226397, 11.672927), "Oberschleißheim": (48.250455, 11.55578),
        "Oberschleissheim": (48.250455, 11.55578), "Unterschleißheim": (48.280459, 11.576164), "Unterschleissheim": (48.280459, 11.576164),
        "Dachau": (48.259114, 11.434858), "Karlsfeld": (48.22676, 11.47503), "Puchheim": (48.171686, 11.350229),
        "Germering": (48.133333, 11.366667), "Gräfelfing": (48.118778, 11.429394), "Graefelfing": (48.118778, 11.429394),
        "Planegg": (48.10679, 11.424827), "Martinsried": (48.108955, 11.450703), "Krailling": (48.1, 11.4),
        "Gauting": (48.069169, 11.377431), "Starnberg": (47.999008, 11.339534), "Fürstenfeldbruck": (48.179044, 11.2547),
        "Fuerstenfeldbruck": (48.179044, 11.2547), "Gröbenzell": (48.194297, 11.374535), "Groebenzell": (48.194297, 11.374535),
        "Olching": (48.2, 11.333333), "Maisach": (48.216667, 11.266667), "Gilching": (48.106613, 11.293669),
        "Ottobrunn": (48.064889, 11.663844), "Unterhaching": (48.065979, 11.61564), "Taufkirchen": (48.048741, 11.617152),
        "Neubiberg": (48.077872, 11.65812), "Haar": (48.108137, 11.726376), "Vaterstetten": (48.105625, 11.768334),
        "Putzbrunn": (48.075, 11.716667), "Feldkirchen": (48.148, 11.732), "Kirchheim bei München": (48.176117, 11.755409),
        "Aschheim": (48.171, 11.716), "Poing": (48.170278, 11.818611),
        "Erding": (48.306389, 11.906944), "Freising": (48.40288, 11.74122), "Landshut": (48.544191, 12.146853),
        "Augsburg": (48.370545, 10.89779), "Ingolstadt": (48.766535, 11.425754), "Regensburg": (49.013432, 12.101624),
        "Erlangen": (49.589674, 11.011961), "Rosenheim": (47.85637, 12.12247), "Nürnberg": (49.452103, 11.076665),
        "Nuernberg": (49.452103, 11.076665), "Nuremberg": (49.452103, 11.076665), "Stuttgart": (48.775846, 9.182932),
        "Ulm": (48.401082, 9.987608), "Aalen": (48.83777, 10.0933), "Oberkochen": (48.786279, 10.105847),
        "Jena": (50.927054, 11.589237), "Berlin": (52.520008, 13.404954), "Dresden": (51.050409, 13.737262),
        "Leipzig": (51.339695, 12.373075), "Hamburg": (53.551086, 9.993682), "Köln": (50.937531, 6.960279),
        "Koeln": (50.937531, 6.960279), "Frankfurt": (50.110924, 8.682127), "Wetzlar": (50.55898, 8.50365),
        "Braunschweig": (52.268874, 10.52677), "Göttingen": (51.54128, 9.915803), "Goettingen": (51.54128, 9.915803),
        "Aachen": (50.775346, 6.083887), "Rossdorf": (49.859722, 8.761667), "Roßdorf": (49.859722, 8.761667),
        "Neubeuern": (47.773343, 12.140356),
        # Common out-of-radius cities frequently exposed by German job portals.
        # Keeping them here lets the crawler skip irrelevant detail URLs before
        # opening a browser page.
        "Pforzheim": (48.892186, 8.694629), "Marbach am Neckar": (48.93964, 9.25995),
        "Marbach": (48.93964, 9.25995), "Höfen an der Enz": (48.80016, 8.58556),
        "Hoefen an der Enz": (48.80016, 8.58556), "Hofen Enz": (48.80016, 8.58556),
        "Heppenheim": (49.64306, 8.63889), "Heppenheim Bergstraße": (49.64306, 8.63889),
        "Heppenheim Bergstrasse": (49.64306, 8.63889), "Schongau": (47.81240, 10.89664),
        "Buchloe": (48.03719, 10.72548), "Fridolfing": (47.99776, 12.82629),
        "Memmingen": (47.98372, 10.18527), "Dasing": (48.38400, 11.04630),
        "Rheinau": (48.66028, 7.93694), "Rheinau Baden": (48.66028, 7.93694),
        "Roggwil": (47.24118, 7.82141), "Roggwil Schweiz": (47.24118, 7.82141),
    }


def default_bootstrap_templates() -> list[str]:
    return [
        "{roles} jobs {location} careers",
        "{roles} {location} job openings",
        "{roles} {location} greenhouse lever workday personio",
        "{role_terms} Stellenangebote {location_terms}",
        "{role_terms} Karriere {location_terms}",
        "{expertise_terms} {location_terms} jobs",
        "{expertise_terms} {location_terms} Karriere",
        "site:greenhouse.io {roles} {location}",
        "site:lever.co {roles} {location}",
        "site:personio.de {roles} {location}",
        "site:join.com {roles} {location}",
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
    local_area: str = "Munich, Germany"
    roles: list[str] = Field(default_factory=list)
    include_remote: bool = True
    languages: list[str] = Field(default_factory=lambda: ["German", "English"])

    @field_validator("roles", "languages")
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
    output_tokens: int = Field(default=5000, gt=0)
    response_format_type: str = "json_object"
    thinking_enabled: bool = True
    context_window_tokens: int = Field(default=12000, gt=0)

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
        ):
            data.pop(key, None)
        return data

    @property
    def max_tokens(self) -> int:
        return self.output_tokens

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
        return self.context_window_tokens - self.output_tokens - self.prompt_safety_margin_tokens

    @property
    def max_profile_chars(self) -> int:
        return min(9000, max(3500, int(self.max_prompt_tokens * 1.0)))

    @property
    def max_memory_chars(self) -> int:
        return min(5000, max(1400, int(self.max_prompt_tokens * 0.45)))

    @property
    def max_page_text_chars_for_prompt(self) -> int:
        return min(22000, max(6500, int(self.max_prompt_tokens * 2.0)))

    @property
    def min_page_text_chars_for_prompt(self) -> int:
        return min(2500, max(900, int(self.max_prompt_tokens * 0.22)))

    @property
    def max_candidate_links_for_prompt(self) -> int:
        return min(70, max(25, int(self.max_prompt_tokens / 180)))

    @property
    def min_candidate_links_for_prompt(self) -> int:
        return min(10, max(5, int(self.max_candidate_links_for_prompt * 0.2)))

    @property
    def max_candidate_link_text_chars(self) -> int:
        return 110

    @property
    def max_candidate_link_url_chars(self) -> int:
        return 420

    @model_validator(mode="after")
    def validate_prompt_budget(self) -> "LLMConfig":
        if self.max_prompt_tokens <= 500:
            raise ValueError("llm.context_window_tokens must be much larger than llm.output_tokens")
        return self


class BrowserConfig(StrictModel):
    engine: Literal["chromium", "firefox", "webkit"] = "chromium"
    headless: bool = True
    viewport_width: int = Field(default=1365, gt=0)
    viewport_height: int = Field(default=900, gt=0)
    navigation_timeout_ms: int = Field(default=30000, gt=0)
    body_text_timeout_ms: int = Field(default=7000, gt=0)
    network_idle_timeout_ms: int = Field(default=800, ge=0)
    wait_until: str = "domcontentloaded"
    fail_on_http_error_statuses: bool = True
    http_error_status_min: int = Field(default=400, ge=300, le=599)


class RunConfig(StrictModel):
    reset_frontier_on_start: bool = True
    max_pages: int = Field(default=80, gt=0)
    max_depth: int = Field(default=3, ge=0)
    min_delay_seconds: float = Field(default=0.2, ge=0)
    max_delay_seconds: float = Field(default=0.8, ge=0)
    export_after_run: bool = True
    export_after_each_page: bool = True
    export_on_interrupt: bool = True

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
    career_path_candidates: list[str] = Field(default_factory=default_career_paths)
    respect_robots_txt: bool = False
    strict_robots_when_unavailable: bool = False
    retry_previously_blocked_when_robots_disabled: bool = True
    retry_error_pages: bool = True
    robots_timeout_seconds: int = Field(default=8, gt=0)
    max_links_per_page_for_llm: int = Field(default=80, gt=0)
    max_raw_links_retained: int = Field(default=400, gt=0)
    max_compact_lines: int = Field(default=180, gt=0)
    max_important_lines: int = Field(default=240, gt=0)
    max_page_text_chars: int = Field(default=14000, gt=0)
    min_body_chars_to_analyze: int = Field(default=150, ge=0)
    max_pages_per_source_key: int = Field(default=25, gt=0)
    max_career_domain_expansions_per_page: int = Field(default=4, ge=0)


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


class LocationRadiusConfig(StrictModel):
    enabled: bool = True
    target_city: str = "Munich"
    target_country_terms: list[str] = Field(default_factory=lambda: ["Germany", "Deutschland", "DE", "DACH"])
    latitude: float = 48.137154
    longitude: float = 11.576124
    radius_km: float = Field(default=30.0, gt=0)
    hard_drop_outside_radius: bool = True
    require_location_for_non_remote: bool = True
    allow_remote_if_country_match: bool = True
    filter_exploration_urls: bool = True
    drop_urls_with_outside_city: bool = True
    allowed_country_url_segments: list[str] = Field(default_factory=lambda: ["de", "de-de", "de_de", "deutschland", "germany"])
    blocked_country_url_segments: list[str] = Field(default_factory=lambda: ["at", "ch", "cn", "tw", "zh_cn", "zh_tw", "fr", "it", "es", "nl", "pl", "cz"])
    remote_terms: list[str] = Field(default_factory=default_remote_terms)
    broad_location_terms: list[str] = Field(default_factory=default_broad_location_terms)
    unknown_location_cap: int = Field(default=44, ge=0, le=100)
    outside_radius_cap: int = Field(default=29, ge=0, le=100)
    city_coordinates: dict[str, tuple[float, float]] = Field(default_factory=default_city_coordinates)

    @field_validator("target_country_terms", "remote_terms", "broad_location_terms", "allowed_country_url_segments", "blocked_country_url_segments")
    @classmethod
    def clean_location_terms(cls, value: list[str]) -> list[str]:
        return [x.strip() for x in value if str(x).strip()]


class MatchingConfig(StrictModel):
    min_fit_score_to_save: int = Field(default=55, ge=0, le=100)
    high_fit_score: int = Field(default=80, ge=0, le=100)
    location_aliases: list[str] = Field(default_factory=list)
    preferred_terms: list[str] = Field(default_factory=list)
    avoid_terms: list[str] = Field(default_factory=list)


class ScoreConsistencyConfig(StrictModel):
    enabled: bool = True
    require_target_role_signal: bool = True
    target_role_terms: list[str] = Field(default_factory=list)
    strong_fit_terms: list[str] = Field(default_factory=list)
    adjacent_role_terms: list[str] = Field(default_factory=list)
    irrelevant_role_patterns: list[str] = Field(default_factory=list)
    protected_relevant_patterns: list[str] = Field(default_factory=list)
    no_target_role_cap: int = Field(default=34, ge=0, le=100)
    irrelevant_role_cap: int = Field(default=29, ge=0, le=100)
    avoid_term_cap: int = Field(default=39, ge=0, le=100)
    unclear_location_cap: int = Field(default=44, ge=0, le=100)
    outside_radius_cap: int = Field(default=29, ge=0, le=100)
    unknown_location_cap: int = Field(default=44, ge=0, le=100)
    initiative_application_cap: int = Field(default=25, ge=0, le=100)
    weak_location_terms: list[str] = Field(default_factory=default_weak_location_terms)
    min_evidence_chars_for_llm_job: int = Field(default=8, ge=0)
    min_reason_chars_for_llm_job: int = Field(default=12, ge=0)


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
    penalty_blocked_by_robots: float = -3.0
    penalty_bad_source_quality: float = -2.5
    no_job_streak_penalty_after: int = Field(default=4, ge=0)
    priority_weight_memory: float = 1.0
    priority_weight_depth: float = 7.0
    priority_weight_hint: float = 2.5
    priority_random_jitter: float = Field(default=2.0, ge=0)
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
    # both: whitelist company searches plus exploratory search/query generation.
    # whitelist_only: only focused whitelist-company search queries plus seed URLs.
    # exploratory_only: generic/exploratory search, no whitelist-company query seeding.
    mode: Literal["both", "whitelist_only", "exploratory_only"] = "both"
    seed_search_when_empty: bool = True
    max_generated_queries_per_run: int = Field(default=6, ge=0)
    query_generation_every_pages: int = Field(default=12, gt=0)
    generated_query_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    candidate_url_limit_per_search_page: int = Field(default=30, gt=0)
    require_role_signal_for_job_detail_urls: bool = True
    require_role_signal_for_human_readable_company_job_urls: bool = True
    drop_avoid_only_job_detail_urls: bool = True
    max_follow_urls_without_llm: int = Field(default=0, ge=0)
    job_portal_domain_substrings: list[str] = Field(default_factory=lambda: ["linkedin.com/jobs", "indeed.", "stepstone.", "stellenanzeigen.", "xing.com/jobs", "monster.", "kimeta.", "jobs.de", "jobvector.", "yourfirm.", "glassdoor."])
    bootstrap_query_templates: list[str] = Field(default_factory=default_bootstrap_templates)
    search_url_templates: list[str] = Field(default_factory=default_search_url_templates)
    whitelist_job_portal_search_templates: list[str] = Field(default_factory=default_whitelist_job_portal_search_templates)
    local_area_terms: list[str] = Field(default_factory=list)
    source_discovery_terms: list[str] = Field(default_factory=list)


class HeuristicExtractionConfig(StrictModel):
    enabled: bool = False
    use_structured_data: bool = True
    use_candidate_links: bool = True
    max_jobs_per_page: int = Field(default=16, gt=0)
    require_detail_url_for_link_jobs: bool = True
    suppress_link_jobs_on_index_pages: bool = True
    index_page_title_patterns: list[str] = Field(default_factory=default_index_title_patterns)
    index_page_url_patterns: list[str] = Field(default_factory=default_index_url_patterns)
    detail_url_positive_patterns: list[str] = Field(default_factory=default_detail_url_positive_patterns)
    detail_url_negative_patterns: list[str] = Field(default_factory=default_detail_url_negative_patterns)
    structured_base_score: int = Field(default=55, ge=0, le=100)
    link_base_score: int = Field(default=42, ge=0, le=100)
    role_bonus: int = Field(default=18, ge=0, le=100)
    location_bonus: int = Field(default=20, ge=0, le=100)
    preferred_term_bonus: int = Field(default=8, ge=0, le=100)
    remote_bonus: int = Field(default=5, ge=0, le=100)
    avoid_term_penalty: int = Field(default=25, ge=0, le=100)
    max_score: int = Field(default=86, ge=0, le=100)


class CompanyFiltersConfig(StrictModel):
    # Names in blacklist drop matched jobs. Names in whitelist generate focused
    # company-specific search queries during seeding. Put exact company names here;
    # direct career URLs still belong in config/seeds.txt.
    blacklist: list[str] = Field(default_factory=list)
    whitelist: list[str] = Field(default_factory=list)
    whitelist_search_when_seeding: bool = True
    enforce_whitelist_in_whitelist_only: bool = True
    # Direct company career-page discovery is derived from these domains plus
    # standard career paths. This keeps whitelist-only mode focused without
    # stuffing company URLs into config/seeds.txt.
    known_domains: dict[str, list[str]] = Field(default_factory=default_known_company_domains)
    # false by default: inferred domains such as airbusdefencespace.com create many
    # 404s. Keep company-domain metadata explicit and editable.
    infer_domains_from_company_names: bool = False
    inferred_domain_suffixes: list[str] = Field(default_factory=default_company_domain_suffixes)

    # How direct company entrypoints are generated:
    #   root_only                 = https://domain only; the crawler then follows visible career links.
    #   root_plus_configured_paths = root plus crawler.career_path_candidates; faster but can cause 404 storms.
    direct_career_discovery: Literal["root_only", "root_plus_configured_paths"] = "root_only"
    max_direct_career_urls_per_company: int = Field(default=3, ge=0)

    # Optional search route for finding official career pages without brute-forcing
    # paths. Use a permitted endpoint, e.g. self-hosted SearxNG. Empty by default.
    career_page_search_templates: list[str] = Field(default_factory=default_career_page_search_templates)
    max_career_page_searches_per_company: int = Field(default=2, ge=0)

    # Company+role job-portal searches use simple portal syntax, not Google-style
    # OR expressions, because LinkedIn/StepStone generally treat OR/parentheses as text.
    portal_role_terms: list[str] = Field(default_factory=default_company_portal_role_terms)
    max_portal_role_terms_per_company: int = Field(default=6, ge=0)
    max_search_queries_per_company: int = Field(default=0, ge=0)

    @field_validator("blacklist", "whitelist", "portal_role_terms", "inferred_domain_suffixes", "career_page_search_templates")
    @classmethod
    def clean_company_names(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            name = str(item).strip()
            key = name.casefold()
            if name and key not in seen:
                seen.add(key)
                out.append(name)
        return out

    @field_validator("known_domains")
    @classmethod
    def clean_known_domains(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for company, domains in (value or {}).items():
            cname = str(company).strip()
            if not cname:
                continue
            clean_domains: list[str] = []
            seen: set[str] = set()
            for domain in domains or []:
                d = str(domain).strip().lower()
                d = d.replace("https://", "").replace("http://", "").strip("/")
                if d and d not in seen:
                    seen.add(d)
                    clean_domains.append(d)
            if clean_domains:
                out[cname] = clean_domains
        return out


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
    location_radius: LocationRadiusConfig = Field(default_factory=LocationRadiusConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    score_consistency: ScoreConsistencyConfig = Field(default_factory=ScoreConsistencyConfig)
    companies: CompanyFiltersConfig = Field(default_factory=CompanyFiltersConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    exploration: ExplorationConfig = Field(default_factory=ExplorationConfig)
    heuristic_extraction: HeuristicExtractionConfig = Field(default_factory=HeuristicExtractionConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


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


def _location_terms(config: JobAgentConfig) -> list[str]:
    city = config.location_radius.target_city
    local = config.target.local_area
    return unique_terms(
        [
            local, city, "Munich", "München", "Muenchen", "Greater Munich", "Greater Munich Area", "Raum München",
            "München Umgebung", "Munich area", "Metropolregion München", "Hybrid Munich", "Hybrid München",
            "Germany remote", "Remote Germany", "Deutschland remote", "Homeoffice Deutschland", "Remote innerhalb Deutschlands",
        ]
    )


def _apply_profile_knowledge(config: JobAgentConfig, profile_text: str) -> JobAgentConfig:
    knowledge = extract_profile_knowledge(profile_text)

    target_roles = unique_terms(knowledge.target_roles or config.target.roles)
    role_signals = unique_terms(knowledge.role_signals + target_roles + config.score_consistency.target_role_terms)
    preferred = unique_terms(knowledge.positive_terms + target_roles + config.matching.preferred_terms)
    avoid = unique_terms(knowledge.avoid_terms + config.matching.avoid_terms)
    search_terms = unique_terms(knowledge.search_terms + preferred + role_signals)
    location_terms = unique_terms(config.matching.location_aliases + _location_terms(config))

    config.target.roles = target_roles or ["profile-defined target role"]
    config.matching.preferred_terms = preferred
    config.matching.avoid_terms = avoid
    config.matching.location_aliases = location_terms
    config.score_consistency.target_role_terms = role_signals
    config.score_consistency.strong_fit_terms = unique_terms(knowledge.positive_terms + config.score_consistency.strong_fit_terms)
    config.score_consistency.adjacent_role_terms = unique_terms(config.score_consistency.adjacent_role_terms)
    config.score_consistency.irrelevant_role_patterns = unique_terms(
        config.score_consistency.irrelevant_role_patterns + regexes_from_terms(avoid)
    )
    # Relevant engineer exceptions are profile-derived role signals; no target-specific words live in prompts.yaml.
    config.score_consistency.protected_relevant_patterns = unique_terms(
        config.score_consistency.protected_relevant_patterns + regexes_from_terms(role_signals)
    )
    config.crawler.job_link_hints = unique_terms(default_job_link_hints() + role_signals + search_terms[:60])
    config.crawler.source_discovery_terms = unique_terms(config.crawler.source_discovery_terms + search_terms[:80])
    config.exploration.source_discovery_terms = unique_terms(config.exploration.source_discovery_terms + search_terms[:80])
    config.exploration.local_area_terms = unique_terms(config.exploration.local_area_terms + location_terms + config.location_radius.remote_terms)
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
    )

    profile_text = paths.profile_path.read_text(encoding="utf-8").strip() if paths.profile_path.exists() else ""
    config = _apply_profile_knowledge(config, profile_text)
    return LoadedConfig(config=config, paths=paths)


def ensure_data_dirs(paths: RuntimePaths) -> None:
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.database_path.parent.mkdir(parents=True, exist_ok=True)
    paths.csv_export_path.parent.mkdir(parents=True, exist_ok=True)
    paths.jsonl_export_path.parent.mkdir(parents=True, exist_ok=True)
    paths.log_path.parent.mkdir(parents=True, exist_ok=True)
