from __future__ import annotations

import json
import re
from typing import Any, Literal, cast

from .config import JobAgentConfig
from .models import LinkClassification, PageDecision, as_text


def compact_text(text: str, config: JobAgentConfig) -> str:
    normalized = re.sub(r"[ \t]+", " ", text or "")
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    max_chars = config.crawler.max_page_context_chars

    important_terms = tuple(
        term.lower()
        for term in (
            config.target.roles
            + config.crawler.job_link_hints
            + config.matching.location_aliases
            + config.matching.preferred_terms
            + config.matching.avoid_terms
            + config.exploration.local_area_terms
            + config.exploration.source_discovery_terms
        )
        if term.strip()
    )
    marker = "\n\nLIKELY RELEVANT LINES:\n"
    important: list[str] = []
    seen_important: set[str] = set()
    important_budget = max(0, max_chars // 2 - len(marker))
    important_chars = 0

    for line in lines:
        low = line.lower()
        if not any(term in low for term in important_terms):
            continue
        if line in seen_important:
            continue
        separator_chars = 1 if important else 0
        remaining = important_budget - important_chars - separator_chars
        if remaining <= 0:
            break
        piece = line[:remaining]
        important.append(piece)
        seen_important.add(line)
        important_chars += separator_chars + len(piece)
        if len(piece) < len(line):
            break

    if not important:
        return "\n".join(lines)[:max_chars]

    suffix = marker + "\n".join(important)
    head_budget = max(0, max_chars - len(suffix))
    return "\n".join(lines)[:head_budget] + suffix





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
    link_classifications = []
    for item in data.get("link_classifications", []) or []:
        if not isinstance(item, dict):
            continue
        raw_index = item.get("index")
        if isinstance(raw_index, bool):
            continue
        if isinstance(raw_index, int):
            index = raw_index
        elif isinstance(raw_index, str) and raw_index.strip().isdigit():
            index = int(raw_index.strip())
        else:
            continue
        try:
            fit_score = int(item.get("fit_score") or 0)
        except (TypeError, ValueError):
            continue
        classification_type = as_text(item.get("type", "skip"), 30)
        if classification_type not in {"job_listing", "explore", "skip"}:
            continue
        if not 0 <= fit_score <= 100:
            continue
        if classification_type == "skip":
            fit_score = 0
        classification = LinkClassification(
            index=index,
            type=cast(Literal["job_listing", "explore", "skip"], classification_type),
            fit_score=fit_score,
            title=as_text(item.get("title"), 300),
            company=as_text(item.get("company"), 200),
            location=as_text(item.get("location"), 200),
            evidence=as_text(item.get("evidence"), 800),
            reason=as_text(item.get("reason"), 800),
        )
        link_classifications.append(classification)

    return PageDecision(link_classifications=link_classifications)
