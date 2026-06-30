from __future__ import annotations

from pathlib import Path

from jobagent.config import load_config
from jobagent.profile_knowledge import extract_profile_knowledge


def test_profile_derives_target_roles_and_avoid_terms(loaded_sample):
    cfg = loaded_sample.config
    assert "Procurement Manager" in cfg.target.roles
    assert "Supplier Quality Manager" in cfg.target.roles
    assert "procurement" in [x.casefold() for x in cfg.score_consistency.target_role_terms]
    assert any("sales representative" == x.casefold() for x in cfg.matching.avoid_terms)


def test_prompts_are_generic_and_do_not_contain_target_role_content(loaded_sample):
    text = loaded_sample.paths.prompts_path.read_text(encoding="utf-8").casefold()
    forbidden = ["procurement", "purchasing", "supplier quality", "supply chain", "einkauf", "beschaffung", "optics", "laser"]
    assert not any(term in text for term in forbidden)


def test_minimal_yaml_can_load_because_terms_come_from_profile(tmp_path: Path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "profile.md").write_text(
        """
# Profile

## Target roles and acceptable titles
- Supplier Quality Manager
- Strategic Buyer / Strategischer Einkäufer

## Target role signals
- supplier quality
- buyer
- Einkauf

## Relevant expertise and positive fit factors
- optics
- mechanical components

## Avoid and exclude
- Sales Representative
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "config" / "seeds.txt").write_text("", encoding="utf-8")
    (tmp_path / "config" / "prompts.yaml").write_text(
        "page_analysis_system: 'Return JSON only.'\npage_analysis_user: '$profile'\nquery_generation_system: 'Return JSON only.'\nquery_generation_user: '$profile'\n",
        encoding="utf-8",
    )
    (tmp_path / "config" / "config.yaml").write_text(
        "profile:\n  path: config/profile.md\nprompts:\n  path: config/prompts.yaml\nseeds:\n  path: config/seeds.txt\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path / "config" / "config.yaml").config
    assert "Supplier Quality Manager" in cfg.target.roles
    assert "supplier quality" in [x.casefold() for x in cfg.score_consistency.target_role_terms]
    assert any(x.casefold() == "sales representative" for x in cfg.matching.avoid_terms)
