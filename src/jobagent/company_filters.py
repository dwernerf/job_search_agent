from __future__ import annotations

import re
import unicodedata

from .config import JobAgentConfig


_STOP_COMPANY_TOKENS = {
    "gmbh", "ag", "se", "kg", "mbh", "co", "company", "corp", "corporation",
    "inc", "ltd", "limited", "llc", "plc", "group", "holding", "holdings",
    "deutschland", "germany", "international", "global", "the", "and", "und",
}


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def compact_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize_text(value))


def company_aliases(name: str) -> list[str]:
    norm = normalize_text(name)
    if not norm:
        return []
    compact = compact_text(name)
    tokens = [tok for tok in norm.split() if tok and tok not in _STOP_COMPANY_TOKENS]

    aliases: list[str] = [norm]
    if compact:
        aliases.append(compact)

    # The first distinctive token catches common shortened employer names, e.g.
    # "Airbus" for "Airbus Defence & Space" and "SUSS" for "SUSS MicroTec".
    if tokens:
        aliases.append(tokens[0])

    # Two-token prefix catches domains/text like "marvel fusion" while remaining generic.
    if len(tokens) >= 2:
        aliases.append(" ".join(tokens[:2]))
        aliases.append("".join(tokens[:2]))

    # Acronyms are useful for all-caps company names like BMW.
    acronym = "".join(tok[0] for tok in tokens if tok)
    if len(acronym) >= 3:
        aliases.append(acronym)

    out: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        alias = alias.strip()
        if len(alias) < 3:
            continue
        if alias not in seen:
            seen.add(alias)
            out.append(alias)
    return out


def company_matches_text(company: str, *values: str) -> bool:
    if not company:
        return False

    aliases = company_aliases(company)
    if not aliases:
        return False

    norm_hay = normalize_text("\n".join(v or "" for v in values))
    compact_hay = compact_text(norm_hay)

    for alias in aliases:
        if " " in alias:
            if re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", norm_hay):
                return True
        else:
            # Single-token company aliases must match as a real token in text.
            if re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", norm_hay):
                return True
            # Compact domains such as bmwgroup.jobs, sussmicrotec.com, or isaraerospace.com.
            if alias in compact_hay:
                return True
    return False


def matches_blacklisted_company(config: JobAgentConfig, *values: str) -> bool:
    for company in config.companies.blacklist:
        if company_matches_text(company, *values):
            return True
    return False
