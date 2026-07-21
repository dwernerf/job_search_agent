from __future__ import annotations

import re
from dataclasses import dataclass, field


def unique_terms(values: list[str]) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for value in values:
        term = str(value).strip()
        key = term.casefold()
        if term and key not in seen:
            seen.add(key)
            terms.append(term)
    return terms


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
            # Neutral bullets are useful search and text-relevance context.
            knowledge.search_terms.extend(terms)

    knowledge.target_roles = unique_terms(knowledge.target_roles)
    knowledge.role_signals = unique_terms(knowledge.role_signals)
    knowledge.positive_terms = unique_terms(knowledge.positive_terms + knowledge.search_terms)
    knowledge.avoid_terms = unique_terms(knowledge.avoid_terms)
    knowledge.search_terms = unique_terms(knowledge.search_terms + knowledge.target_roles + knowledge.positive_terms)
    return knowledge
