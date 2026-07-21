from __future__ import annotations

import pytest

from jobagent.browser import BrowserFetchError, BrowserSession


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class FakePage:
    def __init__(self, result, *, final_url: str = "https://example.test/final") -> None:
        self.result = result
        self.url = final_url
        self.closed = False
        self.requested_url = ""

    def set_default_timeout(self, timeout: int) -> None:
        self.timeout = timeout

    def goto(self, url: str, **kwargs):
        self.requested_url = url
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    def new_page(self) -> FakePage:
        return self.page


def session_with_page(temp_loaded, page: FakePage, clock: FakeClock | None = None) -> BrowserSession:
    clock = clock or FakeClock()
    session = BrowserSession(
        temp_loaded.config,
        clock=clock,
        sleeper=clock.sleep,
        uniform=lambda minimum, maximum: maximum,
    )
    session._context = FakeContext(page)
    return session


@pytest.mark.parametrize("status", [403, 429, 503])
def test_fetch_raises_structured_error_for_any_configured_http_error(temp_loaded, status):
    response = FakeResponse(status)
    page = FakePage(response)
    session = session_with_page(temp_loaded, page)

    with pytest.raises(BrowserFetchError) as raised:
        session.fetch("https://example.test/start")

    error = raised.value
    assert error.page_status == f"error:http_{status}"
    assert error.status_code == status
    assert error.final_url == "https://example.test/final"
    assert page.closed


def test_fetch_wraps_navigation_timeout_and_closes_page(temp_loaded):
    class NavigationTimeout(Exception):
        pass

    page = FakePage(NavigationTimeout("timed out"))
    session = session_with_page(temp_loaded, page)

    with pytest.raises(BrowserFetchError) as raised:
        session.fetch("https://example.test/start")

    assert raised.value.kind == "navigation_timeout"
    assert raised.value.page_status == "error:navigation_timeout"
    assert page.closed


def test_fetch_treats_missing_main_response_as_error(temp_loaded):
    page = FakePage(None)
    session = session_with_page(temp_loaded, page)

    with pytest.raises(BrowserFetchError) as raised:
        session.fetch("https://example.test/start")

    assert raised.value.page_status == "error:no_response"
    assert page.closed


def test_fetch_passes_requested_url_to_playwright_unchanged(temp_loaded):
    url = (
        "https://www.stepstone.de/jobs/mitarbeiter-in-strategischer-einkauf/"
        "in-m%c3%bcnchen?action=facet_selected%3Bage%3Bage_1&ag=age_1"
    )
    page = FakePage(RuntimeError("stop after capturing URL"))
    session = session_with_page(temp_loaded, page)

    with pytest.raises(BrowserFetchError):
        session.fetch(url)

    assert page.requested_url == url


def test_pacing_enforces_one_interval_between_navigation_starts(temp_loaded):
    temp_loaded.config.run.min_delay_seconds = 2
    temp_loaded.config.run.max_delay_seconds = 2
    clock = FakeClock()
    session = BrowserSession(
        temp_loaded.config,
        clock=clock,
        sleeper=clock.sleep,
        uniform=lambda minimum, maximum: maximum,
    )

    session._pace()
    assert clock.sleeps == []

    clock.advance(0.5)
    session._pace()
    assert clock.sleeps == [1.5]

    clock.advance(3)
    session._pace()
    assert clock.sleeps == [1.5]


def test_pacing_can_be_disabled_and_context_uses_configured_user_agent(temp_loaded):
    temp_loaded.config.run.min_delay_seconds = 0
    temp_loaded.config.run.max_delay_seconds = 0
    clock = FakeClock()
    session = BrowserSession(
        temp_loaded.config,
        clock=clock,
        sleeper=clock.sleep,
        uniform=lambda minimum, maximum: maximum,
    )

    session._pace()
    session._pace()

    assert clock.sleeps == []
    assert session._context_options()["user_agent"] == temp_loaded.config.app.user_agent
