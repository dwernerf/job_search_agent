from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


@dataclass(slots=True)
class ProfileKnowledge:
    """Terms derived from profile.md.

    profile.md is the single user-facing place for target-role vocabulary.
    This parser intentionally accepts ordinary Markdown instead of a strict schema.
    """

    target_roles: list[str] = field(default_factory=list)
    role_signals: list[str] = field(default_factory=list)
    positive_terms: list[str] = field(default_factory=list)
    avoid_terms: list[str] = field(default_factory=list)
    search_terms: list[str] = field(default_factory=list)


def unique_terms(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        term = re.sub(r"\s+", " ", str(value or "")).strip(" \t\n\r-–—:;,.•")
        if not term:
            continue
        key = term.casefold()
        if key not in seen:
            seen.add(key)
            out.append(term)
    return out


def _section_key(line: str) -> str:
    text = re.sub(r"^#+\s*", "", line).strip()
    text = re.sub(r"\*\*", "", text).strip().rstrip(":")
    return text.casefold()


def _split_term_item(value: str) -> list[str]:
    text = re.sub(r"\([^)]*\)", "", value).strip()
    text = re.sub(r"\s+", " ", text)
    # Split common bilingual slash lists but avoid breaking URLs.
    parts = re.split(r"\s+[/|]\s+|\s+;\s+|\s+,\s+", text)
    cleaned: list[str] = []
    for part in parts:
        part = re.sub(r"^(and|or|oder|und)\s+", "", part.strip(), flags=re.I)
        if len(part) >= 2:
            cleaned.append(part)
    return cleaned or ([text] if text else [])


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith("#"):
        return True
    if stripped.endswith(":") and not stripped.startswith(("-", "*")) and len(stripped) < 80:
        return True
    return False


def _bullet_text(line: str) -> str | None:
    match = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.+?)\s*$", line)
    return match.group(1).strip() if match else None


def _classify_section(section: str) -> str:
    s = section.casefold()
    if any(k in s for k in ("avoid", "exclude", "not target", "non-target", "nicht", "vermeiden", "ausschluss")):
        return "avoid"
    if any(k in s for k in ("role signal", "target role signal", "fit signal", "suchsignal")):
        return "role_signal"
    if any(k in s for k in ("target role", "desired role", "good fit", "acceptable title", "zielrolle", "rollen", "stellenbezeichnung")):
        return "role"
    if any(k in s for k in ("search", "discovery", "query", "keywords", "such", "suche")):
        return "search"
    if any(k in s for k in ("skill", "expertise", "experience", "industry", "strong", "theme", "background", "special", "kennt", "branche", "kompetenz")):
        return "positive"
    return "other"


def extract_profile_knowledge(profile_text: str) -> ProfileKnowledge:
    knowledge = ProfileKnowledge()
    section = ""

    for raw in (profile_text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if _is_heading(line):
            section = _section_key(line)
            continue

        bullet = _bullet_text(line)
        if bullet is None:
            continue

        terms = _split_term_item(bullet)
        bucket = _classify_section(section)
        if bucket == "role":
            knowledge.target_roles.extend(terms)
            knowledge.role_signals.extend(terms)
        elif bucket == "role_signal":
            knowledge.role_signals.extend(terms)
        elif bucket == "avoid":
            knowledge.avoid_terms.extend(terms)
        elif bucket == "search":
            knowledge.search_terms.extend(terms)
        elif bucket == "positive":
            knowledge.positive_terms.extend(terms)
        else:
            # Neutral bullets are useful search context but should not become score guardrails.
            knowledge.search_terms.extend(terms)

    # Useful short signals derived from multi-word target roles.
    derived_role_signals: list[str] = []
    for role in knowledge.target_roles:
        role_l = role.casefold()
        for token in (
            "procurement", "purchasing", "buyer", "sourcing", "supply chain",
            "supplier quality", "supplier development", "supplier management",
            "category", "commodity", "einkauf", "einkäufer", "einkaeufer",
            "beschaffung", "lieferantenqualität", "lieferantenqualitaet",
            "lieferantenentwicklung", "lieferantenmanagement", "warengruppe",
        ):
            if token in role_l:
                derived_role_signals.append(token)

    knowledge.target_roles = unique_terms(knowledge.target_roles)
    knowledge.role_signals = unique_terms(knowledge.role_signals + derived_role_signals)
    knowledge.positive_terms = unique_terms(knowledge.positive_terms + knowledge.search_terms)
    knowledge.avoid_terms = unique_terms(knowledge.avoid_terms)
    knowledge.search_terms = unique_terms(knowledge.search_terms + knowledge.target_roles + knowledge.positive_terms)
    return knowledge


def regexes_from_terms(terms: Iterable[str]) -> list[str]:
    patterns: list[str] = []
    for term in unique_terms(terms):
        escaped = re.escape(term).replace(r"\ ", r"[\s_-]+")
        if escaped:
            patterns.append(rf"(?i)\b{escaped}\b")
    return patterns
