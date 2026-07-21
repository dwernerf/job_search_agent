from __future__ import annotations

from collections.abc import Mapping

import pytest

from jobagent.agent import JobAgent
from jobagent.browser import BrowserFetchError
from jobagent.db import Database
from jobagent.llm import ContextWindowExceeded
from jobagent.models import LinkCandidate, LinkClassification, PageDecision, PageSnapshot


class FakeBrowser:
    def __init__(self, pages: Mapping[str, PageSnapshot | Exception]) -> None:
        self.pages = pages
        self.opened: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def fetch(self, url: str) -> PageSnapshot:
        self.opened.append(url)
        result = self.pages[url]
        if isinstance(result, Exception):
            raise result
        return result


class StaticLLM:
    def __init__(self, decision: PageDecision) -> None:
        self.decision = decision
        self.calls: list[tuple[PageSnapshot, list[dict[str, str]]]] = []

    def classify_links_batch(
        self,
        snapshot: PageSnapshot,
        links_with_context: list[dict[str, str]],
    ) -> PageDecision:
        self.calls.append((snapshot, links_with_context))
        return self.decision


def snapshot(url: str, *, text: str, links: list[LinkCandidate] | None = None) -> PageSnapshot:
    return PageSnapshot(
        url=url,
        final_url=url,
        title=url.rsplit("/", 1)[-1],
        text=text,
        links=links or [],
    )


def test_agent_saves_job_classified_from_fetched_link_context(temp_loaded):
    source_url = "https://alpha.test/careers"
    job_url = "https://alpha.test/jobs/procurement-manager"
    temp_loaded.paths.seeds_path.write_text(f"{source_url}\n", encoding="utf-8")
    pages = {
        source_url: snapshot(
            source_url,
            text="Alpha careers",
            links=[LinkCandidate(text="Procurement Manager", url=job_url)],
        ),
        job_url: snapshot(
            job_url,
            text="Procurement Manager in Munich. Strategic sourcing responsibilities.",
        ),
    }
    llm = StaticLLM(
        PageDecision(
            link_classifications=[
                LinkClassification(
                    index=0,
                    type="job_listing",
                    fit_score=92,
                    title="Procurement Manager",
                    company="Alpha",
                    location="Munich",
                    evidence="Strategic sourcing responsibilities",
                    reason="Strong procurement fit",
                )
            ],
        )
    )
    browser = FakeBrowser(pages)
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(temp_loaded, db=db, browser_factory=lambda: browser, llm_client=llm)

    assert agent.run() == 0

    row = db.conn.execute("select * from jobs").fetchone()
    assert row["url"] == job_url
    assert row["title"] == "Procurement Manager"
    assert row["source_key"] == "alpha.test/careers"
    assert browser.opened == [source_url, job_url]
    assert llm.calls[0][1][0]["url"] == job_url
    assert "Strategic sourcing responsibilities" in llm.calls[0][1][0]["page_context"]
    db.close()


def test_agent_canonicalizes_candidates_and_binds_returned_url(temp_loaded):
    source_url = "https://alpha.test/careers"
    job_url = "https://alpha.test/jobs/buyer"
    temp_loaded.paths.seeds_path.write_text(f"{source_url}\n", encoding="utf-8")
    pages = {
        source_url: snapshot(
            source_url,
            text="Careers",
            links=[
                LinkCandidate(text="Buyer", url=f"{job_url}?utm_source=test"),
                LinkCandidate(text="Buyer duplicate", url=job_url),
            ],
        ),
        job_url: snapshot(job_url, text="Buyer in Munich"),
    }
    llm = StaticLLM(
        PageDecision(
            link_classifications=[
                LinkClassification(
                    index=0,
                    type="job_listing",
                    fit_score=80,
                    title="Buyer",
                    company="Alpha",
                    location="Munich",
                    url="https://invented.test/job",
                )
            ],
        )
    )
    browser = FakeBrowser(pages)
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)

    assert JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: browser,
        llm_client=llm,
    ).run() == 0

    assert db.conn.execute("select url from jobs").fetchone()["url"] == job_url
    assert browser.opened == [source_url, job_url]
    db.close()


