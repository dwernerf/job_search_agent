from __future__ import annotations

from jobagent.agent import JobAgent
from jobagent.company_filters import match_whitelist_company, whitelist_scope_allows
from jobagent.db import Database
from jobagent.discover import enqueue_links, make_frontier_item, seed_frontier
from jobagent.models import JobMatch, LinkCandidate, PageDecision, PageSnapshot


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


def test_run_start_resets_stale_frontier_items(temp_loaded):
    temp_loaded.config.run.reset_frontier_on_start = True
    temp_loaded.paths.seeds_path.write_text("https://fresh.test/careers\n", encoding="utf-8")
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    stale = make_frontier_item(
        "https://www.stellenanzeigen.de/job/nachhilfelehrer-muenchen-reg12345",
        3,
        "previous-run",
        "stale irrelevant recommendation",
        temp_loaded.config,
        db,
    )
    assert db.enqueue(stale)
    assert db.queued_count() == 1

    fresh_snapshot = PageSnapshot(
        url="https://fresh.test/careers",
        final_url="https://fresh.test/careers",
        title="Fresh Careers",
        text="No jobs yet",
        links=[],
    )
    fake_browser = FakeBrowser({"https://fresh.test/careers": fresh_snapshot})
    agent = JobAgent(
        temp_loaded,
        db=db,
        browser_factory=lambda: fake_browser,
        llm_client=StaticLLM(PageDecision(jobs=[], follow_urls=[], source_quality=10, source_notes="empty")),
        robots=AllowAllRobots(),
    )
    agent.run()
    assert "https://www.stellenanzeigen.de/job/nachhilfelehrer-muenchen-reg12345" not in fake_browser.opened
    stale_row = db.conn.execute("select * from frontier where url like '%nachhilfelehrer%'").fetchone()
    assert stale_row is None
    db.close()


def test_whitelist_only_drops_non_whitelist_saved_job(temp_loaded):
    temp_loaded.config.exploration.mode = "whitelist_only"
    temp_loaded.config.companies.whitelist = ["ZEISS"]
    temp_loaded.paths.seeds_path.write_text("https://jobs.test/job/procurement-manager-12345\n", encoding="utf-8")
    snapshot = PageSnapshot(
        url="https://jobs.test/job/procurement-manager-12345",
        final_url="https://jobs.test/job/procurement-manager-12345",
        title="Procurement Manager",
        text="Procurement Manager Munich at NotWhite GmbH",
        links=[],
    )
    decision = PageDecision(
        jobs=[
            JobMatch(
                title="Procurement Manager",
                company="NotWhite GmbH",
                location="München",
                url=snapshot.final_url,
                fit_score=90,
                reason="Procurement role in Munich",
                evidence="Procurement Manager",
            )
        ],
        follow_urls=[],
        source_quality=90,
        source_notes="detail",
    )
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(temp_loaded, db=db, browser_factory=lambda: FakeBrowser({snapshot.url: snapshot}), llm_client=StaticLLM(decision), robots=AllowAllRobots())
    agent.run()
    assert db.count_rows("jobs") == 0
    db.close()


def test_whitelist_only_keeps_whitelist_saved_job(temp_loaded):
    temp_loaded.config.exploration.mode = "whitelist_only"
    temp_loaded.config.companies.whitelist = ["ZEISS"]
    temp_loaded.paths.seeds_path.write_text("https://jobs.test/job/procurement-manager-zeiss-12345\n", encoding="utf-8")
    snapshot = PageSnapshot(
        url="https://jobs.test/job/procurement-manager-zeiss-12345",
        final_url="https://jobs.test/job/procurement-manager-zeiss-12345",
        title="Procurement Manager",
        text="Procurement Manager Munich at ZEISS",
        links=[],
    )
    decision = PageDecision(
        jobs=[
            JobMatch(
                title="Procurement Manager",
                company="ZEISS",
                location="München",
                url=snapshot.final_url,
                fit_score=90,
                reason="Procurement role in Munich",
                evidence="Procurement Manager at ZEISS",
            )
        ],
        follow_urls=[],
        source_quality=90,
        source_notes="detail",
    )
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    agent = JobAgent(temp_loaded, db=db, browser_factory=lambda: FakeBrowser({snapshot.url: snapshot}), llm_client=StaticLLM(decision), robots=AllowAllRobots())
    agent.run()
    assert db.count_rows("jobs") == 1
    db.close()


