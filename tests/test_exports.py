from __future__ import annotations

import csv
import json

from jobagent.db import Database
from jobagent.models import JobMatch


EXPORT_FIELDS = [
    "fit_score",
    "title",
    "company",
    "location",
    "url",
    "reason",
    "evidence",
    "source_key",
    "first_seen_at",
    "last_seen_at",
]


def test_job_exports_use_the_public_schema(temp_loaded):
    csv_path = temp_loaded.paths.csv_export_path.parent / "test.csv"
    jsonl_path = temp_loaded.paths.jsonl_export_path.parent / "test.jsonl"
    db = Database(
        temp_loaded.paths.database_path,
        temp_loaded.config,
        csv_export_path=csv_path,
        jsonl_export_path=jsonl_path,
    )

    assert db.save_jobs(
        [
            JobMatch(
                title="Supplier Quality Manager",
                company="Example GmbH",
                location="Munich",
                url="https://example.test/jobs/supplier-quality-manager",
                fit_score=88,
                reason="Supplier quality role in Munich",
                evidence="Supplier Quality Manager",
            )
        ],
        "example.test/jobs",
    ) == 1

    with csv_path.open(encoding="utf-8", newline="") as file:
        header = next(csv.reader(file))
    exported = json.loads(jsonl_path.read_text(encoding="utf-8").strip())
    assert header == EXPORT_FIELDS
    assert list(exported) == EXPORT_FIELDS
    db.close()
