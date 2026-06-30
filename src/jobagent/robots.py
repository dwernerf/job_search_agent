from __future__ import annotations

from urllib.parse import urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests

from .config import JobAgentConfig


class RobotsCache:
    def __init__(self, config: JobAgentConfig) -> None:
        self.config = config
        self._cache: dict[str, RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        if not self.config.crawler.respect_robots_txt:
            return True

        parsed = urlparse(url)
        root = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))

        if root not in self._cache:
            self._cache[root] = self._load(root)

        parser = self._cache[root]
        if parser is None:
            return not self.config.crawler.strict_robots_when_unavailable

        try:
            return parser.can_fetch(self.config.app.user_agent, url)
        except Exception:
            return not self.config.crawler.strict_robots_when_unavailable

    def _load(self, root: str) -> RobotFileParser | None:
        robots_url = f"{root}/robots.txt"
        parser = RobotFileParser(robots_url)

        try:
            response = requests.get(
                robots_url,
                headers={"User-Agent": self.config.app.user_agent},
                timeout=self.config.crawler.robots_timeout_seconds,
            )
        except requests.RequestException:
            return None

        if response.status_code in {401, 403}:
            return None if not self.config.crawler.strict_robots_when_unavailable else parser

        if response.status_code >= 400:
            return None

        parser.parse(response.text.splitlines())
        return parser