def test_whitelist_scope_filters_candidate_links_in_whitelist_only(temp_loaded):
    cfg = temp_loaded.config
    cfg.exploration.mode = "whitelist_only"
    cfg.companies.whitelist = ["ZEISS"]
    db = Database(temp_loaded.paths.database_path, cfg)
    links = [
        LinkCandidate(text="Procurement Manager ZEISS", url="https://www.linkedin.com/jobs/view/zeiss-procurement-123", score=3, reason="ZEISS result"),
        LinkCandidate(text="Nachhilfelehrer Unknown", url="https://www.linkedin.com/jobs/view/nachhilfelehrer-456", score=3, reason="unrelated recommendation"),
    ]
    added = enqueue_links(links, "https://www.linkedin.com/jobs/search/?keywords=ZEISS+procurement", 1, cfg, db)
    rows = db.conn.execute("select url from frontier order by url").fetchall()
    assert added == 1
    assert len(rows) == 1
    assert "zeiss" in rows[0]["url"].lower()
    db.close()


def test_company_name_matching_supports_domains_and_short_names(temp_loaded):
    cfg = temp_loaded.config
    cfg.companies.whitelist = ["SUSS MicroTec", "Airbus Defence & Space", "BMW"]
    assert match_whitelist_company(cfg, "https://www.sussmicrotec.com/de/karriere")
    assert match_whitelist_company(cfg, "Airbus sucht Procurement Manager")
    assert match_whitelist_company(cfg, "https://www.bmwgroup.jobs/de/job/123")


def test_whitelist_search_templates_include_linkedin(loaded_sample):
    templates = loaded_sample.config.exploration.search_url_templates
    assert any("linkedin.com/jobs/search" in t for t in templates)


def test_whitelist_only_export_filters_old_non_whitelist_rows(temp_loaded):
    import csv

    cfg = temp_loaded.config
    cfg.exploration.mode = "whitelist_only"
    cfg.companies.whitelist = ["ZEISS"]
    db = Database(temp_loaded.paths.database_path, cfg)
    db.save_jobs(
        [
            JobMatch(
                title="Procurement Manager",
                company="ZEISS",
                location="München",
                url="https://jobs.test/job/zeiss-procurement-1",
                fit_score=90,
                reason="Procurement at ZEISS",
                evidence="ZEISS Procurement Manager",
            ),
            JobMatch(
                title="Procurement Manager",
                company="Other GmbH",
                location="München",
                url="https://jobs.test/job/other-procurement-1",
                fit_score=90,
                reason="Procurement at another company",
                evidence="Other GmbH Procurement Manager",
            ),
        ],
        "https://jobs.test/source",
        "jobs.test/source",
    )
    out = temp_loaded.paths.data_dir / "whitelist.csv"
    db.export_csv(out)
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["company"] == "ZEISS"
    assert db.count_rows("jobs") == 2
    db.close()


def test_whitelist_company_detail_urls_still_need_role_signal(temp_loaded):
    from jobagent.discover import exploration_url_allowed

    cfg = temp_loaded.config
    cfg.exploration.mode = "whitelist_only"
    cfg.companies.whitelist = ["Rohde & Schwarz"]

    assert not exploration_url_allowed(
        "https://www.rohde-schwarz.com/de/karriere/stellenangebote/werkstudent-software-entwicklung-in-c-m-w-d_251563-1630701.html",
        "Werkstudent Software Entwicklung in C (m/w/d) | Rohde & Schwarz",
        cfg,
    )
    assert exploration_url_allowed(
        "https://www.rohde-schwarz.com/de/karriere/stellenangebote/supplier-quality-manager-m-w-d_251563-1630701.html",
        "Supplier Quality Manager (m/w/d) | Rohde & Schwarz",
        cfg,
    )


def test_whitelist_only_rejects_unfocused_company_sort_and_foreign_locale_urls(temp_loaded):
    from jobagent.discover import exploration_url_allowed

    cfg = temp_loaded.config
    cfg.exploration.mode = "whitelist_only"
    cfg.companies.whitelist = ["Rohde & Schwarz"]

    assert not exploration_url_allowed(
        "https://www.rohde-schwarz.com/de/karriere/stellenangebote/karriere-stellenangebote_251573.html?term=%2A&sort=rsJobsFieldOfWork&sortDir=desc",
        "Stellenangebote | Rohde & Schwarz Karriere",
        cfg,
    )
    assert not exploration_url_allowed(
        "https://www.rohde-schwarz.com/ch/karriere/stellenangebote/karriere-stellenangebote_251573.html?change_c=CH",
        "Stellenangebote | Rohde & Schwarz Karriere",
        cfg,
    )


