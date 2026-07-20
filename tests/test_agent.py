from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from jobagent.agent import JobAgent
from jobagent.db import Database
from jobagent.models import JobMatch, LinkCandidate, PageDecision, PageSnapshot


class FakeBrowser:
    def __init__(self, pages: dict[str, PageSnapshot]) -> None:
        self.pages = pages
        self.opened: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def fetch(self, url: str) -> PageSnapshot:
        self.opened.append(url)
        if url not in self.pages:
            raise KeyError(url)
        return self.pages[url]


from jobagent.models import LinkClassification


class FakeLLM:
    def analyze_page(self, snapshot: PageSnapshot, links_with_context, memory_summary: str) -> PageDecision:
        if snapshot.url == "https://alpha.test/careers":
            return PageDecision(
                jobs=[],
                link_classifications=[
                    LinkClassification(index=0, type="job_listing", fit_score=92, title="Procurement Manager", company="Alpha", location="Munich, Germany", evidence="Procurement Manager", reason="Procurement role in Munich"),
                ],
                source_quality=85,
                source_notes="career index with relevant procurement links",
            )
        if snapshot.url == "https://alpha.test/jobs/procurement-manager":
            return PageDecision(
                jobs=[
                    JobMatch(
                        title="Procurement Manager",
                        company="Alpha",
                        location="Munich, Germany",
                        url=snapshot.url,
                        fit_score=92,
                        reason="Procurement role in Munich",
                        evidence="Procurement Manager",
                    )
                ],
                link_classifications=[],
                source_quality=95,
                source_notes="strong direct job detail page",
            )
        return PageDecision(jobs=[], link_classifications=[], source_quality=20, source_notes="not useful")

    def classify_links_batch(self, snapshot: PageSnapshot, links_with_context, memory_summary: str) -> PageDecision:
        decision = self.analyze_page(snapshot, links_with_context, memory_summary)
        ctx_by_index = {int(item["index"]): item["url"] for item in links_with_context}
        for c in decision.link_classifications:
            if not c.url and c.index in ctx_by_index:
                c.url = ctx_by_index[c.index]
        return decision


class ExploringLLM(FakeLLM):
    def analyze_page(self, snapshot: PageSnapshot, links_with_context, memory_summary: str) -> PageDecision:
        if snapshot.url == "https://alpha.test/start":
            return PageDecision(
                jobs=[],
                link_classifications=[
                    LinkClassification(index=0, type="explore", reason="Relevant job index")
                ],
                source_quality=70,
                source_notes="Relevant source to explore",
            )
        return PageDecision(jobs=[], link_classifications=[], source_quality=20, source_notes="No links")


@pytest.mark.parametrize("exploration_enabled", [False, True])
def test_agent_exploration_flag_controls_explore_enqueue(temp_loaded, exploration_enabled):
    start_url = "https://alpha.test/start"
    explore_url = "https://alpha.test/jobs"
    temp_loaded.config.exploration.enabled = exploration_enabled
    temp_loaded.paths.seeds_path.write_text(f"{start_url}\n", encoding="utf-8")
    pages = {
        start_url: PageSnapshot(
            url=start_url,
            final_url=start_url,
            title="Alpha",
            text="Procurement jobs in Munich",
            links=[LinkCandidate(text="Open jobs", url=explore_url)],
        ),
        explore_url: PageSnapshot(
            url=explore_url,
            final_url=explore_url,
            title="Alpha Jobs",
            text="Open positions",
            links=[],
        ),
    }
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: FakeBrowser(pages),
        llm_client=ExploringLLM(),
    )
    reporter = MagicMock()
    agent.reporter = reporter

    assert agent.run() == 0
    explore_row = db.conn.execute("select status from backlog where url = ?", (explore_url,)).fetchone()
    assert (explore_row is not None) is exploration_enabled
    if explore_row is not None:
        assert explore_row["status"] == "done"

    action_calls = reporter.action.call_args_list
    assert all(call.args[0] != "page_analyzed" for call in action_calls)

    batch_calls = [call for call in action_calls if call.args[0] == "batch_complete"]
    assert len(batch_calls) == 1
    assert set(batch_calls[0].kwargs) == {
        "batch", "saved", "high_fit", "enqueued", "queued", "source_quality", "source_notes"
    }
    assert batch_calls[0].kwargs["enqueued"] == int(exploration_enabled)

    page_calls = [call for call in action_calls if call.args[0] == "page_complete"]
    start_page_call = next(call for call in page_calls if call.kwargs["title"] == "Alpha")
    assert set(start_page_call.kwargs) == {
        "saved", "high_fit", "enqueued", "queued", "source_quality", "source_notes", "title"
    }
    db.close()


