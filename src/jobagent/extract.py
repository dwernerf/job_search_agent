from __future__ import annotations

import json
import re
from typing import Any

from .config import JobAgentConfig
from .language import multilingual_relevance_terms
from .models import JobMatch, LinkCandidate, PageDecision, PageSnapshot, QuerySuggestion, as_text, clamp_int
from .urltools import clean_url, denied_by_safety, link_hint_score, same_domain


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

    for link in snapshot.links[: config.crawler.max_raw_links_retained]:
        url = clean_url(link.url, snapshot.final_url or snapshot.url, config)
        if not url or url in seen:
            continue

        text = re.sub(r"\s+", " ", link.text or "").strip()
        if denied_by_safety(url, text, config):
            continue

        score, reason = link_hint_score(url, text, config)
        if score <= 0:
            continue
        if same_domain(url, snapshot.final_url or snapshot.url):
            score += 0.25

        seen.add(url)
        ranked.append(LinkCandidate(text=text[:240], url=url, score=score, reason=reason))

    ranked.sort(key=lambda x: x.score, reverse=True)
    return ranked[: config.crawler.max_links_per_page_for_llm]


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
            fit_score=clamp_int(item.get("fit_score"), 0, 100),
            reason=as_text(item.get("reason"), 800),
            evidence=as_text(item.get("evidence"), 800),
            posting_language=as_text(item.get("posting_language") or item.get("language"), 80),
            score_source=as_text(item.get("score_source"), 80) or "llm",
            score_basis=as_text(item.get("score_basis"), 1000),
        )
        if job.title and job.url:
            jobs.append(job)

    follow_urls = []
    for value in data.get("follow_urls", []) or []:
        if isinstance(value, str) and value.strip():
            follow_urls.append(value.strip())

    return PageDecision(
        jobs=jobs,
        follow_urls=list(dict.fromkeys(follow_urls)),
        source_quality=clamp_int(data.get("source_quality"), 0, 100),
        source_notes=as_text(data.get("source_notes"), 1000),
    )


def query_suggestions_from_dict(data: dict[str, Any]) -> list[QuerySuggestion]:
    suggestions: list[QuerySuggestion] = []
    for item in data.get("queries", []) or []:
        if isinstance(item, str):
            query = item.strip()
            reason = ""
        elif isinstance(item, dict):
            query = str(item.get("query") or "").strip()
            reason = str(item.get("reason") or "").strip()
        else:
            continue

        if query:
            suggestions.append(QuerySuggestion(query=query[:400], reason=reason[:800]))

    return suggestions
