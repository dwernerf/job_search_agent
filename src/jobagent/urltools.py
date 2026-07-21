from __future__ import annotations

import posixpath
import re
from urllib.parse import parse_qsl, quote, unquote, unquote_plus, urlencode, urljoin, urlparse, urlunparse

from .config import JobAgentConfig
from .models import LinkCandidate


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def normalize_domain(netloc: str) -> str:
    domain = netloc.lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def filter_url(raw: str, base: str | None, config: JobAgentConfig) -> str | None:
    if not raw:
        return None

    value = raw.strip()
    if not value or _CONTROL_CHARACTERS.search(value):
        return None

    try:
        joined = urljoin(base, value) if base else value
        parsed = urlparse(joined)
        hostname = parsed.hostname
        parsed.port  # Force validation of malformed ports.
    except ValueError:
        return None

    scheme = parsed.scheme.casefold()
    if scheme not in {item.casefold() for item in config.crawler.allowed_schemes}:
        return None

    if not hostname:
        return None

    netloc = parsed.netloc.lower()

    normalized_hostname = hostname.casefold()
    if any(part.casefold() in normalized_hostname for part in config.crawler.excluded_domain_substrings):
        return None

    path = parsed.path or "/"
    decoded_path = unquote(path).casefold()
    if any(decoded_path.endswith(ext.casefold()) for ext in config.crawler.excluded_file_extensions):
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
    if path.endswith("/") and normalized_path != "/":
        normalized_path += "/"

    filtered = urlunparse((scheme, netloc, normalized_path, "", query, ""))
    denial_target = urlunparse(
        (
            scheme,
            netloc,
            unquote(normalized_path),
            "",
            unquote_plus(query),
            "",
        )
    )
    if any(re.search(pattern, denial_target) for pattern in config.crawler.denied_url_patterns):
        return None
    return filtered


def filter_links(
    links: list[LinkCandidate],
    source_url: str,
    config: JobAgentConfig,
) -> list[LinkCandidate]:
    canonical_source = filter_url(source_url, None, config)
    seen: set[str] = set()
    filtered: list[LinkCandidate] = []

    for link in links:
        url = filter_url(link.url, source_url, config)
        if not url or url == canonical_source or url in seen:
            continue
        seen.add(url)
        filtered.append(LinkCandidate(text=link.text, url=url))

    return filtered


def source_key(url: str, config: JobAgentConfig) -> str:
    parsed = urlparse(url)
    domain = normalize_domain(parsed.netloc)

    if config.crawler.source_key_mode == "domain":
        return domain

    parts = [p for p in parsed.path.split("/") if p]
    if config.crawler.source_key_mode == "domain_path1" and parts:
        return f"{domain}/{parts[0].lower()}"
    if config.crawler.source_key_mode == "domain_path2" and len(parts) >= 2:
        return f"{domain}/{parts[0].lower()}/{parts[1].lower()}"
    if config.crawler.source_key_mode == "domain_path2" and parts:
        return f"{domain}/{parts[0].lower()}"

    return domain


def render_query_url(query: str, template: str) -> str:
    return template.format(query=quote(query))
