from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(slots=True)
class LinkCandidate:
    text: str
    url: str


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


@dataclass(slots=True)
class LinkClassification:
    index: int
    type: Literal["job_listing", "explore", "skip"]
    fit_score: int = 0
    title: str = ""
    company: str = ""
    location: str = ""
    url: str = ""
    evidence: str = ""
    reason: str = ""


@dataclass(slots=True)
class PageDecision:
    jobs: list[JobMatch]
    source_quality: int
    source_notes: str
    link_classifications: list[LinkClassification] = field(default_factory=list)


@dataclass(slots=True)
class FrontierItem:
    url: str
    depth: int
    discovered_from: str = ""
    reason: str = ""
    source_key: str = ""


def as_text(value: Any, max_len: int) -> str:
    if value is None:
        return ""
    return str(value).strip()[:max_len]
