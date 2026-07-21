from __future__ import annotations

import re

import pytest

from jobagent.llm import ContextWindowExceeded, LocalLLMClient
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
        text="Open roles in Munich",
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
    assert "https://example.test/jobs/1" in rendered
    assert "Strategic sourcing responsibilities in Munich" in rendered
    assert "likelihood that browsing through this URL" in rendered
    assert '"type": "explore", "fit_score": 82' in rendered
    assert re.search(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}]+\})", rendered) is None


def test_classification_rejects_prompt_larger_than_context_window(temp_loaded):
    temp_loaded.config.llm.context_window_tokens = 3001
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

    with pytest.raises(ContextWindowExceeded):
        client.classify_links_batch(snapshot, [])
