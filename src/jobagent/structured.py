from __future__ import annotations

import json
from typing import Any
from urllib.parse import urljoin


def _iter_objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_objects(child)


def _has_jobposting_type(obj: dict[str, Any]) -> bool:
    value = obj.get("@type") or obj.get("type")
    if isinstance(value, str):
        return value.casefold() == "jobposting"
    if isinstance(value, list):
        return any(str(item).casefold() == "jobposting" for item in value)
    return False


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(x for x in (_text(item) for item in value) if x)
    if isinstance(value, dict):
        return ", ".join(x for x in (_text(item) for item in value.values()) if x)
    return str(value).strip()


def _company(value: Any) -> str:
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("legalName") or value)
    return _text(value)


def _location(value: Any) -> str:
    locations = value if isinstance(value, list) else [value]
    out: list[str] = []
    for loc in locations:
        if isinstance(loc, dict):
            address = loc.get("address", loc)
            if isinstance(address, dict):
                parts = [
                    address.get("addressLocality"),
                    address.get("addressRegion"),
                    address.get("addressCountry"),
                ]
                text = ", ".join(_text(part) for part in parts if _text(part))
            else:
                text = _text(address)
        else:
            text = _text(loc)
        if text:
            out.append(text)
    return "; ".join(dict.fromkeys(out))


def extract_jobpostings(raw_json_ld: list[str], base_url: str) -> list[dict[str, str]]:
    jobs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for raw in raw_json_ld:
        if not raw or not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for obj in _iter_objects(parsed):
            if not _has_jobposting_type(obj):
                continue
            title = _text(obj.get("title"))
            url = _text(obj.get("url") or obj.get("sameAs") or base_url)
            if url:
                url = urljoin(base_url, url)
            company = _company(obj.get("hiringOrganization"))
            location = _location(obj.get("jobLocation"))
            description = _text(obj.get("description"))
            employment_type = _text(obj.get("employmentType"))
            date_posted = _text(obj.get("datePosted"))
            valid_through = _text(obj.get("validThrough"))
            key = (title.casefold(), url)
            if not title or not url or key in seen:
                continue
            seen.add(key)
            jobs.append(
                {
                    "title": title[:300],
                    "company": company[:200],
                    "location": location[:200],
                    "url": url[:1500],
                    "description": description[:4000],
                    "employment_type": employment_type[:120],
                    "date_posted": date_posted[:80],
                    "valid_through": valid_through[:80],
                }
            )
    return jobs


def structured_jobs_as_text(jobs: list[dict[str, str]], max_jobs: int = 20) -> str:
    if not jobs:
        return ""
    lines = ["STRUCTURED JOBPOSTING DATA:"]
    for idx, job in enumerate(jobs[:max_jobs], start=1):
        lines.append(f"{idx}. Title: {job.get('title', '')}")
        if job.get("company"):
            lines.append(f"   Company: {job['company']}")
        if job.get("location"):
            lines.append(f"   Location: {job['location']}")
        if job.get("employment_type"):
            lines.append(f"   Employment type: {job['employment_type']}")
        if job.get("url"):
            lines.append(f"   URL: {job['url']}")
    return "\n".join(lines)
