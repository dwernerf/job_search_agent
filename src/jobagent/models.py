from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class LinkCandidate:
    text: str
    url: str
    score: float = 0.0
    reason: str = ""

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "url": self.url,
            "score": round(self.score, 2),
            "reason": self.reason,
        }


@dataclass(slots=True)
class PageSnapshot:
    url: str
    final_url: str
    title: str
    text: str
    links: list[LinkCandidate] = field(default_factory=list)
    structured_jobs: list[dict[str, Any]] = field(default_factory=list)
    status_code: int = 0


@dataclass(slots=True)
class JobMatch:
    title: str
    company: str
    location: str
    url: str
    fit_score: int
    reason: str
    evidence: str
    posting_language: str = ""
    score_source: str = "llm"
    score_basis: str = ""


@dataclass(slots=True)
class PageDecision:
    jobs: list[JobMatch]
    follow_urls: list[str]
    source_quality: int
    source_notes: str


@dataclass(slots=True)
class QuerySuggestion:
    query: str
    reason: str


@dataclass(slots=True)
class FrontierItem:
    url: str
    depth: int
    priority: float
    discovered_from: str
    reason: str
    source_key: str


@dataclass(slots=True)
class SourceMemoryRow:
    source_key: str
    domain: str
    score: float
    visits: int
    jobs_found: int
    high_fit_jobs: int
    errors: int
    blocked: int
    no_job_streak: int
    last_quality: int
    notes: str


def clamp_int(value: Any, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = minimum
    return max(minimum, min(maximum, parsed))


def as_text(value: Any, max_len: int) -> str:
    if value is None:
        return ""
    return str(value).strip()[:max_len]
