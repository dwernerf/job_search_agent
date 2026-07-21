from __future__ import annotations

import random
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any

from .config import JobAgentConfig
from .models import LinkCandidate, PageSnapshot
from .structured import extract_jobpostings, structured_jobs_as_text


class BrowserFetchError(RuntimeError):
    def __init__(
        self,
        *,
        kind: str,
        requested_url: str,
        final_url: str = "",
        status_code: int | None = None,
    ) -> None:
        self.kind = kind
        self.requested_url = requested_url
        self.final_url = final_url
        self.status_code = status_code

        if kind == "http":
            message = f"HTTP {status_code or 0}"
        else:
            message = f"browser {kind} failure"
        super().__init__(f"{message} while opening {requested_url}")

    @property
    def page_status(self) -> str:
        if self.kind == "http":
            return f"error:http_{self.status_code or 'unknown'}"
        if self.kind == "navigation_timeout":
            return "error:navigation_timeout"
        return f"error:{self.kind}"


class BrowserSession:
    def __init__(
        self,
        config: JobAgentConfig,
        *,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        uniform: Callable[[float, float], float] | None = None,
    ) -> None:
        self.config = config
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._uniform = uniform or random.uniform
        self._last_navigation_at: float | None = None
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None

    def __enter__(self) -> "BrowserSession":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run `pip install -e .` and `playwright install --with-deps chromium`."
            ) from exc

        self._playwright = sync_playwright().start()
        browser_type = getattr(self._playwright, self.config.browser.engine)
        self._browser = browser_type.launch(headless=self.config.browser.headless)
        self._context = self._browser.new_context(**self._context_options())
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._context is not None:
            self._context.close()
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()

    def fetch(self, url: str) -> PageSnapshot:
        if self._context is None:
            raise RuntimeError("BrowserSession must be used as a context manager")

        page: Any = None
        phase = "setup"

        try:
            page = self._context.new_page()
            page.set_default_timeout(self.config.browser.navigation_timeout_ms)
            self._pace()
            phase = "navigation"
            response = page.goto(
                url,
                wait_until=self.config.browser.wait_until,
                timeout=self.config.browser.navigation_timeout_ms,
            )
            final_url = self._page_url(page, url)
            if response is None:
                raise BrowserFetchError(
                    kind="no_response",
                    requested_url=url,
                    final_url=final_url,
                )

            status_code = int(response.status)
            if (
                self.config.browser.fail_on_http_error_statuses
                and status_code >= self.config.browser.http_error_status_min
            ):
                raise self._http_error(
                    response=response,
                    requested_url=url,
                    final_url=final_url,
                )

            if self.config.browser.network_idle_timeout_ms > 0:
                try:
                    page.wait_for_load_state(
                        "networkidle",
                        timeout=self.config.browser.network_idle_timeout_ms,
                    )
                except Exception:
                    pass

            phase = "page_processing"
            title = page.title() or ""

            try:
                text = page.locator("body").inner_text(
                    timeout=self.config.browser.body_text_timeout_ms
                )
            except Exception:
                text = ""

            raw_links = page.eval_on_selector_all(
                "a",
                """els => els.map(a => ({
                    text: (a.innerText || a.getAttribute('aria-label') || a.getAttribute('title') || '').trim(),
                    url: a.href || a.getAttribute('href') || ''
                })).filter(x => x.url)""",
            )

            links = [
                LinkCandidate(text=str(item.get("text") or ""), url=str(item.get("url") or ""))
                for item in raw_links
            ]

            try:
                raw_json_ld = page.eval_on_selector_all(
                    "script[type='application/ld+json']",
                    "els => els.map(e => e.textContent || '').filter(Boolean)",
                )
            except Exception:
                raw_json_ld = []

            structured_jobs = extract_jobpostings([str(x) for x in raw_json_ld], page.url or url)
            structured_text = structured_jobs_as_text(structured_jobs)
            if structured_text:
                text = (text + "\n\n" + structured_text).strip()

            return PageSnapshot(
                url=url,
                final_url=self._page_url(page, url),
                title=title,
                text=text,
                links=links,
                status_code=status_code,
            )
        except BrowserFetchError:
            raise
        except Exception as exc:
            if phase == "navigation":
                kind = (
                    "navigation_timeout"
                    if "timeout" in type(exc).__name__.casefold()
                    else "navigation"
                )
            elif phase == "setup":
                kind = "setup"
            else:
                kind = "page_processing"
            raise BrowserFetchError(
                kind=kind,
                requested_url=url,
                final_url=self._page_url(page, url),
            ) from exc
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    def _context_options(self) -> dict[str, object]:
        return {
            "user_agent": self.config.app.user_agent,
            "viewport": {
                "width": self.config.browser.viewport_width,
                "height": self.config.browser.viewport_height,
            }
        }

    def _pace(self) -> None:
        now = self._clock()
        if self._last_navigation_at is None:
            self._last_navigation_at = now
            return

        interval = self._uniform(
            self.config.run.min_delay_seconds,
            self.config.run.max_delay_seconds,
        )
        remaining = interval - (now - self._last_navigation_at)
        if remaining > 0:
            self._sleeper(remaining)
        self._last_navigation_at = self._clock()

    @staticmethod
    def _page_url(page: Any, fallback: str) -> str:
        try:
            return str(page.url or fallback)
        except Exception:
            return fallback

    def _http_error(
        self,
        *,
        response: Any,
        requested_url: str,
        final_url: str,
    ) -> BrowserFetchError:
        return BrowserFetchError(
            kind="http",
            requested_url=requested_url,
            final_url=final_url,
            status_code=int(response.status),
        )
