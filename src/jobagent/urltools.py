from __future__ import annotations

import posixpath
import re
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

from .config import JobAgentConfig
from .language import multilingual_job_terms, multilingual_role_terms


def normalize_domain(netloc: str) -> str:
    domain = netloc.lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def clean_url(raw: str, base: str | None, config: JobAgentConfig) -> str | None:
    if not raw:
        return None

    value = raw.strip()
    lowered = value.lower()

    if lowered.startswith(("mailto:", "tel:", "javascript:", "data:")):
        return None

    joined = urljoin(base, value) if base else value
    parsed = urlparse(joined)

    if parsed.scheme.lower() not in {x.lower() for x in config.crawler.allowed_schemes}:
        return None

    if not parsed.netloc:
        return None

    netloc = parsed.netloc.lower()

    redirect_params = parse_qs(parsed.query)
    if "duckduckgo.com" in netloc and "uddg" in redirect_params:
        return clean_url(redirect_params["uddg"][0], None, config)
    if "google." in netloc and parsed.path == "/url" and "q" in redirect_params:
        return clean_url(redirect_params["q"][0], None, config)

    if any(part.lower() in netloc for part in config.crawler.excluded_domain_substrings):
        return None

    path = parsed.path or "/"
    if any(path.lower().endswith(ext.lower()) for ext in config.crawler.excluded_file_extensions):
        return None

    query_items = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in {p.lower() for p in config.crawler.dedupe_url_tracking_params}
    ]
    query = urlencode(query_items, doseq=True)

    normalized_path = posixpath.normpath(path)
    if normalized_path == ".":
        normalized_path = "/"
    if not normalized_path.startswith("/"):
        normalized_path = "/" + normalized_path
    if normalized_path != "/":
        normalized_path = normalized_path.rstrip("/")

    cleaned = urlunparse((parsed.scheme.lower(), netloc, normalized_path, "", query, ""))
    return cleaned


def denied_by_safety(url: str, link_text: str, config: JobAgentConfig) -> bool:
    haystacks = [url, link_text or ""]

    for pattern in config.safety.deny_url_patterns:
        if any(re.search(pattern, h) for h in haystacks):
            return True

    for pattern in config.safety.forbidden_link_text_patterns:
        if re.search(pattern, link_text or ""):
            return True

    return False


def source_key(url: str, config: JobAgentConfig) -> str:
    parsed = urlparse(url)
    domain = normalize_domain(parsed.netloc)

    if config.memory.source_key_mode == "domain":
        return domain

    parts = [p for p in parsed.path.split("/") if p]
    if config.memory.source_key_mode == "domain_path1" and parts:
        return f"{domain}/{parts[0].lower()}"
    if config.memory.source_key_mode == "domain_path2" and len(parts) >= 2:
        return f"{domain}/{parts[0].lower()}/{parts[1].lower()}"
    if config.memory.source_key_mode == "domain_path2" and parts:
        return f"{domain}/{parts[0].lower()}"

    return domain


def domain_from_url(url: str) -> str:
    return normalize_domain(urlparse(url).netloc)


def render_query_url(query: str, template: str) -> str:
    return template.format(query=quote(query))
