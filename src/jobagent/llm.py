from __future__ import annotations

import json
import math
import os
from typing import Any

import requests

from .config import JobAgentConfig
from .extract import compact_text, page_decision_from_dict, parse_json_object
from .language import language_policy_summary, multilingual_job_terms, multilingual_role_terms
from .models import LinkCandidate, PageDecision, PageSnapshot
from .prompts import PromptBook


class LLMResponseError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, response_text: str = "", raw_content: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text
        self.raw_content = raw_content

    def compact(self, limit: int = 900) -> str:
        parts = [str(self)]
        if self.status_code is not None:
            parts.append(f"status_code={self.status_code}")
        if self.response_text:
            parts.append("response=" + self.response_text[:limit])
        if self.raw_content:
            parts.append("raw_content=" + self.raw_content[:limit])
        return " | ".join(parts)


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
        marker = f"\n[TRUNCATED to {max_chars} characters by llm.max_* prompt budget]\n"
        keep = max(0, max_chars - len(marker))
        return value[:keep] + marker

    def _links_json_for_prompt(self, links_or_context: list[dict[str, str]]) -> str:
        items: list[dict[str, Any]] = []
        for item in links_or_context:
            # Accept both dict format (from agent.py) and LinkCandidate (from tests)
            if isinstance(item, dict):
                entry = {
                    "index": item.get("index", 0),
                    "text": item.get("text") or "",
                    "url": item.get("url") or "",
                    "page_context": (item.get("page_context") or "")[:8000],
                }
            else:
                # LinkCandidate fallback
                entry = {
                    "index": getattr(item, "index", 0),
                    "text": getattr(item, "text", "") or "",
                    "url": getattr(item, "url", "") or "",
                    "page_context": (getattr(item, "page_context", "") or "")[:8000],
                }
            items.append(entry)
        return json.dumps(items, ensure_ascii=False)

    def _common_values(self) -> dict[str, str]:
        return {
            "no_think_prefix": self.config.llm.no_think_prefix if self.config.llm.disable_thinking else "",
            "profile": self.profile_text[:50000],
            "local_area": self.config.target.local_area,
            "roles": ", ".join(self.config.target.roles),
            "target_languages": ", ".join(self.config.target.languages),
            "language_policy": self._clip(language_policy_summary(self.config), 500),
            "multilingual_role_terms": self._clip(", ".join(multilingual_role_terms(self.config)), 700),
            "multilingual_job_terms": self._clip(", ".join(multilingual_job_terms(self.config)), 700),
            "location_aliases": self._clip(", ".join(self.config.matching.location_aliases), 400),
            "preferred_terms": self._clip(", ".join(self.config.matching.preferred_terms), 800),
            "avoid_terms": self._clip(", ".join(self.config.matching.avoid_terms), 700),
        }

    def _render_page_prompts(
        self,
        snapshot: PageSnapshot,
        links_with_context: list[dict[str, str]],
        memory_summary: str,
    ) -> tuple[str, str]:
        values = self._common_values()
        values.update(
            {
                "url": snapshot.url,
                "final_url": snapshot.final_url,
                "title": self._clip(snapshot.title, 500),
                "http_status_code": str(getattr(snapshot, "status_code", 0) or 0),
                "text": self._clip(compact_text(snapshot.text, self.config), 14000),
                "links_with_context": self._links_json_for_prompt(links_with_context),
            }
        )
        system = self.prompt_book.render("page_analysis_system", values)
        user = self.prompt_book.render("page_analysis_user", values)
        return system, user

    def chat_json(self, system_prompt: str, user_prompt: str, temperature: float) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.llm.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
        }

        if self.config.llm.response_format_type:
            payload["response_format"] = {"type": self.config.llm.response_format_type}

        if self.config.llm.disable_thinking:
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
                "LLM HTTP request failed",
                status_code=response.status_code,
                response_text=response.text,
            )

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            raise LLMResponseError(
                f"LLM response did not contain choices[0].message.content: {type(exc).__name__}",
                response_text=response.text,
            ) from exc

        try:
            return parse_json_object(content)
        except Exception as exc:
            raise LLMResponseError(
                f"LLM response was not valid JSON: {type(exc).__name__}: {exc}",
                raw_content=str(content),
            ) from exc

    def analyze_page(
        self,
        snapshot: PageSnapshot,
        links_with_context: list[dict[str, str]],
        memory_summary: str,
    ) -> PageDecision:
        system, user = self._render_page_prompts(snapshot, links_with_context, memory_summary)
        return page_decision_from_dict(
            self.chat_json(system, user, self.config.llm.temperature)
        )

    def _estimate_prompt_size(self, system: str, user: str) -> int:
        """Estimate the number of tokens in the prompt using char/4 heuristic."""
        total_chars = len(system) + len(user)
        # Add ~3 tokens for the system/user role markers
        return total_chars // 4 + 6

    def classify_links_batch(
        self,
        snapshot: PageSnapshot,
        links_with_context: list[dict[str, str]],
        memory_summary: str,
    ) -> PageDecision:
        """Classify a batch of links and return the PageDecision."""
        system, user = self._render_page_prompts(snapshot, links_with_context, memory_summary)
        estimated = self._estimate_prompt_size(system, user)
        max_allowed = self.config.llm.context_window_tokens - 3000
        if estimated > max_allowed:
            raise ContextWindowExceeded(
                f"Prompt size estimate {estimated} tokens exceeds available budget {max_allowed} tokens. "
                f"Reduce batch_size_for_llm or link page_context size."
            )
        decision = self.analyze_page(snapshot, links_with_context, memory_summary)
        # The LLM prompt explicitly omits URLs (see prompts.yaml:58). Inject them
        # from links_with_context using the classification index.
        ctx_by_index = {int(item["index"]): item["url"] for item in links_with_context}
        for c in decision.link_classifications:
            if not c.url and c.index in ctx_by_index:
                c.url = ctx_by_index[c.index]
        return decision


