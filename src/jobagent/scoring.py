from __future__ import annotations

import re
from dataclasses import replace
from typing import Iterable
from urllib.parse import unquote

from .config import JobAgentConfig
from .models import JobMatch
from .location import evaluate_job_location


def _norm(value: str) -> str:
    return unquote(value or "").casefold()


def _contains_any(text: str, terms: Iterable[str]) -> list[str]:
    hay = _norm(text)
    hits: list[str] = []
    for term in terms:
        needle = _norm(str(term)).strip()
        if needle and needle in hay:
            hits.append(str(term))
    # Preserve order while deduping case-insensitively.
    out: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        key = _norm(hit)
        if key not in seen:
            seen.add(key)
            out.append(hit)
    return out


def _matches_any(patterns: Iterable[str], text: str) -> list[str]:
    hits: list[str] = []
    for pattern in patterns:
        try:
            if re.search(pattern, text or ""):
                hits.append(pattern)
        except re.error:
            continue
    return hits


def _location_is_unclear(job: JobMatch, config: JobAgentConfig) -> bool:
    if not job.location.strip():
        return True
    hay = f"{job.location}\n{job.reason}\n{job.evidence}\n{job.url}"
    location_hits = _contains_any(
        hay,
        [config.target.local_area] + config.matching.location_aliases + config.exploration.local_area_terms,
    )
    if location_hits:
        return False
    weak_hits = _contains_any(job.location, config.score_consistency.weak_location_terms)
    return bool(weak_hits)


def normalize_job_score(job: JobMatch, config: JobAgentConfig) -> JobMatch | None:
    """Apply deterministic guardrails around an LLM or heuristic score.

    The LLM is still allowed to produce the score. This function makes the score
    consistent by enforcing target-role and exclusion caps configured in YAML.
    A job capped below matching.min_fit_score_to_save is returned as None.
    """
    if not config.score_consistency.enabled:
        return None if job.fit_score < config.matching.min_fit_score_to_save else job

    cfg = config.score_consistency
    score = max(0, min(100, int(job.fit_score)))
    text = "\n".join(
        [
            job.title,
            job.company,
            job.location,
            job.reason,
            job.evidence,
            job.url,
        ]
    )

    reasons: list[str] = []
    original = score

    target_hits = _contains_any(text, cfg.target_role_terms)
    strong_hits = _contains_any(text, cfg.strong_fit_terms)
    adjacent_hits = _contains_any(text, cfg.adjacent_role_terms)
    avoid_hits = _contains_any(text, config.matching.avoid_terms)
    protected_hits = _matches_any(cfg.protected_relevant_patterns, text)
    irrelevant_hits = [] if protected_hits else _matches_any(cfg.irrelevant_role_patterns, text)

    if target_hits:
        reasons.append("target role signal: " + ", ".join(target_hits[:4]))
    elif adjacent_hits:
        reasons.append("adjacent role signal only: " + ", ".join(adjacent_hits[:3]))
    else:
        reasons.append("no configured target-role signal")

    if strong_hits:
        reasons.append("strong fit term: " + ", ".join(strong_hits[:4]))

    caps: list[tuple[int, str]] = []

    if irrelevant_hits:
        caps.append((cfg.irrelevant_role_cap, "irrelevant-role pattern"))

    if avoid_hits:
        caps.append((cfg.avoid_term_cap, "avoid term: " + ", ".join(avoid_hits[:3])))

    if cfg.require_target_role_signal and not target_hits:
        caps.append((cfg.no_target_role_cap, "missing target-role signal"))

    radius_verdict = evaluate_job_location(job, config)
    if not radius_verdict.allowed or radius_verdict.score_cap is not None:
        cap = radius_verdict.score_cap
        if cap is None:
            cap = config.location_radius.outside_radius_cap
        detail = radius_verdict.reason
        if radius_verdict.matched_place and radius_verdict.distance_km is not None:
            detail += f": {radius_verdict.matched_place} ({radius_verdict.distance_km:.1f} km)"
        caps.append((cap, "location radius: " + detail))
    elif config.location_radius.enabled and radius_verdict.distance_km is not None:
        reasons.append(f"location radius: {radius_verdict.matched_place} ({radius_verdict.distance_km:.1f} km)")
    elif _location_is_unclear(job, config):
        caps.append((cfg.unclear_location_cap, "unclear/non-target location"))

    if job.score_source.startswith("llm"):
        if len(job.evidence.strip()) < cfg.min_evidence_chars_for_llm_job:
            caps.append((cfg.no_target_role_cap, "weak LLM evidence"))
        if len(job.reason.strip()) < cfg.min_reason_chars_for_llm_job:
            caps.append((cfg.no_target_role_cap, "weak LLM reason"))

    if caps:
        cap_value, cap_reason = min(caps, key=lambda x: x[0])
        if score > cap_value:
            score = cap_value
            reasons.append(f"score capped from {original} to {score}: {cap_reason}")

    if score < config.matching.min_fit_score_to_save:
        return None

    source = job.score_source or "llm"
    if score != original and not source.endswith("_guarded"):
        source = f"{source}_guarded"

    basis = "; ".join(reasons[:6])
    if job.score_basis.strip():
        basis = f"{job.score_basis.strip()}; {basis}" if basis else job.score_basis.strip()

    return replace(
        job,
        fit_score=score,
        score_source=source,
        score_basis=basis[:1000],
    )