def test_agent_records_structured_top_level_browser_error(temp_loaded):
    source_url = "https://search.example.test/jobs"
    final_url = "https://search.example.test/blocked"
    temp_loaded.paths.seeds_path.write_text(f"{source_url}\n", encoding="utf-8")
    error = BrowserFetchError(
        kind="http",
        requested_url=source_url,
        final_url=final_url,
        status_code=429,
    )
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)

    assert JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: FakeBrowser({source_url: error}),
        llm_client=StaticLLM(PageDecision()),
    ).run() == 0

    row = db.conn.execute(
        "select final_url, status from pages where url = ?", (source_url,)
    ).fetchone()
    assert dict(row) == {"final_url": final_url, "status": "error:http_429"}
    db.close()


def test_candidate_browser_error_is_not_sent_to_llm_or_retried_with_source(temp_loaded):
    source_url = "https://alpha.test/careers"
    job_url = "https://alpha.test/jobs/buyer"
    temp_loaded.config.crawler.retry_error_pages = False
    temp_loaded.paths.seeds_path.write_text(f"{source_url}\n", encoding="utf-8")
    error = BrowserFetchError(
        kind="http",
        requested_url=job_url,
        final_url=job_url,
        status_code=503,
    )
    llm = StaticLLM(
        PageDecision(
            link_classifications=[
                LinkClassification(
                    index=0,
                    type="job_listing",
                    fit_score=99,
                    title="Invented Buyer",
                    company="Alpha",
                    location="Munich",
                )
            ],
        )
    )
    browser = FakeBrowser(
        {
            source_url: snapshot(
                source_url,
                text="Careers",
                links=[LinkCandidate(text="Buyer", url=job_url)],
            ),
            job_url: error,
        }
    )
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)

    assert JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: browser,
        llm_client=llm,
    ).run() == 0

    assert llm.calls == []
    assert db.page_status(source_url) == "ok"
    assert db.page_status(job_url) == "error:http_503"
    assert db.count_rows("jobs") == 0
    assert db.conn.execute(
        "select status from backlog where url = ?", (source_url,)
    ).fetchone() is None
    assert browser.opened == [source_url, job_url]
    db.close()

    reopened = Database(temp_loaded.paths.database_path, temp_loaded.config)
    assert reopened.pop_backlog() is None
    reopened.close()


def test_agent_retries_transient_candidate_http_error_when_rediscovered(temp_loaded):
    first_source = "https://alpha.test/careers"
    second_source = "https://beta.test/careers"
    job_url = "https://jobs.test/buyer"
    temp_loaded.config.crawler.retry_error_pages = True
    temp_loaded.paths.seeds_path.write_text(
        f"{first_source}\n{second_source}\n",
        encoding="utf-8",
    )
    pages = {
        first_source: snapshot(
            first_source,
            text="Alpha careers",
            links=[LinkCandidate(text="Buyer", url=job_url)],
        ),
        second_source: snapshot(
            second_source,
            text="Beta careers",
            links=[LinkCandidate(text="Buyer", url=job_url)],
        ),
    }
    candidate_results = [
        BrowserFetchError(
            kind="http",
            requested_url=job_url,
            final_url=job_url,
            status_code=503,
        ),
        snapshot(job_url, text="Buyer in Munich"),
    ]

    class RetryBrowser(FakeBrowser):
        def fetch(self, url: str) -> PageSnapshot:
            self.opened.append(url)
            result = candidate_results.pop(0) if url == job_url else self.pages[url]
            if isinstance(result, Exception):
                raise result
            return result

    llm = StaticLLM(
        PageDecision(
            link_classifications=[
                LinkClassification(
                    index=0,
                    type="job_listing",
                    fit_score=85,
                    title="Buyer",
                    company="Example",
                    location="Munich",
                )
            ]
        )
    )
    browser = RetryBrowser(pages)
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)

    assert JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: browser,
        llm_client=llm,
    ).run() == 0

    assert browser.opened.count(job_url) == 2
    assert len(llm.calls) == 1
    assert db.page_status(job_url) == "ok"
    assert db.count_rows("jobs") == 1
    db.close()


