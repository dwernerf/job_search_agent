from __future__ import annotations

import math
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from .config import JobAgentConfig
from .models import JobMatch


@dataclass(frozen=True, slots=True)
class LocationVerdict:
    allowed: bool
    score_cap: int | None
    reason: str
    matched_place: str = ""
    distance_km: float | None = None
    remote: bool = False
    unknown: bool = False


def _asciiish(value: str) -> str:
    text = unquote(value or "").casefold()
    replacements = {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return text


def _norm(value: str) -> str:
    return unquote(value or "").casefold()


def _variants(value: str) -> list[str]:
    original = _norm(value).strip()
    asciiish = _asciiish(value).strip()
    out: list[str] = []
    for item in (original, asciiish):
        if item and item not in out:
            out.append(item)
    return out


def _contains_term(text: str, term: str) -> bool:
    if not str(term).strip():
        return False
    hay_variants = [_norm(text), _asciiish(text)]
    # Use loose token boundaries for city and phrase matching. This avoids
    # matching "Berlin" inside a longer unrelated token but still works with
    # punctuation, slashes, and URL-encoded strings.
    for needle in _variants(term):
        pattern = r"(?<![\w])" + re.escape(needle) + r"(?![\w])"
        if any(re.search(pattern, hay, flags=re.I) is not None for hay in hay_variants):
            return True
    return False


def _hits(text: str, terms: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if _contains_term(text, term):
            key = _norm(term)
            if key not in seen:
                seen.add(key)
                out.append(term)
    return out


def _city_matches(text: str, config: JobAgentConfig) -> list[tuple[str, float]]:
    cfg = config.location_radius
    matches: list[tuple[str, float]] = []
    for place, coords in cfg.city_coordinates.items():
        if not _contains_term(text, place):
            continue
        lat, lon = float(coords[0]), float(coords[1])
        distance = haversine_km(cfg.latitude, cfg.longitude, lat, lon)
        matches.append((place, distance))
    matches.sort(key=lambda x: x[1])
    return matches


def is_location_only_title(title: str, config: JobAgentConfig) -> bool:
    """Return true when a supposed job title is just a city/area label.

    This catches portal/sidebar links such as "Erlangen" or "München, Germany"
    that should be followed or ignored, not saved as job postings.
    """
    raw = re.sub(r"\s+", " ", title or "").strip(" -–—|,;:()[]{}\t\n")
    if not raw or len(raw) > 90:
        return False

    lowered = _norm(raw)
    ascii_lowered = _asciiish(raw)

    # If it contains a configured target-role signal, it is not location-only.
    role_terms = config.score_consistency.target_role_terms + config.target.roles + config.matching.preferred_terms
    if any(_contains_term(raw, term) for term in role_terms):
        return False

    location_terms = (
        list(config.location_radius.city_coordinates.keys())
        + config.location_radius.broad_location_terms
        + config.location_radius.target_country_terms
        + config.matching.location_aliases
        + [config.target.local_area, config.location_radius.target_city]
    )

    exact_variants: set[str] = set()
    for term in location_terms:
        for variant in _variants(term):
            exact_variants.add(variant)
            exact_variants.add(variant.replace(",", ""))
            exact_variants.add(variant + " germany")
            exact_variants.add(variant + " deutschland")

    normalized_without_punct = re.sub(r"[^a-z0-9äöüß]+", " ", lowered, flags=re.I).strip()
    ascii_without_punct = re.sub(r"[^a-z0-9]+", " ", ascii_lowered, flags=re.I).strip()
    return lowered in exact_variants or ascii_lowered in exact_variants or normalized_without_punct in exact_variants or ascii_without_punct in exact_variants


def _city_match_groups(text: str, config: JobAgentConfig) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    cfg = config.location_radius
    matches = _city_matches(text, config)
    inside = [(place, distance) for place, distance in matches if distance <= cfg.radius_km]
    outside = [(place, distance) for place, distance in matches if distance > cfg.radius_km]
    return inside, outside


def _format_city_reason(prefix: str, matches: list[tuple[str, float]]) -> tuple[str, str, float | None]:
    if not matches:
        return prefix, "", None
    place, distance = matches[0]
    return f"{prefix}: {place} ({distance:.1f} km)", place, distance


def evaluate_exploration_url_location(url: str, context: str, config: JobAgentConfig) -> LocationVerdict:
    """Guard exploration URLs against drifting into known out-of-radius cities.

    The key detail is that URL text and link/search context are evaluated
    separately. A portal search page may contain "Munich" in its originating
    query, but a concrete candidate URL such as /pforzheim-technischer-einkaeufer
    must still be rejected before the browser opens it.
    """
    cfg = config.location_radius
    if not cfg.enabled or not cfg.filter_exploration_urls:
        return LocationVerdict(True, None, "exploration URL location filter disabled")

    parsed = urlparse(url)
    path_segments = [seg.casefold() for seg in re.split(r"[/_]+", unquote(parsed.path or "")) if seg]
    query_text = unquote(parsed.query or "")
    allowed_country_segments = {seg.casefold() for seg in cfg.allowed_country_url_segments}
    blocked_country_segments = {seg.casefold() for seg in cfg.blocked_country_url_segments}
    blocked_hits = [seg for seg in path_segments if seg in blocked_country_segments]
    allowed_hits = [seg for seg in path_segments if seg in allowed_country_segments]
    if blocked_hits and not allowed_hits:
        return LocationVerdict(
            False,
            cfg.outside_radius_cap,
            "exploration URL has non-target country/language path segment: " + ", ".join(blocked_hits[:3]),
        )

    # Common country-switch parameters on ATS pages are as strong as path
    # segments. This catches ?change_c=CH / ?country=AT before they fan out.
    country_query_hits: list[str] = []
    for pattern in (r"(?:^|[&?])(change_c|country|locale|lang|language)=([^&]+)",):
        for _key, value in re.findall(pattern, "?" + query_text, flags=re.I):
            decoded = unquote(value).strip().casefold()
            if decoded in blocked_country_segments and decoded not in allowed_country_segments:
                country_query_hits.append(decoded)
    if country_query_hits:
        return LocationVerdict(
            False,
            cfg.outside_radius_cap,
            "exploration URL has non-target country/language query parameter: " + ", ".join(country_query_hits[:3]),
        )

    url_inside, url_outside = _city_match_groups(url, config)
    context_inside, context_outside = _city_match_groups(context or "", config)

    if cfg.drop_urls_with_outside_city:
        # URL-encoded city slugs are high-confidence. Do not let a Munich search
        # query in the source context override /pforzheim-... or /buchloe-... .
        if url_outside and not url_inside:
            reason, place, distance = _format_city_reason("exploration URL has outside-radius city in URL", url_outside)
            return LocationVerdict(False, cfg.outside_radius_cap, reason, matched_place=place, distance_km=distance)

        # Link text is also a high-confidence signal for job-detail candidates.
        # Treat it independently from the originating search query.
        if context_outside and not context_inside:
            reason, place, distance = _format_city_reason("exploration URL has outside-radius city in link/search context", context_outside)
            return LocationVerdict(False, cfg.outside_radius_cap, reason, matched_place=place, distance_km=distance)

    combined = f"{url}\n{context or ''}"
    matches = _city_matches(combined, config)
    if not matches:
        return LocationVerdict(True, None, "no city encoded in exploration URL/context", unknown=True)

    inside = [(place, distance) for place, distance in matches if distance <= cfg.radius_km]
    if inside:
        place, distance = inside[0]
        return LocationVerdict(True, None, "exploration URL has target-radius city", matched_place=place, distance_km=distance)

    place, distance = matches[0]
    return LocationVerdict(
        not cfg.drop_urls_with_outside_city,
        cfg.outside_radius_cap,
        f"exploration URL has outside-radius city",
        matched_place=place,
        distance_km=distance,
    )

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def job_location_text(job: JobMatch) -> str:
    return "\n".join(
        [
            job.location,
            job.title,
            job.company,
            job.reason,
            job.evidence,
            job.url,
        ]
    )


def location_radius_summary(config: JobAgentConfig) -> str:
    cfg = config.location_radius
    if not cfg.enabled:
        return "Location radius filter disabled."
    remote = "allowed" if cfg.allow_remote_if_country_match and config.target.include_remote else "not allowed"
    return (
        f"Target city: {cfg.target_city}. Radius: {cfg.radius_km:.0f} km. "
        f"Remote jobs are {remote} only when they explicitly fit the configured target country terms. "
        "Reject generic initiative/speculative applications and jobs outside the radius."
    )


def evaluate_job_location(job: JobMatch, config: JobAgentConfig) -> LocationVerdict:
    cfg = config.location_radius
    if not cfg.enabled:
        return LocationVerdict(True, None, "location radius filter disabled")

    text = job_location_text(job)
    remote_hits = _hits(text, cfg.remote_terms)
    country_hits = _hits(text, cfg.target_country_terms)

    if remote_hits and config.target.include_remote and cfg.allow_remote_if_country_match:
        if country_hits or _hits(text, [cfg.target_city] + config.matching.location_aliases):
            return LocationVerdict(
                True,
                None,
                "remote/hybrid posting with configured target-country or target-city signal",
                remote=True,
            )

    city_matches = _city_matches(text, config)

    if city_matches:
        nearest_place, nearest_distance = city_matches[0]
        inside = [(place, distance) for place, distance in city_matches if distance <= cfg.radius_km]
        if inside:
            place, distance = inside[0]
            return LocationVerdict(
                True,
                None,
                f"matched location within {cfg.radius_km:.0f} km radius",
                matched_place=place,
                distance_km=distance,
            )
        return LocationVerdict(
            not cfg.hard_drop_outside_radius,
            cfg.outside_radius_cap,
            f"nearest matched location is outside {cfg.radius_km:.0f} km radius",
            matched_place=nearest_place,
            distance_km=nearest_distance,
        )

    broad_hits = _hits(text, cfg.broad_location_terms)
    if remote_hits and config.target.include_remote:
        return LocationVerdict(
            False if cfg.require_location_for_non_remote else True,
            cfg.unknown_location_cap,
            "remote/hybrid signal without target-country evidence",
            remote=True,
            unknown=True,
        )

    if cfg.require_location_for_non_remote:
        detail = "broad location only: " + ", ".join(broad_hits[:3]) if broad_hits else "no recognized target-area city"
        return LocationVerdict(
            False,
            cfg.unknown_location_cap,
            detail,
            unknown=True,
        )

    return LocationVerdict(
        True,
        cfg.unknown_location_cap,
        "location not recognized; applying configured cap",
        unknown=True,
    )
