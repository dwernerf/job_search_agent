from __future__ import annotations

from jobagent.config import load_config


def test_sample_config_loads(sample_config_path):
    loaded = load_config(sample_config_path)
    assert loaded.config.target.local_area
    assert loaded.config.target.roles
    assert loaded.paths.profile_path.exists()
    assert loaded.paths.prompts_path.exists()


def test_runtime_paths_resolve_relative_to_repo(sample_config_path):
    loaded = load_config(sample_config_path)
    assert loaded.paths.project_root == sample_config_path.parents[1].resolve()
    assert loaded.paths.database_path.name == "jobs.sqlite"



def test_llm_config_uses_simple_12k_context(sample_config_path):
    loaded = load_config(sample_config_path)
    assert loaded.config.llm.base_url == "http://127.0.0.1:8087/v1"
    assert loaded.config.llm.context_window_tokens == 12000
    assert loaded.config.llm.output_tokens == 5000
    assert loaded.config.llm.max_prompt_tokens == 6400
    assert loaded.config.llm.thinking_enabled is True
    assert loaded.config.llm.timeout_seconds == 400


def test_legacy_llm_budget_keys_are_migrated(tmp_path, sample_config_path):
    import shutil
    import yaml

    project = tmp_path / "project"
    shutil.copytree(sample_config_path.parents[1], project, ignore=shutil.ignore_patterns("data", "__pycache__", ".pytest_cache"))
    config_path = project / "config" / "config.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["llm"].pop("output_tokens", None)
    data["llm"].pop("thinking_enabled", None)
    data["llm"].update(
        {
            "max_tokens": 1234,
            "disable_thinking": True,
            "no_think_prefix": "/no_think",
            "max_prompt_tokens": 2600,
            "prompt_safety_margin_tokens": 250,
            "token_estimate_chars_per_token": 4.0,
            "max_page_text_chars_for_prompt": 6500,
            "min_page_text_chars_for_prompt": 1800,
            "max_candidate_links_for_prompt": 35,
            "min_candidate_links_for_prompt": 8,
        }
    )
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    loaded = load_config(config_path)
    assert loaded.config.llm.output_tokens == 1234
    assert loaded.config.llm.thinking_enabled is False
    assert loaded.config.llm.disable_thinking is True