def test_agent_sends_only_successfully_fetched_candidates_to_llm(temp_loaded):
    source_url = "https://alpha.test/careers"
    failed_url = "https://alpha.test/jobs/unavailable"
    job_url = "https://alpha.test/jobs/buyer"
    temp_loaded.paths.seeds_path.write_text(f"{source_url}\n", encoding="utf-8")
    llm = StaticLLM(
        PageDecision(
            link_classifications=[
                LinkClassification(
                    index=0,
                    type="job_listing",
                    fit_score=85,
                    title="Buyer",
                    company="Alpha",
                    location="Munich",
                )
            ],
        )
    )
    browser = FakeBrowser(
        {
            source_url: snapshot(
                source_url,
                text="Careers",
                links=[
                    LinkCandidate(text="Unavailable", url=failed_url),
                    LinkCandidate(text="Buyer", url=job_url),
                ],
            ),
            failed_url: RuntimeError("navigation failed"),
            job_url: snapshot(job_url, text="Buyer in Munich"),
        }
    )
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)

    assert JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: browser,
        llm_client=llm,
    ).run() == 0

    assert len(llm.calls) == 1
    assert len(llm.calls[0][1]) == 1
    llm_link = llm.calls[0][1][0]
    assert llm_link["index"] == "0"
    assert llm_link["text"] == "Buyer"
    assert llm_link["url"] == job_url
    assert "Buyer in Munich" in llm_link["page_context"]
    assert db.conn.execute("select url from jobs").fetchone()["url"] == job_url
    assert db.page_status(source_url) == "ok"
    assert db.page_status(failed_url) == "ok"
    assert browser.opened == [source_url, failed_url, job_url]
    db.close()


def test_agent_fetches_candidate_only_once_across_sources(temp_loaded):
    first_source = "https://alpha.test/careers"
    second_source = "https://beta.test/careers"
    job_url = "https://jobs.test/buyer"
    temp_loaded.paths.seeds_path.write_text(
        f"{first_source}\n{second_source}\n",
        encoding="utf-8",
    )
    pages = {
        first_source: snapshot(
            first_source,
            text="Alpha careers",
            links=[LinkCandidate(text="Buyer", url=job_url)],
        ),
        second_source: snapshot(
            second_source,
            text="Beta careers",
            links=[LinkCandidate(text="Buyer", url=job_url)],
        ),
        job_url: snapshot(job_url, text="Buyer in Munich"),
    }
    llm = StaticLLM(
        PageDecision(
            link_classifications=[
                LinkClassification(index=0, type="skip", reason="Not a target role")
            ]
        )
    )
    browser = FakeBrowser(pages)
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)

    assert JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: browser,
        llm_client=llm,
    ).run() == 0

    assert browser.opened.count(job_url) == 1
    assert len(llm.calls) == 1
    assert db.page_status(job_url) == "ok"
    assert db.count_rows("backlog") == 0
    db.close()


def test_agent_drops_out_of_range_classification_index(temp_loaded):
    source_url = "https://alpha.test/careers"
    job_url = "https://alpha.test/jobs/buyer"
    temp_loaded.paths.seeds_path.write_text(f"{source_url}\n", encoding="utf-8")
    pages = {
        source_url: snapshot(
            source_url,
            text="Careers",
            links=[LinkCandidate(text="Buyer", url=job_url)],
        ),
        job_url: snapshot(job_url, text="Buyer in Munich"),
    }
    llm = StaticLLM(
        PageDecision(
            link_classifications=[
                LinkClassification(
                    index=9,
                    type="job_listing",
                    fit_score=80,
                    title="Buyer",
                    company="Alpha",
                    location="Munich",
                )
            ],
        )
    )
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)

    assert JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: FakeBrowser(pages),
        llm_client=llm,
    ).run() == 0
    assert db.count_rows("jobs") == 0
    db.close()


def test_agent_applies_export_score_threshold(temp_loaded):
    source_url = "https://alpha.test/careers"
    low_url = "https://alpha.test/jobs/low-fit"
    accepted_url = "https://alpha.test/jobs/accepted-fit"
    temp_loaded.config.scoring.min_score_to_export = 80
    temp_loaded.paths.seeds_path.write_text(f"{source_url}\n", encoding="utf-8")
    pages = {
        source_url: snapshot(
            source_url,
            text="Alpha careers",
            links=[
                LinkCandidate(text="Low fit", url=low_url),
                LinkCandidate(text="Accepted fit", url=accepted_url),
            ],
        ),
        low_url: snapshot(low_url, text="A weakly related role"),
        accepted_url: snapshot(accepted_url, text="A matching procurement role"),
    }
    llm = StaticLLM(
        PageDecision(
            link_classifications=[
                LinkClassification(
                    index=0,
                    type="job_listing",
                    fit_score=79,
                    title="Low Fit Role",
                    company="Alpha",
                    location="Munich",
                ),
                LinkClassification(
                    index=1,
                    type="job_listing",
                    fit_score=80,
                    title="Accepted Role",
                    company="Alpha",
                    location="Munich",
                ),
            ],
        )
    )
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(temp_loaded, db=db, browser_factory=lambda: FakeBrowser(pages), llm_client=llm)

    assert agent.run() == 0
    rows = db.conn.execute("select url, fit_score from jobs").fetchall()
    assert [(row["url"], row["fit_score"]) for row in rows] == [(accepted_url, 80)]
    db.close()


