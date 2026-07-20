from __future__ import annotations

import json
import os
from typing import Any

import requests

from .config import JobAgentConfig
from .extract import compact_text, page_decision_from_dict, parse_json_object
from .language import language_policy_summary
from .models import PageDecision, PageSnapshot
from .prompts import PromptBook


class LLMResponseError(RuntimeError):
    pass


class ContextWindowExceeded(ValueError):
    """Raised when the prompt would exceed the configured context window."""
    pass


class LocalLLMClient:
    def __init__(self, config: JobAgentConfig, prompt_book: PromptBook, profile_text: str) -> None:
        self.config = config
        self.prompt_book = prompt_book
        self.profile_text = profile_text
        self.base_url = config.llm.base_url.rstrip("/")
        self.api_key = os.environ.get(config.llm.api_key_env, config.llm.api_key_fallback)


    def health_check(self) -> tuple[bool, str]:
        endpoint = f"{self.base_url}{self.config.llm.models_endpoint}"
        try:
            response = requests.get(
                endpoint,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.config.llm.health_check_timeout_seconds,
            )
        except requests.exceptions.RequestException as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if response.status_code >= 400:
            return False, f"HTTP {response.status_code}: {response.text[:500]}"
        return True, "ok"

    def _clip(self, text: str, max_chars: int) -> str:
        value = text or ""
        if len(value) <= max_chars:
            return value
        marker = f"\n[TRUNCATED to {max_chars} characters for the prompt]\n"
        keep = max(0, max_chars - len(marker))
        return value[:keep] + marker

    def _links_json_for_prompt(self, links_with_context: list[dict[str, str]]) -> str:
        items: list[dict[str, Any]] = []
        for item in links_with_context:
            entry = {
                "index": item.get("index", 0),
                "text": item.get("text") or "",
                "url": item.get("url") or "",
                "page_context": (item.get("page_context") or "")[:self.config.crawler.max_page_context_chars],
            }
            items.append(entry)
        return json.dumps(items, ensure_ascii=False)

    def _common_values(self) -> dict[str, str]:
        return {
            "no_think_prefix": "" if self.config.llm.thinking_enabled else "/no_think",
            "profile": self.profile_text,
            "local_area": self.config.target.local_area,
            "target_languages": ", ".join(self.config.target.languages),
            "language_policy": self._clip(language_policy_summary(self.config), 500),
            "min_score_to_export": str(self.config.scoring.min_score_to_export),
        }

    def _render_page_prompts(
        self,
        snapshot: PageSnapshot,
        links_with_context: list[dict[str, str]],
    ) -> tuple[str, str]:
        values = self._common_values()
        values.update(
            {
                "url": snapshot.url,
                "final_url": snapshot.final_url,
                "title": self._clip(snapshot.title, 500),
                "http_status_code": str(getattr(snapshot, "status_code", 0) or 0),
                "text": compact_text(snapshot.text, self.config),
                "links_with_context": self._links_json_for_prompt(links_with_context),
            }
        )
        system = self.prompt_book.render("page_analysis_system", values)
        user = self.prompt_book.render("page_analysis_user", values)
        return system, user

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.llm.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.config.llm.temperature,
        }

        if self.config.llm.response_format_type:
            payload["response_format"] = {"type": self.config.llm.response_format_type}

        if not self.config.llm.thinking_enabled:
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        endpoint = f"{self.base_url}{self.config.llm.chat_endpoint}"

        response = requests.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=self.config.llm.timeout_seconds,
        )

        if response.status_code >= 400 and ("response_format" in payload or "chat_template_kwargs" in payload):
            fallback_payload = dict(payload)
            fallback_payload.pop("response_format", None)
            fallback_payload.pop("chat_template_kwargs", None)
            response = requests.post(
                endpoint,
                headers=headers,
                json=fallback_payload,
                timeout=self.config.llm.timeout_seconds,
            )

        if response.status_code >= 400:
            raise LLMResponseError(
                f"LLM HTTP request failed: status={response.status_code} "
                f"response={response.text[:900]}",
            )

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            raise LLMResponseError(
                f"LLM response did not contain choices[0].message.content: "
                f"{type(exc).__name__}; response={response.text[:900]}",
            ) from exc

        try:
            return parse_json_object(content)
        except Exception as exc:
            raise LLMResponseError(
                f"LLM response was not valid JSON: {type(exc).__name__}: {exc}; "
                f"content={str(content)[:900]}",
            ) from exc

    def _estimate_prompt_size(self, system: str, user: str) -> int:
        """Estimate the number of tokens in the prompt using char/4 heuristic."""
        total_chars = len(system) + len(user)
        # Add ~3 tokens for the system/user role markers
        return total_chars // 4 + 6

    def classify_links_batch(
        self,
        snapshot: PageSnapshot,
        links_with_context: list[dict[str, str]],
    ) -> PageDecision:
        """Classify a batch of links and return the PageDecision."""
        system, user = self._render_page_prompts(snapshot, links_with_context)
        estimated = self._estimate_prompt_size(system, user)
        max_allowed = self.config.llm.context_window_tokens - 3000
        if estimated > max_allowed:
            raise ContextWindowExceeded(
                f"Prompt size estimate {estimated} tokens exceeds available budget {max_allowed} tokens. "
                f"Reduce batch_size_for_llm or max_page_context_chars."
            )
        return page_decision_from_dict(
            self.chat_json(system, user)
        )
