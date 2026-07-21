from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from jobagent.config import LoadedConfig, load_config


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def sample_config_path(tmp_path: Path) -> Path:
    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
    return tmp_path / "config" / "config.yaml"


@pytest.fixture()
def loaded_sample(sample_config_path: Path) -> LoadedConfig:
    return load_config(sample_config_path)


@pytest.fixture()
def temp_loaded(sample_config_path: Path) -> LoadedConfig:
    config_path = sample_config_path
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["run"]["min_delay_seconds"] = 0
    data["run"]["max_delay_seconds"] = 0
    data["browser"]["headless"] = True
    data["logging"]["console"] = False
    data["logging"]["file"] = False
    data["seeding"]["mode"] = "seeds"

    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    intent_path = config_path.parent / "intent.yaml"
    intent_data = yaml.safe_load(intent_path.read_text(encoding="utf-8"))
    intent_data["location"] = {
        "local_area": "Munich, Bavaria, Germany",
    }
    intent_data["companies"] = {
        "blacklist": [],
        "whitelist": ["Zeiss", "Trumpf", "Rohde-Schwarz"],
    }
    intent_path.write_text(yaml.safe_dump(intent_data, sort_keys=False), encoding="utf-8")

    return load_config(config_path)
