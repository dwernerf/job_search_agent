from __future__ import annotations

import json
import math
import os
from typing import Any

import requests

from .config import JobAgentConfig
from .extract import compact_text, page_decision_from_dict, parse_json_object, query_suggestions_from_dict
from .language import language_policy_summary, multilingual_job_terms, multilingual_role_terms
from .location import location_radius_summary
from .models import LinkCandidate, PageDecision, PageSnapshot, QuerySuggestion
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

    def _estimate_tokens(self, *parts: str) -> int:
        chars = sum(len(part or "") for part in parts)
        return int(math.ceil(chars / self.config.llm.token_estimate_chars_per_token))

    def _clip(self, text: str, max_chars: int) -> str:
        value = text or ""
        if len(value) <= max_chars:
            return value
        marker = f"\n[TRUNCATED to {max_chars} characters by llm.max_* prompt budget]\n"
        keep = max(0, max_chars - len(marker))
        return value[:keep] + marker

    def _links_json_for_prompt(self, links: list[LinkCandidate], limit: int) -> str:
        items: list[dict[str, Any]] = []
        for link in links[:limit]:
            items.append(
                {
                    "text": self._clip(link.text, self.config.llm.max_candidate_link_text_chars),
                    "url": self._clip(link.url, self.config.llm.max_candidate_link_url_chars),
                    "score": round(link.score, 2),
                    "reason": self._clip(link.reason, 120),
                }
            )
        return json.dumps(items, ensure_ascii=False)

    def _common_values(self, memory_summary: str) -> dict[str, str]:
        return {
            "no_think_prefix": self.config.llm.no_think_prefix if self.config.llm.disable_thinking else "",
            "profile": self._clip(self.profile_text, self.config.llm.max_profile_chars),
            "local_area": self.config.target.local_area,
            "roles": ", ".join(self.config.target.roles),
            "target_languages": ", ".join(self.config.target.languages),
            "language_policy": self._clip(language_policy_summary(self.config), 500),
            "location_policy": location_radius_summary(self.config),
            "location_radius_policy": location_radius_summary(self.config),
            "company_scope_policy": (
                "Whitelist-only mode is active. Extract and follow only postings/pages for these companies: "
                + ", ".join(self.config.companies.whitelist)
                if self.config.exploration.mode == "whitelist_only" and self.config.companies.whitelist
                else "No company whitelist restriction is active for this run."
            ),
            "multilingual_role_terms": self._clip(", ".join(multilingual_role_terms(self.config)), 700),
            "multilingual_job_terms": self._clip(", ".join(multilingual_job_terms(self.config)), 700),
            "location_aliases": self._clip(", ".join(self.config.matching.location_aliases), 400),
            "preferred_terms": self._clip(", ".join(self.config.matching.preferred_terms), 800),
            "avoid_terms": self._clip(", ".join(self.config.matching.avoid_terms), 700),
            "memory_summary": self._clip(memory_summary, self.config.llm.max_memory_chars),
        }

    def _render_page_prompts(
        self,
        snapshot: PageSnapshot,
        candidate_links: list[LinkCandidate],
        memory_summary: str,
    ) -> tuple[str, str]:
        link_limit = min(len(candidate_links), self.config.llm.max_candidate_links_for_prompt)
        text_limit = self.config.llm.max_page_text_chars_for_prompt
        profile_limit = self.config.llm.max_profile_chars
        memory_limit = self.config.llm.max_memory_chars

        last_system = ""
        last_user = ""
        for _ in range(18):
            values = self._common_values(memory_summary[:memory_limit])
            values["profile"] = self._clip(self.profile_text, profile_limit)
            values.update(
                {
                    "url": snapshot.url,
                    "final_url": snapshot.final_url,
                    "title": self._clip(snapshot.title, 500),
                    "http_status_code": str(getattr(snapshot, "status_code", 0) or 0),
                    "text": self._clip(compact_text(snapshot.text, self.config), text_limit),
                    "candidate_links_json": self._links_json_for_prompt(candidate_links, link_limit),
                }
            )
            system = self.prompt_book.render("page_analysis_system", values)
            user = self.prompt_book.render("page_analysis_user", values)
            last_system, last_user = system, user
            if self._estimate_tokens(system, user) <= self.config.llm.max_prompt_tokens:
                return system, user

            if link_limit > self.config.llm.min_candidate_links_for_prompt:
                link_limit = max(self.config.llm.min_candidate_links_for_prompt, int(link_limit * 0.65))
            elif text_limit > self.config.llm.min_page_text_chars_for_prompt:
                text_limit = max(self.config.llm.min_page_text_chars_for_prompt, int(text_limit * 0.65))
            elif memory_limit > 600:
                memory_limit = max(600, int(memory_limit * 0.65))
            elif profile_limit > 1400:
                profile_limit = max(1400, int(profile_limit * 0.65))
            elif link_limit > 3:
                link_limit = 3
            elif text_limit > 700:
                text_limit = 700
            else:
                break

        # Last-resort hard cap: keep the JSON instructions at the end of the prompt
        # by trimming only the large variable fields through another render.
        values = self._common_values(memory_summary[:500])
        values["profile"] = self._clip(self.profile_text, 800)
        values.update(
            {
                "url": snapshot.url,
                "final_url": snapshot.final_url,
                "title": self._clip(snapshot.title, 200),
                "http_status_code": str(getattr(snapshot, "status_code", 0) or 0),
                "text": self._clip(compact_text(snapshot.text, self.config), 400),
                "candidate_links_json": self._links_json_for_prompt(candidate_links, min(2, len(candidate_links))),
            }
        )
        system = self.prompt_book.render("page_analysis_system", values)
        user = self.prompt_book.render("page_analysis_user", values)
        if self._estimate_tokens(system, user) <= self.config.llm.max_prompt_tokens:
            return system, user
        # Should be rare; throw a local error with an actionable message rather than
        # sending an over-context request to llama.cpp.
        raise LLMResponseError(
            f"LLM prompt still exceeds configured budget after truncation: estimated_prompt_tokens={self._estimate_tokens(system, user)}, allowed_prompt_tokens={self.config.llm.max_prompt_tokens}"
        )

    def _render_query_prompts(self, memory_summary: str, run_summary: str) -> tuple[str, str]:
        memory_limit = self.config.llm.max_memory_chars
        profile_limit = self.config.llm.max_profile_chars
        last_system = ""
        last_user = ""
        for _ in range(12):
            values = self._common_values(memory_summary[:memory_limit])
            values["profile"] = self._clip(self.profile_text, profile_limit)
            values["run_summary"] = self._clip(run_summary, 900)
            system = self.prompt_book.render("query_generation_system", values)
            user = self.prompt_book.render("query_generation_user", values)
            last_system, last_user = system, user
            if self._estimate_tokens(system, user) <= self.config.llm.max_prompt_tokens:
                return system, user
            if memory_limit > 500:
                memory_limit = max(500, int(memory_limit * 0.6))
            elif profile_limit > 1200:
                profile_limit = max(1200, int(profile_limit * 0.6))
            else:
                break
        return last_system, last_user

    def chat_json(self, system_prompt: str, user_prompt: str, temperature: float, max_tokens: int) -> dict[str, Any]:
        estimated = self._estimate_tokens(system_prompt, user_prompt)
        if estimated > self.config.llm.max_prompt_tokens:
            raise LLMResponseError(
                f"Refusing to send over-budget LLM request: estimated_prompt_tokens={estimated}, allowed_prompt_tokens={self.config.llm.max_prompt_tokens}"
            )

        payload: dict[str, Any] = {
            "model": self.config.llm.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
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
            estimated = self._estimate_tokens(system_prompt, user_prompt)
            raise LLMResponseError(
                "LLM HTTP request failed",
                status_code=response.status_code,
                response_text=(
                    response.text
                    + f" | estimated_prompt_tokens={estimated}"
                    + f" | allowed_prompt_tokens={self.config.llm.max_prompt_tokens}"
                    + f" | configured_context_window_tokens={self.config.llm.context_window_tokens}"
                    + f" | automatic_safety_margin_tokens={self.config.llm.prompt_safety_margin_tokens}"
                    + f" | configured_output_tokens={max_tokens}"
                ),
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
        candidate_links: list[LinkCandidate],
        memory_summary: str,
    ) -> PageDecision:
        system, user = self._render_page_prompts(snapshot, candidate_links, memory_summary)
        return page_decision_from_dict(
            self.chat_json(system, user, self.config.llm.temperature, self.config.llm.max_tokens)
        )

    def generate_queries(self, memory_summary: str, run_summary: str) -> list[QuerySuggestion]:
        system, user = self._render_query_prompts(memory_summary, run_summary)
        data = self.chat_json(
            system,
            user,
            self.config.exploration.generated_query_temperature,
            self.config.llm.max_tokens,
        )
        return query_suggestions_from_dict(data)
