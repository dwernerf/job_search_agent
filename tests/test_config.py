from __future__ import annotations

import yaml

from jobagent.config import load_config


def test_sample_config_loads(sample_config_path):
    loaded = load_config(sample_config_path)
    assert loaded.config.target.local_area
    assert loaded.config.target.roles
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
    assert loaded.config.llm.context_window_tokens == raw["llm"]["context_window_tokens"]
    assert loaded.config.llm.thinking_enabled is True
    assert loaded.config.llm.timeout_seconds == raw["llm"]["timeout_seconds"]