def test_linkedin_signup_and_legal_pages_are_blocked_by_safety(temp_loaded):
    from jobagent.urltools import denied_by_safety

    cfg = temp_loaded.config
    assert denied_by_safety("https://www.linkedin.com/signup/cold-join?session_redirect=x", "Sign Up", cfg)
    assert denied_by_safety("https://www.linkedin.com/legal/privacy-policy", "Privacy Policy", cfg)


def test_whitelist_seeding_uses_direct_career_domains_without_search_engine(temp_loaded):
    from jobagent.discover import seed_frontier
    from jobagent.db import Database

    cfg = temp_loaded.config
    cfg.exploration.mode = "whitelist_only"
    cfg.companies.whitelist_search_when_seeding = True
    cfg.companies.whitelist = ["HENSOLDT"]
    cfg.companies.known_domains = {"HENSOLDT": ["hensoldt.net"]}
    cfg.companies.portal_role_terms = ["Procurement Manager"]
    cfg.companies.max_portal_role_terms_per_company = 1
    cfg.companies.max_search_queries_per_company = 0
    temp_loaded.paths.seeds_path.write_text("", encoding="utf-8")

    db = Database(temp_loaded.paths.database_path, cfg)
    added = seed_frontier(cfg, db, temp_loaded.paths.seeds_path)
    rows = db.conn.execute("select url, reason, discovered_from from frontier order by discovered_from, url").fetchall()
    assert added > 0
    assert any(row["discovered_from"] == "company-direct-career" and "hensoldt.net" in row["url"] for row in rows)
    assert any(row["discovered_from"] == "company-whitelist-portal-query" and "Procurement" in row["reason"] for row in rows)
    assert not any("duckduckgo.com" in row["url"] for row in rows)
    db.close()


def test_company_direct_entrypoints_do_not_bruteforce_career_paths_by_default(temp_loaded):
    from jobagent.discover import company_direct_career_urls

    cfg = temp_loaded.config
    cfg.companies.whitelist_search_when_seeding = True
    cfg.companies.whitelist = ["ZEISS"]
    cfg.companies.known_domains = {"ZEISS": ["zeiss.com"]}
    cfg.companies.infer_domains_from_company_names = False
    cfg.companies.direct_career_discovery = "root_only"

    urls = [url for url, _company in company_direct_career_urls(cfg)]
    assert [u.rstrip("/") for u in urls] == ["https://zeiss.com"]


def test_company_direct_entrypoints_can_probe_paths_when_explicitly_enabled(temp_loaded):
    from jobagent.discover import company_direct_career_urls

    cfg = temp_loaded.config
    cfg.companies.whitelist_search_when_seeding = True
    cfg.companies.whitelist = ["ZEISS"]
    cfg.companies.known_domains = {"ZEISS": ["zeiss.com"]}
    cfg.companies.infer_domains_from_company_names = False
    cfg.companies.direct_career_discovery = "root_plus_configured_paths"
    cfg.companies.max_direct_career_urls_per_company = 3

    urls = [url for url, _company in company_direct_career_urls(cfg)]
    assert "https://zeiss.com" in [u.rstrip("/") for u in urls]
    assert any(url.endswith("/careers") or url.endswith("/career") for url in urls)


def test_career_page_search_queries_use_configured_search_endpoint(temp_loaded):
    from jobagent.discover import seed_frontier
    from jobagent.db import Database

    cfg = temp_loaded.config
    cfg.exploration.mode = "whitelist_only"
    cfg.companies.whitelist_search_when_seeding = True
    cfg.companies.whitelist = ["ZEISS"]
    cfg.companies.known_domains = {"ZEISS": ["zeiss.com"]}
    cfg.companies.infer_domains_from_company_names = False
    cfg.companies.career_page_search_templates = ["https://search.local/?q={query}"]
    cfg.companies.max_career_page_searches_per_company = 1
    cfg.companies.portal_role_terms = []
    cfg.companies.max_portal_role_terms_per_company = 0
    temp_loaded.paths.seeds_path.write_text("", encoding="utf-8")

    db = Database(temp_loaded.paths.database_path, cfg)
    seed_frontier(cfg, db, temp_loaded.paths.seeds_path)
    rows = db.conn.execute("select url, discovered_from, reason from frontier order by url").fetchall()
    assert any(row["discovered_from"] == "company-career-search-query" and "search.local" in row["url"] for row in rows)
    db.close()
