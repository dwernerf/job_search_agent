from __future__ import annotations

from jobagent.agent import JobAgent
from jobagent.db import Database
from jobagent.models import JobMatch, LinkCandidate, LinkClassification, PageDecision, PageSnapshot


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
    pass


class HallucinatedUrlLLM(FakeLLM):
    def analyze_page(self, snapshot: PageSnapshot, links_with_context, memory_summary: str) -> PageDecision:
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
            link_classifications=[],
            source_quality=60,
            source_notes="test page",
        )

    def classify_links_batch(self, snapshot: PageSnapshot, links_with_context, memory_summary: str) -> PageDecision:
        return self.analyze_page(snapshot, links_with_context, memory_summary)


class InitiativeLLM(FakeLLM):
    def analyze_page(self, snapshot: PageSnapshot, links_with_context, memory_summary: str) -> PageDecision:
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
            link_classifications=[],
            source_quality=20,
            source_notes="initiative application only",
        )

    def classify_links_batch(self, snapshot: PageSnapshot, links_with_context, memory_summary: str) -> PageDecision:
        return self.analyze_page(snapshot, links_with_context, memory_summary)


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
    )
    assert agent.run() == 0
    assert db.count_rows("jobs") == 0
    db.close()


class OverviewLinkAsJobLLM(FakeLLM):
    def analyze_page(self, snapshot: PageSnapshot, links_with_context, memory_summary: str) -> PageDecision:
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
            link_classifications=[],
            source_quality=70,
            source_notes="overview page",
        )

    def classify_links_batch(self, snapshot: PageSnapshot, links_with_context, memory_summary: str) -> PageDecision:
        return self.analyze_page(snapshot, links_with_context, memory_summary)


class DetailPageLLM(FakeLLM):
    def analyze_page(self, snapshot: PageSnapshot, links_with_context, memory_summary: str) -> PageDecision:
        return PageDecision(
            jobs=[],
            link_classifications=[],
            source_quality=90,
            source_notes="job detail page",
        )

    def classify_links_batch(self, snapshot: PageSnapshot, links_with_context, memory_summary: str) -> PageDecision:
        # Simulate that the current page is a detail page by returning the job
        jobs = [
            JobMatch(
                title="Procurement Manager Optical Components",
                company="Example GmbH",
                location="München",
                url=snapshot.final_url,
                fit_score=91,
                reason="Loaded page is a concrete procurement job in München",
                evidence="Procurement Manager Optical Components",
            )
        ]
        # Simulate that the first link is a job_listing with a high fit score
        classifications = [
            LinkClassification(index=0, type="job_listing", fit_score=91, title="Procurement Manager Optical Components", company="Example GmbH", location="München", evidence="Procurement Manager Optical Components", reason="Loaded page is a concrete procurement job in München"),
        ]
        decision = PageDecision(
            jobs=jobs,
            link_classifications=classifications,
            source_quality=90,
            source_notes="job detail page",
        )
        # Inject URLs from links_with_context (the LLM prompt omits them)
        ctx_by_index = {int(item["index"]): item["url"] for item in links_with_context}
        for c in decision.link_classifications:
            if not c.url and c.index in ctx_by_index:
                c.url = ctx_by_index[c.index]
        return decision


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
        links=[LinkCandidate(text="Procurement Manager", url=url)],
    )
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: FakeBrowser({url: snapshot}),
        llm_client=DetailPageLLM(),
    )
    assert agent.run() == 0
    row = db.conn.execute("select title, url from jobs").fetchone()
    assert row["title"] == "Procurement Manager Optical Components"
    assert row["url"] == url
    db.close()
