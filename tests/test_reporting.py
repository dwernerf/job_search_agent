from __future__ import annotations

import io
import logging

from jobagent.reporting import ActionReporter


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def make_stream_logger(level: int = logging.INFO) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    logger = logging.getLogger("jobagent-test-reporting")
    logger.handlers.clear()
    logger.setLevel(level)
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger, stream


def test_action_reporter_prints_structured_action_lines_without_truncation(temp_loaded):
    cfg = temp_loaded.config
    cfg.logging.max_url_chars = 30
    logger, stream = make_stream_logger()
    reporter = ActionReporter(cfg, logger, clock=FakeClock())

    long_url = "https://example.test/" + "very-long-path" * 10
    reporter.action("open_page", url=long_url, depth=1)

    output = stream.getvalue()
    assert "STEP open_page" in output
    assert "depth='1'" in output
    assert long_url in output


def test_action_reporter_no_longer_emits_interval_summary(temp_loaded):
    cfg = temp_loaded.config
    logger, stream = make_stream_logger()
    clock = FakeClock()
    reporter = ActionReporter(cfg, logger, clock=clock)

    reporter.record_enqueued(5)
    reporter.record_page(status="ok", jobs_seen=1, jobs_saved=1, high_fit_jobs=1, source_quality=80, queued=3)
    clock.advance(9999)
    reporter.record_page(status="error:KeyError", jobs_seen=0, jobs_saved=0, source_quality=0, queued=2)

    assert "run_summary" not in stream.getvalue()

    reporter.maybe_summary(queued=2, force=True)
    output = stream.getvalue()
    assert "RESULT run_summary" in output
    assert "jobs_saved=1" in output
    assert "enqueued_urls=5" in output
    assert "errors=1" in output
    assert "avg_source_quality=80.0" in output


def test_action_reporter_hides_debug_only_events_at_info_level(temp_loaded):
    cfg = temp_loaded.config
    logger, stream = make_stream_logger(logging.INFO)
    reporter = ActionReporter(cfg, logger, clock=FakeClock())

    reporter.action("skip_visited", url="https://example.test/jobs")
    assert stream.getvalue() == ""

    logger, stream = make_stream_logger(logging.DEBUG)
    reporter = ActionReporter(cfg, logger, clock=FakeClock())
    reporter.action("skip_visited", url="https://example.test/jobs")
    assert "STEP skip_visited" in stream.getvalue()


def test_action_reporter_prints_batch_complete_as_result(temp_loaded):
    logger, stream = make_stream_logger()
    reporter = ActionReporter(temp_loaded.config, logger, clock=FakeClock())

    reporter.action(
        "batch_complete",
        batch="1/2",
        saved=2,
        high_fit=1,
        enqueued=3,
        queued=5,
        source_quality=80,
        source_notes="Relevant source",
    )

    output = stream.getvalue()
    assert "RESULT batch_complete" in output
    assert "batch='1/2'" in output
    assert "saved='2'" in output
    assert "source_notes='Relevant source'" in output