def test_agent_explores_follow_url_saves_job(temp_loaded):
    temp_loaded.paths.seeds_path.write_text("https://alpha.test/careers\n", encoding="utf-8")
    pages = {
        "https://alpha.test/careers": PageSnapshot(
            url="https://alpha.test/careers",
            final_url="https://alpha.test/careers",
            title="Alpha Careers",
            text="Procurement and supplier quality jobs in Munich",
            links=[LinkCandidate(text="Procurement Manager", url="https://alpha.test/jobs/procurement-manager")],
        ),
        "https://alpha.test/jobs/procurement-manager": PageSnapshot(
            url="https://alpha.test/jobs/procurement-manager",
            final_url="https://alpha.test/jobs/procurement-manager",
            title="Procurement Manager",
            text="Procurement Manager Munich Germany supplier quality sourcing",
            links=[],
        ),
    }
    fake_browser = FakeBrowser(pages)
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: fake_browser,
        llm_client=FakeLLM(),
    )

    assert agent.run() == 0
    assert db.count_rows("jobs") == 1
    job = db.conn.execute("select title, fit_score from jobs").fetchone()
    assert job["title"] == "Procurement Manager"
    assert job["fit_score"] == 92
    assert "https://alpha.test/jobs/procurement-manager" in fake_browser.opened
    db.close()


class FailingLLM(FakeLLM):
    def analyze_page(self, snapshot: PageSnapshot, links_with_context, memory_summary: str) -> PageDecision:
        raise RuntimeError("simulated local LLM failure")


def test_agent_does_not_save_job_when_llm_fails(temp_loaded):
    temp_loaded.paths.seeds_path.write_text("https://jobs.test/procurement-munich\n", encoding="utf-8")
    pages = {
        "https://jobs.test/procurement-munich": PageSnapshot(
            url="https://jobs.test/procurement-munich",
            final_url="https://jobs.test/procurement-munich",
            title="Procurement Manager Jobs in München",
            text="Procurement Manager Jobs in München. Einkauf Beschaffung Supply Chain.",
            links=[
                LinkCandidate(
                    text="Supplier Quality Manager Optics München",
                    url="https://jobs.test/jobs/supplier-quality-manager-optics-muenchen",
                )
            ],
        )
    }
    fake_browser = FakeBrowser(pages)
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: fake_browser,
        llm_client=FailingLLM(),
    )

    assert agent.run() == 0
    assert db.count_rows("jobs") == 0
    db.close()


class NoisyScoringLLM(FakeLLM):
    def analyze_page(self, snapshot: PageSnapshot, links_with_context, memory_summary: str) -> PageDecision:
        if snapshot.url == "https://noise.test/jobs":
            url_to_classification = {
                "https://noise.test/jobs/sales-representative-optics": LinkClassification(index=0, type="job_listing", fit_score=78, title="Sales Representative Optics", company="Noise GmbH", location="Munich, Germany", evidence="Sales Representative Optics", reason="LLM over-scored a sales role because optics was mentioned"),
                "https://noise.test/jobs/electronics-engineer-laser": LinkClassification(index=1, type="job_listing", fit_score=81, title="Electronics Engineer Laser Systems", company="Noise GmbH", location="München", evidence="Electronics Engineer Laser Systems", reason="LLM over-scored an engineering role because laser was mentioned"),
                "https://noise.test/jobs/supplier-quality-manager-optics": LinkClassification(index=2, type="job_listing", fit_score=86, title="Supplier Quality Manager Optical Components", company="Good GmbH", location="München", evidence="Supplier Quality Manager Optical Components", reason="Supplier quality management for optical components in München"),
            }
            classifications = [url_to_classification.get(url, LinkClassification(index=i, type="skip", fit_score=0)) for i, url in enumerate([lc["url"] for lc in links_with_context])]
            return PageDecision(
                jobs=[],
                link_classifications=classifications,
                source_quality=80,
                source_notes="mixed quality listing page",
            )
        # Detail pages - return the job from this detail page
        url_to_job = {
            "https://noise.test/jobs/sales-representative-optics": JobMatch(
                title="Sales Representative Optics",
                company="Noise GmbH",
                location="Munich, Germany",
                url="https://noise.test/jobs/sales-representative-optics",
                fit_score=78,
                reason="LLM over-scored a sales role because optics was mentioned",
                evidence="Sales Representative Optics",
            ),
            "https://noise.test/jobs/electronics-engineer-laser": JobMatch(
                title="Electronics Engineer Laser Systems",
                company="Noise GmbH",
                location="München",
                url="https://noise.test/jobs/electronics-engineer-laser",
                fit_score=81,
                reason="LLM over-scored an engineering role because laser was mentioned",
                evidence="Electronics Engineer Laser Systems",
            ),
            "https://noise.test/jobs/supplier-quality-manager-optics": JobMatch(
                title="Supplier Quality Manager Optical Components",
                company="Good GmbH",
                location="München",
                url="https://noise.test/jobs/supplier-quality-manager-optics",
                fit_score=86,
                reason="Supplier quality management for optical components in München",
                evidence="Supplier Quality Manager Optical Components",
            ),
        }
        job = url_to_job.get(snapshot.url)
        if job:
            return PageDecision(
                jobs=[job],
                link_classifications=[],
                source_quality=80,
                source_notes="mixed quality detail page",
            )
        return PageDecision(jobs=[], link_classifications=[], source_quality=80, source_notes="unknown detail page")


