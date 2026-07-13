from __future__ import annotations

import json
import re
from typing import Any

from .config import JobAgentConfig
from .language import multilingual_relevance_terms
from .models import JobMatch, LinkCandidate, LinkClassification, PageDecision, PageSnapshot, as_text
from .urltools import clean_url, denied_by_safety


def compact_text(text: str, config: JobAgentConfig) -> str:
    normalized = re.sub(r"[ \t]+", " ", text or "")
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    head = lines[: config.crawler.max_compact_lines]

    important_terms = multilingual_relevance_terms(config)
    important: list[str] = []

    for line in lines:
        low = line.lower()
        if any(term.lower() in low for term in important_terms):
            important.append(line)
        if len(important) >= config.crawler.max_important_lines:
            break

    body = "\n".join(head)
    if important:
        body += "\n\nLIKELY RELEVANT LINES:\n"
        body += "\n".join(important)

    return body[: config.crawler.max_page_text_chars]


def rank_candidate_links(snapshot: PageSnapshot, config: JobAgentConfig) -> list[LinkCandidate]:
    ranked: list[LinkCandidate] = []
    seen: set[str] = set()

    for raw_link in snapshot.links:
        url = clean_url(raw_link.url, snapshot.final_url or snapshot.url, config)
        if not url or url in seen:
            continue

        text = re.sub(r"\s+", " ", raw_link.text or "").strip()
        if denied_by_safety(url, text, config):
            continue

        score = 0.0
        reason = ""

        if url == (snapshot.final_url or snapshot.url):
            score += 0.25

        seen.add(url)
        ranked.append(LinkCandidate(text=text, url=url, score=score, reason=reason))

    ranked.sort(key=lambda x: x.score, reverse=True)
    return ranked


def strip_llm_noise(raw: str) -> str:
    text = raw or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I).strip()
    text = re.sub(r"^```(?:json)?", "", text, flags=re.I).strip()
    text = re.sub(r"```$", "", text).strip()
    return text


def parse_json_object(raw: str) -> dict[str, Any]:
    cleaned = strip_llm_noise(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if not match:
            raise
        parsed = json.loads(match.group(0))

    if not isinstance(parsed, dict):
        raise ValueError("LLM response is not a JSON object")
    return parsed


def page_decision_from_dict(data: dict[str, Any]) -> PageDecision:
    jobs: list[JobMatch] = []

    for item in data.get("jobs", []) or []:
        if not isinstance(item, dict):
            continue
        job = JobMatch(
            title=as_text(item.get("title"), 300),
            company=as_text(item.get("company"), 200),
            location=as_text(item.get("location"), 200),
            url=as_text(item.get("url"), 1500),
            fit_score=int(item.get("fit_score") or 0),
            reason=as_text(item.get("reason"), 800),
            evidence=as_text(item.get("evidence"), 800),
            posting_language=as_text(item.get("posting_language") or item.get("language"), 80),
        )
        if job.title and job.url:
            jobs.append(job)

    link_classifications = []
    for item in data.get("link_classifications", []) or []:
        if not isinstance(item, dict):
            continue
        classification = LinkClassification(
            index=int(item.get("index", 0)),
            type=item.get("type", "skip"),
            fit_score=int(item.get("fit_score") or 0),
            title=as_text(item.get("title"), 300),
            company=as_text(item.get("company"), 200),
            location=as_text(item.get("location"), 200),
            evidence=as_text(item.get("evidence"), 800),
            reason=as_text(item.get("reason"), 800),
        )
        link_classifications.append(classification)

    return PageDecision(
        jobs=jobs,
        link_classifications=link_classifications,
        source_quality=int(data.get("source_quality") or 0),
        source_notes=as_text(data.get("source_notes"), 1000),
    )



