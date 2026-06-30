from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from jobagent.config import LoadedConfig, load_config


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def sample_config_path() -> Path:
    return REPO_ROOT / "config" / "config.yaml"


@pytest.fixture()
def loaded_sample(sample_config_path: Path) -> LoadedConfig:
    return load_config(sample_config_path)


@pytest.fixture()
def temp_loaded(tmp_path: Path) -> LoadedConfig:
    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
    config_path = tmp_path / "config" / "config.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["run"]["max_pages"] = 4
    data["run"]["min_delay_seconds"] = 0
    data["run"]["max_delay_seconds"] = 0
    data["browser"]["headless"] = True
    data["logging"]["console"] = False
    data["logging"]["file"] = False
    data["crawler"]["respect_robots_txt"] = False
    data["exploration"]["seed_search_when_empty"] = False
    data["exploration"]["query_generation_every_pages"] = 2
    data.setdefault("companies", {})["whitelist_search_when_seeding"] = False
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return load_config(config_path)