def test_agent_drops_jobs_from_blacklisted_companies(temp_loaded):
    source_url = "https://example.test/careers"
    job_url = "https://example.test/jobs/buyer"
    temp_loaded.config.companies.blacklist = ["BadCo"]
    temp_loaded.paths.seeds_path.write_text(f"{source_url}\n", encoding="utf-8")
    pages = {
        source_url: snapshot(
            source_url,
            text="Careers",
            links=[LinkCandidate(text="Buyer", url=job_url)],
        ),
        job_url: snapshot(job_url, text="Buyer role in Munich"),
    }
    llm = StaticLLM(
        PageDecision(
            link_classifications=[
                LinkClassification(
                    index=0,
                    type="job_listing",
                    fit_score=91,
                    title="Buyer",
                    company="BadCo GmbH",
                    location="Munich",
                )
            ],
        )
    )
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(temp_loaded, db=db, browser_factory=lambda: FakeBrowser(pages), llm_client=llm)

    assert agent.run() == 0
    assert db.count_rows("jobs") == 0
    db.close()


@pytest.mark.parametrize("exploration_enabled", [False, True])
def test_agent_exploration_flag_controls_explore_enqueue(temp_loaded, exploration_enabled):
    source_url = "https://alpha.test/start"
    explore_url = "https://alpha.test/jobs"
    temp_loaded.config.exploration.enabled = exploration_enabled
    temp_loaded.paths.seeds_path.write_text(f"{source_url}\n", encoding="utf-8")
    pages = {
        source_url: snapshot(
            source_url,
            text="Alpha",
            links=[LinkCandidate(text="Open jobs", url=explore_url)],
        ),
        explore_url: snapshot(explore_url, text="Open positions"),
    }
    llm = StaticLLM(
        PageDecision(
            link_classifications=[
                LinkClassification(index=0, type="explore", reason="Relevant job index")
            ],
        )
    )
    browser = FakeBrowser(pages)
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(temp_loaded, db=db, browser_factory=lambda: browser, llm_client=llm)

    assert agent.run() == 0
    row = db.conn.execute("select status from backlog where url = ?", (explore_url,)).fetchone()
    assert row is None
    assert db.page_status(explore_url) == "ok"
    assert browser.opened.count(explore_url) == (2 if exploration_enabled else 1)
    db.close()


class UnavailableLLM:
    def health_check(self):
        return False, "ConnectionError: connection refused"

    def classify_links_batch(self, snapshot, links_with_context):
        raise AssertionError("classification must not run")


def test_agent_stops_before_browsing_when_llm_unavailable(temp_loaded):
    temp_loaded.paths.seeds_path.write_text("https://alpha.test/careers\n", encoding="utf-8")
    browser = FakeBrowser({})
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(temp_loaded, db=db, browser_factory=lambda: browser, llm_client=UnavailableLLM())

    assert agent.run() == 2
    assert browser.opened == []
    assert db.queued_count() == 0
    db.close()


class FailingLLM:
    def classify_links_batch(self, snapshot, links_with_context):
        raise RuntimeError("simulated local LLM failure")


def test_agent_records_generic_midrun_llm_failure_without_saving(temp_loaded):
    source_url = "https://alpha.test/careers"
    job_url = "https://alpha.test/jobs/procurement-manager"
    temp_loaded.paths.seeds_path.write_text(f"{source_url}\n", encoding="utf-8")
    pages = {
        source_url: snapshot(
            source_url,
            text="Procurement jobs",
            links=[LinkCandidate(text="Procurement Manager", url=job_url)],
        ),
        job_url: snapshot(job_url, text="Procurement Manager in Munich"),
    }
    browser = FakeBrowser(pages)
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(temp_loaded, db=db, browser_factory=lambda: browser, llm_client=FailingLLM())

    assert agent.run() == 0
    assert db.count_rows("jobs") == 0
    assert db.page_status(source_url) == "error:RuntimeError"
    assert db.page_status(job_url) == "ok"
    assert db.queued_count() == 0
    assert db.conn.execute(
        "select status from backlog where url = ?", (source_url,)
    ).fetchone()["status"] == "error"
    assert browser.opened == [source_url, job_url]
    db.close()


