from __future__ import annotations

import csv

from jobagent.agent import JobAgent
from jobagent.db import Database
from jobagent.discover import exploration_url_allowed, seed_frontier
from jobagent.location import evaluate_exploration_url_location, is_location_only_title
from jobagent.models import JobMatch, LinkCandidate, PageDecision, PageSnapshot
from jobagent.urltools import clean_url


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
        return self.pages[url]


class StaticLLM:
    def __init__(self, decision: PageDecision) -> None:
        self.decision = decision

    def analyze_page(self, snapshot: PageSnapshot, candidate_links, memory_summary: str) -> PageDecision:
        return self.decision

    def generate_queries(self, memory_summary: str, run_summary: str):
        return []


def test_exploration_url_filter_rejects_known_outside_city(temp_loaded):
    url = "https://www.stellenanzeigen.de/jobs/procurement/erlangen"
    verdict = evaluate_exploration_url_location(url, "Procurement jobs", temp_loaded.config)
    assert not verdict.allowed
    assert verdict.matched_place == "Erlangen"
    assert not exploration_url_allowed(url, "Procurement jobs", temp_loaded.config)


def test_location_only_title_is_rejected(temp_loaded):
    assert is_location_only_title("Erlangen", temp_loaded.config)
    assert is_location_only_title("München, Germany", temp_loaded.config)
    assert not is_location_only_title("Procurement Manager München", temp_loaded.config)


def test_agent_skips_outside_city_frontier_url_before_fetch(temp_loaded):
    temp_loaded.paths.seeds_path.write_text("https://www.stellenanzeigen.de/jobs/procurement/erlangen\n", encoding="utf-8")
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    fake_browser = FakeBrowser({})
    agent = JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: fake_browser,
        llm_client=StaticLLM(PageDecision(jobs=[], follow_urls=[], source_quality=0, source_notes="")),
        robots=AllowAllRobots(),
    )
    assert agent.run() == 0
    assert fake_browser.opened == []
    row = db.conn.execute("select status from frontier").fetchone()
    assert row["status"] == "skipped_location"
    db.close()


def test_agent_rejects_city_as_job_title(temp_loaded):
    temp_loaded.paths.seeds_path.write_text("https://example.test/jobs/supplier-quality-manager\n", encoding="utf-8")
    snapshot = PageSnapshot(
        url="https://example.test/jobs/supplier-quality-manager",
        final_url="https://example.test/jobs/supplier-quality-manager",
        title="Supplier Quality Manager",
        text="Supplier Quality Manager Munich Germany",
        links=[],
    )
    decision = PageDecision(
        jobs=[
            JobMatch(
                title="München",
                company="Example GmbH",
                location="München",
                url="https://example.test/jobs/supplier-quality-manager",
                fit_score=92,
                reason="LLM accidentally used the city as title",
                evidence="München",
            )
        ],
        follow_urls=[],
        source_quality=70,
        source_notes="test",
    )
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(temp_loaded, db=db, browser_factory=lambda: FakeBrowser({snapshot.url: snapshot}), llm_client=StaticLLM(decision), robots=AllowAllRobots())
    assert agent.run() == 0
    assert db.count_rows("jobs") == 0
    db.close()


def test_company_blacklist_drops_matching_jobs(temp_loaded):
    temp_loaded.config.companies.blacklist = ["BadCo"]
    temp_loaded.paths.seeds_path.write_text("https://example.test/jobs/procurement-manager\n", encoding="utf-8")
    snapshot = PageSnapshot(
        url="https://example.test/jobs/procurement-manager",
        final_url="https://example.test/jobs/procurement-manager",
        title="Procurement Manager",
        text="Procurement Manager Munich Germany",
        links=[],
    )
    decision = PageDecision(
        jobs=[
            JobMatch(
                title="Procurement Manager",
                company="BadCo GmbH",
                location="München",
                url="https://example.test/jobs/procurement-manager",
                fit_score=91,
                reason="Procurement role in Munich",
                evidence="Procurement Manager",
            )
        ],
        follow_urls=[],
        source_quality=70,
        source_notes="test",
    )
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(temp_loaded, db=db, browser_factory=lambda: FakeBrowser({snapshot.url: snapshot}), llm_client=StaticLLM(decision), robots=AllowAllRobots())
    assert agent.run() == 0
    assert db.count_rows("jobs") == 0
    db.close()


def test_linkedin_job_urls_are_not_globally_blocked(temp_loaded):
    assert clean_url("https://www.linkedin.com/jobs/search/?keywords=procurement&location=Munich", None, temp_loaded.config)
    assert clean_url("https://www.linkedin.com/jobs/view/1234567890", None, temp_loaded.config)


def test_csv_export_has_title_as_third_column(temp_loaded):
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    db.save_jobs(
        [
            JobMatch(
                title="Supplier Quality Manager",
                company="Example GmbH",
                location="München",
                url="https://example.test/jobs/supplier-quality-manager",
                fit_score=88,
                reason="Supplier quality role in Munich",
                evidence="Supplier Quality Manager",
                score_basis="target role signal",
            )
        ],
        "https://example.test/jobs/supplier-quality-manager",
        "example.test/jobs",
    )
    out = temp_loaded.paths.data_dir / "test.csv"
    db.export_csv(out)
    with out.open(encoding="utf-8", newline="") as f:
        header = next(csv.reader(f))
    assert header[2] == "title"
    db.close()


def test_current_search_page_url_is_not_saved_as_job_detail(temp_loaded):
    url = "https://www.stellenanzeigen.de/jobs/procurement/muenchen"
    temp_loaded.paths.seeds_path.write_text(url + "\n", encoding="utf-8")
    snapshot = PageSnapshot(
        url=url,
        final_url=url,
        title="Procurement Jobs München",
        text="Procurement Manager jobs in München",
        links=[],
    )
    decision = PageDecision(
        jobs=[
            JobMatch(
                title="Procurement Manager",
                company="Example GmbH",
                location="München",
                url=url,
                fit_score=85,
                reason="The LLM used the current search page as if it were a job detail page",
                evidence="Procurement Manager jobs in München",
            )
        ],
        follow_urls=[],
        source_quality=40,
        source_notes="listing page",
    )
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(temp_loaded, db=db, browser_factory=lambda: FakeBrowser({url: snapshot}), llm_client=StaticLLM(decision), robots=AllowAllRobots())
    assert agent.run() == 0
    assert db.count_rows("jobs") == 0
    db.close()
