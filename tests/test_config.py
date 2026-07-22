from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from jobagent.config import load_config


def test_sample_config_loads(sample_config_path):
    loaded = load_config(sample_config_path)
    raw = yaml.safe_load(sample_config_path.read_text(encoding="utf-8"))

    assert loaded.config.target.local_area
    assert loaded.config.target.roles
    assert loaded.config.run.backlog_order == raw["run"]["backlog_order"]
    assert loaded.config.run.reset_pages_on_start == raw["run"]["reset_pages_on_start"]
    assert loaded.paths.profile_path.exists()
    assert loaded.paths.prompts_path.exists()


def test_runtime_paths_resolve_relative_to_config_project(sample_config_path):
    loaded = load_config(sample_config_path)
    project_root = sample_config_path.parents[1].resolve()

    assert loaded.paths.database_path == project_root / "data" / "jobs.sqlite"
    assert loaded.paths.profile_path == project_root / "config" / "profile.md"
    assert loaded.paths.prompts_path == project_root / "config" / "prompts.yaml"


def test_llm_config_matches_yaml(sample_config_path):
    loaded = load_config(sample_config_path)
    raw = yaml.safe_load(sample_config_path.read_text(encoding="utf-8"))

    assert loaded.config.llm.base_url == raw["llm"]["base_url"]
    assert loaded.config.llm.timeout_seconds == raw["llm"]["timeout_seconds"]
    assert not hasattr(loaded.config.llm, "context_window_tokens")


def test_browser_user_agent_and_url_denials_are_configured(sample_config_path):
    loaded = load_config(sample_config_path)

    assert loaded.config.app.user_agent.startswith("JobMatchAgent/")
    assert loaded.config.crawler.denied_url_patterns
    assert loaded.config.crawler.batch_size_for_llm == 35
    assert loaded.config.crawler.max_page_context_chars == 5000
    assert loaded.config.scoring.min_score_to_explore == 40
    assert not hasattr(loaded.config.crawler, "max_page_text_chars")
    assert not hasattr(loaded.config.crawler, "max_compact_lines")
    assert not hasattr(loaded.config.crawler, "max_important_lines")
    assert not hasattr(loaded.config.crawler, "forbidden_link_text_patterns")


def test_invalid_denied_url_pattern_fails_config_validation(sample_config_path):
    raw = yaml.safe_load(sample_config_path.read_text(encoding="utf-8"))
    raw.setdefault("crawler", {})["denied_url_patterns"] = ["["]
    sample_config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValidationError):
        load_config(sample_config_path)


def test_unknown_backlog_order_fails_config_validation(sample_config_path):
    raw = yaml.safe_load(sample_config_path.read_text(encoding="utf-8"))
    raw["run"]["backlog_order"] = "highest_rating"
    sample_config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValidationError):
        load_config(sample_config_path)


def test_reset_pages_on_start_defaults_to_false(sample_config_path):
    raw = yaml.safe_load(sample_config_path.read_text(encoding="utf-8"))
    raw["run"].pop("reset_pages_on_start")
    sample_config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    assert load_config(sample_config_path).config.run.reset_pages_on_start is False


@pytest.mark.parametrize("value", [-1, 101])
def test_invalid_min_score_to_explore_fails_config_validation(
    sample_config_path,
    value,
):
    raw = yaml.safe_load(sample_config_path.read_text(encoding="utf-8"))
    raw["scoring"]["min_score_to_explore"] = value
    sample_config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValidationError):
        load_config(sample_config_path)
