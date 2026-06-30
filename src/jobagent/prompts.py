from __future__ import annotations

from pathlib import Path
from string import Template
from typing import Mapping

import yaml


class PromptBook:
    def __init__(self, prompts: dict[str, str]) -> None:
        self._prompts = prompts

    @classmethod
    def from_file(cls, path: Path) -> "PromptBook":
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if not isinstance(raw, dict):
            raise ValueError("prompts file must contain a YAML mapping")
        prompts = {str(k): str(v) for k, v in raw.items()}
        return cls(prompts)

    def render(self, name: str, values: Mapping[str, object]) -> str:
        if name not in self._prompts:
            raise KeyError(f"missing prompt template: {name}")
        text_values = {k: str(v) for k, v in values.items()}
        return Template(self._prompts[name]).safe_substitute(text_values)