def test_agent_score_guard_filters_noisy_llm_matches(temp_loaded):
    temp_loaded.config.job_validation.require_loaded_job_detail_page = True
    temp_loaded.paths.seeds_path.write_text("https://noise.test/jobs\n", encoding="utf-8")
    pages = {
        "https://noise.test/jobs": PageSnapshot(
            url="https://noise.test/jobs",
            final_url="https://noise.test/jobs",
            title="Jobs",
            text="Supplier Quality Manager Optical Components Munich. Sales Representative Optics. Electronics Engineer Laser Systems.",
            links=[
                LinkCandidate(text="Sales Representative Optics", url="https://noise.test/jobs/sales-representative-optics"),
                LinkCandidate(text="Electronics Engineer Laser Systems", url="https://noise.test/jobs/electronics-engineer-laser"),
                LinkCandidate(text="Supplier Quality Manager Optical Components", url="https://noise.test/jobs/supplier-quality-manager-optics"),
            ],
        ),
        "https://noise.test/jobs/sales-representative-optics": PageSnapshot(
            url="https://noise.test/jobs/sales-representative-optics",
            final_url="https://noise.test/jobs/sales-representative-optics",
            title="Sales Representative Optics",
            text="Sales Representative Optics Munich",
            links=[],
        ),
        "https://noise.test/jobs/electronics-engineer-laser": PageSnapshot(
            url="https://noise.test/jobs/electronics-engineer-laser",
            final_url="https://noise.test/jobs/electronics-engineer-laser",
            title="Electronics Engineer Laser Systems",
            text="Electronics Engineer Laser Systems München",
            links=[],
        ),
        "https://noise.test/jobs/supplier-quality-manager-optics": PageSnapshot(
            url="https://noise.test/jobs/supplier-quality-manager-optics",
            final_url="https://noise.test/jobs/supplier-quality-manager-optics",
            title="Supplier Quality Manager Optical Components",
            text="Supplier Quality Manager Optical Components München",
            links=[],
        ),
    }
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: FakeBrowser(pages),
        llm_client=NoisyScoringLLM(),
    )
    assert agent.run() == 0
    rows = db.conn.execute("select title, fit_score from jobs order by fit_score desc").fetchall()
    assert [row["title"] for row in rows] == [
        "Supplier Quality Manager Optical Components",
        "Electronics Engineer Laser Systems",
        "Sales Representative Optics",
    ]
    db.close()


class UnavailableLLM(FakeLLM):
    def health_check(self):
        return False, "ConnectionError: connection refused"


def test_agent_stops_before_browsing_when_llm_unavailable(temp_loaded):
    temp_loaded.paths.seeds_path.write_text("https://alpha.test/careers\n", encoding="utf-8")
    fake_browser = FakeBrowser({})
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: fake_browser,
        llm_client=UnavailableLLM(),
    )
    assert agent.run() == 2
    assert fake_browser.opened == []
    db.close()


class ConnectionFailingLLM(FakeLLM):
    def analyze_page(self, snapshot: PageSnapshot, links_with_context, memory_summary: str) -> PageDecision:
        raise ConnectionError("Failed to establish a new connection: [Errno 111] Connection refused")


def test_agent_stops_on_midrun_llm_connection_error_without_expanding_queue(temp_loaded):
    temp_loaded.config.llm.require_available_on_start = False
    temp_loaded.paths.seeds_path.write_text("https://alpha.test/careers\n", encoding="utf-8")
    pages = {
        "https://alpha.test/careers": PageSnapshot(
            url="https://alpha.test/careers",
            final_url="https://alpha.test/careers",
            title="Alpha Careers",
            text="Procurement jobs",
            links=[LinkCandidate(text="Procurement Manager", url="https://alpha.test/jobs/procurement-manager")],
        ),
        "https://alpha.test/jobs/procurement-manager": PageSnapshot(
            url="https://alpha.test/jobs/procurement-manager",
            final_url="https://alpha.test/jobs/procurement-manager",
            title="Procurement Manager",
            text="Procurement Manager Munich",
            links=[],
        ),
    }
    fake_browser = FakeBrowser(pages)
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: fake_browser,
        llm_client=ConnectionFailingLLM(),
    )
    assert agent.run() == 0
    # Link page_context is fetched before LLM call, so the link URL is opened
    assert "https://alpha.test/careers" in fake_browser.opened
    assert "https://alpha.test/jobs/procurement-manager" in fake_browser.opened
    assert db.queued_count() == 0
    db.close()
