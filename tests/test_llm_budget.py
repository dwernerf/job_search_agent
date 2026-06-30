from __future__ import annotations

from jobagent.llm import LocalLLMClient
from jobagent.models import LinkCandidate, PageSnapshot
from jobagent.prompts import PromptBook


def test_page_prompt_is_trimmed_to_configured_budget(temp_loaded):
    prompt_book = PromptBook.from_file(temp_loaded.paths.prompts_path)
    profile = "Procurement supplier quality optics laser mechanical components. " * 500
    client = LocalLLMClient(temp_loaded.config, prompt_book, profile)
    snapshot = PageSnapshot(
        url="https://example.test/jobs",
        final_url="https://example.test/jobs",
        title="Large job page",
        text=("Procurement Manager München Supplier Quality Optics Laser. " * 3000),
        links=[
            LinkCandidate(
                text=f"Procurement Manager Optical Components München {i} " * 5,
                url=f"https://example.test/jobs/procurement-manager-optics-munich-{i}?with=a-very-long-query-string-that-should-be-trimmed",
                score=10,
                reason="role location preferred terms " * 5,
            )
            for i in range(120)
        ],
    )
    system, user = client._render_page_prompts(snapshot, snapshot.links, "memory line\n" * 1000)
    assert client._estimate_tokens(system, user) <= temp_loaded.config.llm.max_prompt_tokens
    assert "Procurement Manager" in user
    assert "Return JSON only" in user
