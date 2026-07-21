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


def test_action_reporter_prints_structured_action_fields():
    logger, stream = make_stream_logger()
    reporter = ActionReporter(logger, clock=FakeClock())
    long_url = "https://example.test/" + "very-long-path" * 10

    reporter.action("page_fetched", final_url=long_url, candidate_links=1)

    output = stream.getvalue()
    assert "RESULT page_fetched" in output
    assert "candidate_links='1'" in output
    assert long_url in output


def test_action_reporter_aggregates_pages_into_run_summary():
    logger, stream = make_stream_logger()
    clock = FakeClock()
    reporter = ActionReporter(logger, clock=clock)

    reporter.record_page(
        status="ok",
        jobs_saved=2,
    )
    reporter.record_page(status="error:RuntimeError")
    clock.advance(12.5)
    reporter.run_summary(queued=3)

    output = stream.getvalue()
    assert "RESULT run_summary" in output
    assert "pages=2" in output
    assert "jobs_saved=2" in output
    assert "errors=1" in output
    assert "queued=3" in output
    assert "elapsed_seconds=12.5" in output


def test_action_reporter_hides_debug_only_events_at_info_level():
    logger, stream = make_stream_logger(logging.INFO)
    reporter = ActionReporter(logger, clock=FakeClock())

    reporter.action("skip_visited", url="https://example.test/jobs")
    assert stream.getvalue() == ""

    logger, stream = make_stream_logger(logging.DEBUG)
    reporter = ActionReporter(logger, clock=FakeClock())
    reporter.action("skip_visited", url="https://example.test/jobs")
    assert "STEP skip_visited" in stream.getvalue()


def test_action_reporter_prints_batch_complete_as_result():
    logger, stream = make_stream_logger()
    reporter = ActionReporter(logger, clock=FakeClock())

    reporter.action(
        "batch_complete",
        batch="1/2",
        saved=2,
        enqueued=3,
        queued=5,
    )

    output = stream.getvalue()
    assert "RESULT batch_complete" in output
    assert "batch='1/2'" in output
    assert "saved='2'" in output
