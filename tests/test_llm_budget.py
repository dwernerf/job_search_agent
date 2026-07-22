from __future__ import annotations

import re

from jobagent.llm import LocalLLMClient
from jobagent.models import LinkCandidate, PageSnapshot
from jobagent.prompts import PromptBook


def test_page_prompt_contains_link_context_and_no_unresolved_placeholders(temp_loaded):
    prompt_book = PromptBook.from_file(temp_loaded.paths.prompts_path)
    client = LocalLLMClient(
        temp_loaded.config,
        prompt_book,
        "Procurement and supplier quality profile",
    )
    snapshot = PageSnapshot(
        url="https://example.test/careers",
        final_url="https://example.test/careers",
        title="Careers",
        text="SOURCE_BODY_MUST_NOT_BE_INCLUDED",
        links=[LinkCandidate(text="Procurement Manager", url="https://example.test/jobs/1")],
    )
    links_with_context = [
        {
            "index": "0",
            "text": "Procurement Manager",
            "url": "https://example.test/jobs/1",
            "page_context": "Strategic sourcing responsibilities in Munich",
        }
    ]

    system, user = client._render_page_prompts(snapshot, links_with_context)

    rendered = system + "\n" + user
    assert "Procurement and supplier quality profile" in rendered
    assert "https://example.test/careers" in rendered
    assert "Careers" in rendered
    assert "SOURCE_BODY_MUST_NOT_BE_INCLUDED" not in rendered
    assert "https://example.test/jobs/1" in rendered
    assert "Strategic sourcing responsibilities in Munich" in rendered
    assert "likelihood that browsing through this URL" in rendered
    assert '"type": "explore", "fit_score": 82' in rendered
    assert re.search(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}]+\})", rendered) is None


def test_classification_sends_large_prompt_without_local_budget_check(
    temp_loaded,
    monkeypatch,
):
    client = LocalLLMClient(
        temp_loaded.config,
        PromptBook.from_file(temp_loaded.paths.prompts_path),
        "x" * 20000,
    )
    snapshot = PageSnapshot(
        url="https://example.test/careers",
        final_url="https://example.test/careers",
        title="Careers",
        text="Open roles",
    )
    captured: dict[str, str] = {}

    def fake_chat_json(system_prompt: str, user_prompt: str):
        captured["system"] = system_prompt
        captured["user"] = user_prompt
        return {"link_classifications": []}

    monkeypatch.setattr(client, "chat_json", fake_chat_json)

    decision = client.classify_links_batch(snapshot, [])

    assert decision.link_classifications == []
    assert "x" * 20000 in captured["user"]
