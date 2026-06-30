from __future__ import annotations

from jobagent.db import Database


def test_source_memory_rewards_high_fit_jobs(temp_loaded):
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    row = db.update_source_memory(
        source_key="acme.test/jobs",
        domain="acme.test",
        status="ok",
        jobs_found=1,
        high_fit_jobs=1,
        source_quality=90,
        notes="good procurement jobs",
    )
    assert row.score > temp_loaded.config.memory.initial_score
    assert row.jobs_found == 1
    assert row.high_fit_jobs == 1
    db.close()


def test_source_memory_penalizes_repeated_no_job_pages(temp_loaded):
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    for _ in range(temp_loaded.config.memory.no_job_streak_penalty_after + 1):
        row = db.update_source_memory(
            source_key="noise.test/careers",
            domain="noise.test",
            status="ok",
            jobs_found=0,
            high_fit_jobs=0,
            source_quality=10,
            notes="no useful openings",
        )
    assert row.score < temp_loaded.config.memory.initial_score
    assert row.no_job_streak >= temp_loaded.config.memory.no_job_streak_penalty_after
    db.close()


def test_memory_persists_across_database_reopen(temp_loaded):
    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    db.update_source_memory(
        source_key="persist.test/jobs",
        domain="persist.test",
        status="ok",
        jobs_found=1,
        high_fit_jobs=0,
        source_quality=70,
        notes="persisted",
    )
    db.close()

    db2 = Database(temp_loaded.paths.database_path, temp_loaded.config)
    row = db2.get_source("persist.test/jobs")
    assert row.notes == "persisted"
    assert row.visits == 1
    db2.close()


def test_enqueue_recovers_stale_active_frontier_without_page_record(temp_loaded):
    from jobagent.discover import make_frontier_item

    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    item = make_frontier_item(
        url="https://stale.test/careers",
        depth=0,
        discovered_from="seed-file",
        reason="configured seed URL",
        config=temp_loaded.config,
        db=db,
        link_hint=1.0,
    )
    assert db.enqueue(item)
    popped = db.pop_frontier()
    assert popped is not None
    assert db.queued_count() == 0

    assert db.enqueue(item)
    assert db.queued_count() == 1
    db.close()


def test_recalibrate_existing_jobs_drops_old_wrong_city_rows(temp_loaded):
    from jobagent.models import JobMatch

    db = Database(temp_loaded.paths.database_path, temp_loaded.config)
    db.save_jobs(
        [
            JobMatch(
                title="Procurement Manager Optical Components",
                company="Old GmbH",
                location="Oberkochen",
                url="https://old.test/jobs/procurement-manager-oberkochen",
                fit_score=92,
                reason="Old over-scored row before radius filtering",
                evidence="Procurement Manager Oberkochen",
                score_source="llm",
            )
        ],
        source_page="https://old.test/jobs",
        source_key="old.test/jobs",
    )
    assert db.count_rows("jobs") == 1
    result = db.recalibrate_existing_jobs()
    assert result["checked"] == 1
    assert result["dropped"] == 1
    assert db.count_rows("jobs") == 0
    db.close()
