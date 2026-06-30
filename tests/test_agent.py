from __future__ import annotations

from dataclasses import dataclass

from jobagent.agent import JobAgent
from jobagent.db import Database
from jobagent.models import JobMatch, LinkCandidate, PageDecision, PageSnapshot, QuerySuggestion


class AllowAllRobots:
    def allowed(self, url: str) -> bool:
        return True


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


class FakeLLM:
    def __init__(self) -> None:
        self.query_calls = 0

    def analyze_page(self, snapshot: PageSnapshot, candidate_links, memory_summary: str) -> PageDecision:
        if snapshot.url == "https://alpha.test/careers":
            return PageDecision(
                jobs=[],
                follow_urls=["https://alpha.test/jobs/procurement-manager"],
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
                follow_urls=[],
                source_quality=95,
                source_notes="strong direct job detail page",
            )
        return PageDecision(jobs=[], follow_urls=[], source_quality=20, source_notes="not useful")

    def generate_queries(self, memory_summary: str, run_summary: str):
        self.query_calls += 1
        return [QuerySuggestion(query="Procurement Manager Munich careers", reason="local procurement role discovery")]


class QueryOnlyLLM(FakeLLM):
    def analyze_page(self, snapshot: PageSnapshot, candidate_links, memory_summary: str) -> PageDecision:
        return PageDecision(jobs=[], follow_urls=[], source_quality=40, source_notes="search page")


def test_agent_explores_follow_url_saves_job_and_learns(temp_loaded):
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
        robots=AllowAllRobots(),
    )

    assert agent.run() == 0
    assert db.count_rows("jobs") == 1
    job = db.conn.execute("select title, fit_score from jobs").fetchone()
    assert job["title"] == "Procurement Manager"
    assert job["fit_score"] == 92
    learned = db.get_source("alpha.test/jobs")
    assert learned.score > temp_loaded.config.memory.initial_score
    assert "https://alpha.test/jobs/procurement-manager" in fake_browser.opened
    db.close()


def test_agent_generates_query_when_frontier_empty(temp_loaded):
    temp_loaded.paths.seeds_path.write_text("", encoding="utf-8")
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    fake_browser = FakeBrowser({})
    fake_llm = FakeLLM()
    agent = JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: fake_browser,
        llm_client=fake_llm,
        robots=AllowAllRobots(),
    )
    agent.run()
    assert fake_llm.query_calls >= 1
    assert db.count_rows("queries") == 1
    assert db.queued_count() >= 0
    db.close()


class FailingLLM(FakeLLM):
    def analyze_page(self, snapshot: PageSnapshot, candidate_links, memory_summary: str) -> PageDecision:
        raise RuntimeError("simulated local LLM failure")


def test_agent_does_not_save_heuristic_job_when_disabled_and_llm_fails(temp_loaded):
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
        robots=AllowAllRobots(),
    )

    assert agent.run() == 0
    assert db.count_rows("jobs") == 0
    db.close()


def test_agent_can_save_heuristic_job_when_explicitly_enabled_and_llm_fails(temp_loaded):
    temp_loaded.config.heuristic_extraction.enabled = True
    temp_loaded.config.heuristic_extraction.suppress_link_jobs_on_index_pages = False
    temp_loaded.config.job_validation.require_loaded_job_detail_page = False
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
        robots=AllowAllRobots(),
    )

    assert agent.run() == 0
    assert db.count_rows("jobs") == 1
    job = db.conn.execute("select title, reason from jobs").fetchone()
    assert "Supplier Quality Manager" in job["title"]
    assert "Heuristic" in job["reason"]
    db.close()

class NoisyScoringLLM(FakeLLM):
    def analyze_page(self, snapshot: PageSnapshot, candidate_links, memory_summary: str) -> PageDecision:
        return PageDecision(
            jobs=[
                JobMatch(
                    title="Sales Representative Optics",
                    company="Noise GmbH",
                    location="Munich, Germany",
                    url="https://noise.test/jobs/sales-representative-optics",
                    fit_score=78,
                    reason="LLM over-scored a sales role because optics was mentioned",
                    evidence="Sales Representative Optics",
                ),
                JobMatch(
                    title="Electronics Engineer Laser Systems",
                    company="Noise GmbH",
                    location="München",
                    url="https://noise.test/jobs/electronics-engineer-laser",
                    fit_score=81,
                    reason="LLM over-scored an engineering role because laser was mentioned",
                    evidence="Electronics Engineer Laser Systems",
                ),
                JobMatch(
                    title="Supplier Quality Manager Optical Components",
                    company="Good GmbH",
                    location="München",
                    url="https://noise.test/jobs/supplier-quality-manager-optics",
                    fit_score=86,
                    reason="Supplier quality management for optical components in München",
                    evidence="Supplier Quality Manager Optical Components",
                ),
            ],
            follow_urls=[],
            source_quality=80,
            source_notes="mixed quality page",
        )


def test_agent_score_guard_filters_noisy_llm_matches(temp_loaded):
    temp_loaded.config.job_validation.require_loaded_job_detail_page = False
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
        )
    }
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: FakeBrowser(pages),
        llm_client=NoisyScoringLLM(),
        robots=AllowAllRobots(),
    )
    assert agent.run() == 0
    rows = db.conn.execute("select title, fit_score, score_source, score_basis from jobs order by fit_score desc").fetchall()
    assert [row["title"] for row in rows] == ["Supplier Quality Manager Optical Components"]
    assert rows[0]["fit_score"] == 86
    assert rows[0]["score_source"] == "llm"
    assert "target role signal" in rows[0]["score_basis"]
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
        robots=AllowAllRobots(),
    )
    assert agent.run() == 2
    assert fake_browser.opened == []
    db.close()


class ConnectionFailingLLM(FakeLLM):
    def analyze_page(self, snapshot: PageSnapshot, candidate_links, memory_summary: str) -> PageDecision:
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
        )
    }
    fake_browser = FakeBrowser(pages)
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: fake_browser,
        llm_client=ConnectionFailingLLM(),
        robots=AllowAllRobots(),
    )
    assert agent.run() == 0
    assert fake_browser.opened == ["https://alpha.test/careers"]
    assert db.queued_count() == 0
    db.close()