class ContextFailingLLM:
    def classify_links_batch(self, snapshot, links_with_context):
        raise ContextWindowExceeded("too large")


def test_single_link_context_overflow_marks_page_as_error(temp_loaded):
    source_url = "https://alpha.test/careers"
    job_url = "https://alpha.test/jobs/buyer"
    temp_loaded.paths.seeds_path.write_text(f"{source_url}\n", encoding="utf-8")
    pages = {
        source_url: snapshot(
            source_url,
            text="Careers",
            links=[LinkCandidate(text="Buyer", url=job_url)],
        ),
        job_url: snapshot(job_url, text="Buyer in Munich"),
    }
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)

    assert JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: FakeBrowser(pages),
        llm_client=ContextFailingLLM(),
    ).run() == 0

    assert db.page_status(source_url) == "error:ContextWindowExceeded"
    assert db.count_rows("jobs") == 0
    db.close()


class RecoveringContextLLM:
    def __init__(self) -> None:
        self.calls = 0

    def classify_links_batch(self, snapshot, links_with_context):
        self.calls += 1
        if self.calls == 1:
            raise ContextWindowExceeded("split this batch")
        return PageDecision(
            link_classifications=[
                LinkClassification(
                    index=0,
                    type="job_listing",
                    fit_score=80,
                    title=links_with_context[0]["text"],
                    company="Alpha",
                    location="Munich",
                )
            ],
        )


def test_context_overflow_defers_dropped_link_instead_of_losing_it(temp_loaded):
    source_url = "https://alpha.test/careers"
    first_url = "https://alpha.test/jobs/buyer"
    second_url = "https://alpha.test/jobs/manager"
    temp_loaded.paths.seeds_path.write_text(f"{source_url}\n", encoding="utf-8")
    pages = {
        source_url: snapshot(
            source_url,
            text="Careers",
            links=[
                LinkCandidate(text="Buyer", url=first_url),
                LinkCandidate(text="Manager", url=second_url),
            ],
        ),
        first_url: snapshot(first_url, text="Buyer in Munich"),
        second_url: snapshot(second_url, text="Manager in Munich"),
    }
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)

    assert JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: FakeBrowser(pages),
        llm_client=RecoveringContextLLM(),
    ).run() == 0

    assert db.count_rows("jobs") == 2
    assert {row["url"] for row in db.conn.execute("select url from jobs")} == {
        first_url,
        second_url,
    }
    db.close()


class SecondBatchFailingLLM:
    def __init__(self) -> None:
        self.calls = 0

    def classify_links_batch(self, snapshot, links_with_context):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("second batch failed")
        return PageDecision(
            link_classifications=[
                LinkClassification(
                    index=0,
                    type="job_listing",
                    fit_score=80,
                    title="Buyer",
                    company="Alpha",
                    location="Munich",
                )
            ],
        )


def test_later_batch_failure_keeps_committed_job_in_run_stats(temp_loaded):
    source_url = "https://alpha.test/careers"
    first_url = "https://alpha.test/jobs/buyer"
    second_url = "https://alpha.test/jobs/manager"
    temp_loaded.config.crawler.batch_size_for_llm = 1
    temp_loaded.paths.seeds_path.write_text(f"{source_url}\n", encoding="utf-8")
    pages = {
        source_url: snapshot(
            source_url,
            text="Careers",
            links=[
                LinkCandidate(text="Buyer", url=first_url),
                LinkCandidate(text="Manager", url=second_url),
            ],
        ),
        first_url: snapshot(first_url, text="Buyer in Munich"),
        second_url: snapshot(second_url, text="Manager in Munich"),
    }
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: FakeBrowser(pages),
        llm_client=SecondBatchFailingLLM(),
    )

    assert agent.run() == 0
    assert db.count_rows("jobs") == 1
    assert agent.reporter.stats.jobs_saved == 1
    assert db.page_status(source_url) == "error:RuntimeError"
    db.close()
