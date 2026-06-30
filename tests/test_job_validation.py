from __future__ import annotations

from jobagent.agent import JobAgent
from jobagent.db import Database
from jobagent.models import JobMatch, LinkCandidate, PageDecision, PageSnapshot


class AllowAllRobots:
    def allowed(self, url: str) -> bool:
        return True


class FakeBrowser:
    def __init__(self, pages: dict[str, PageSnapshot]) -> None:
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def fetch(self, url: str) -> PageSnapshot:
        return self.pages[url]


class FakeLLM:
    def generate_queries(self, memory_summary: str, run_summary: str):
        return []


class HallucinatedUrlLLM(FakeLLM):
    def analyze_page(self, snapshot: PageSnapshot, candidate_links, memory_summary: str) -> PageDecision:
        return PageDecision(
            jobs=[
                JobMatch(
                    title="Procurement Manager Optical Components",
                    company="Example GmbH",
                    location="München",
                    url="https://example.test/jobs/not-present-on-page",
                    fit_score=90,
                    reason="Procurement role in München",
                    evidence="Procurement Manager Optical Components",
                )
            ],
            follow_urls=[],
            source_quality=60,
            source_notes="test page",
        )


class InitiativeLLM(FakeLLM):
    def analyze_page(self, snapshot: PageSnapshot, candidate_links, memory_summary: str) -> PageDecision:
        return PageDecision(
            jobs=[
                JobMatch(
                    title="Initiativbewerbung Einkauf",
                    company="Example GmbH",
                    location="München",
                    url="https://example.test/jobs/initiativbewerbung-einkauf",
                    fit_score=80,
                    reason="General application, not a concrete job posting",
                    evidence="Initiativbewerbung",
                )
            ],
            follow_urls=[],
            source_quality=20,
            source_notes="initiative application only",
        )


def test_llm_job_url_must_come_from_current_page(temp_loaded):
    temp_loaded.paths.seeds_path.write_text("https://example.test/jobs\n", encoding="utf-8")
    snapshot = PageSnapshot(
        url="https://example.test/jobs",
        final_url="https://example.test/jobs",
        title="Jobs",
        text="Procurement Manager Optical Components München",
        links=[LinkCandidate(text="Different job", url="https://example.test/jobs/different-job")],
    )
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: FakeBrowser({"https://example.test/jobs": snapshot}),
        llm_client=HallucinatedUrlLLM(),
        robots=AllowAllRobots(),
    )
    assert agent.run() == 0
    assert db.count_rows("jobs") == 0
    db.close()


def test_initiativbewerbung_is_not_saved_as_job(temp_loaded):
    temp_loaded.paths.seeds_path.write_text("https://example.test/jobs\n", encoding="utf-8")
    snapshot = PageSnapshot(
        url="https://example.test/jobs",
        final_url="https://example.test/jobs",
        title="Jobs",
        text="Initiativbewerbung Einkauf München",
        links=[LinkCandidate(text="Initiativbewerbung Einkauf", url="https://example.test/jobs/initiativbewerbung-einkauf")],
    )
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: FakeBrowser({"https://example.test/jobs": snapshot}),
        llm_client=InitiativeLLM(),
        robots=AllowAllRobots(),
    )
    assert agent.run() == 0
    assert db.count_rows("jobs") == 0
    db.close()


class OverviewLinkAsJobLLM(FakeLLM):
    def analyze_page(self, snapshot: PageSnapshot, candidate_links, memory_summary: str) -> PageDecision:
        return PageDecision(
            jobs=[
                JobMatch(
                    title="Procurement Manager Optical Components",
                    company="Example GmbH",
                    location="München",
                    url="https://example.test/jobs/procurement-manager-optics-123456",
                    fit_score=91,
                    reason="This is only a link on an overview page, not the loaded detail page",
                    evidence="Procurement Manager Optical Components",
                )
            ],
            follow_urls=["https://example.test/jobs/procurement-manager-optics-123456"],
            source_quality=70,
            source_notes="overview page",
        )


class DetailPageLLM(FakeLLM):
    def analyze_page(self, snapshot: PageSnapshot, candidate_links, memory_summary: str) -> PageDecision:
        return PageDecision(
            jobs=[
                JobMatch(
                    title="Procurement Manager Optical Components",
                    company="Example GmbH",
                    location="München",
                    url=snapshot.final_url,
                    fit_score=91,
                    reason="Loaded page is a concrete procurement job in München",
                    evidence="Procurement Manager Optical Components",
                )
            ],
            follow_urls=[],
            source_quality=90,
            source_notes="job detail page",
        )


def test_overview_page_candidate_link_is_not_saved_as_job(temp_loaded):
    temp_loaded.paths.seeds_path.write_text("https://example.test/jobs/procurement/muenchen\n", encoding="utf-8")
    snapshot = PageSnapshot(
        url="https://example.test/jobs/procurement/muenchen",
        final_url="https://example.test/jobs/procurement/muenchen",
        title="Procurement Jobs München",
        text="Overview page with Procurement Manager Optical Components card",
        links=[LinkCandidate(text="Procurement Manager Optical Components", url="https://example.test/jobs/procurement-manager-optics-123456")],
    )
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: FakeBrowser({"https://example.test/jobs/procurement/muenchen": snapshot}),
        llm_client=OverviewLinkAsJobLLM(),
        robots=AllowAllRobots(),
    )
    assert agent.run() == 0
    assert db.count_rows("jobs") == 0
    db.close()


def test_loaded_detail_page_current_url_can_be_saved(temp_loaded):
    url = "https://example.test/jobs/procurement-manager-optics-123456"
    temp_loaded.paths.seeds_path.write_text(url + "\n", encoding="utf-8")
    snapshot = PageSnapshot(
        url=url,
        final_url=url,
        title="Procurement Manager Optical Components",
        text="Procurement Manager Optical Components. München. Responsibilities and requirements for sourcing optical components.",
        links=[],
    )
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: FakeBrowser({url: snapshot}),
        llm_client=DetailPageLLM(),
        robots=AllowAllRobots(),
    )
    assert agent.run() == 0
    row = db.conn.execute("select title, url from jobs").fetchone()
    assert row["title"] == "Procurement Manager Optical Components"
    assert row["url"] == url
    db.close()
