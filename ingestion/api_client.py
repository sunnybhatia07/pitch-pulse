"""
Thin client around the API-Football v3 REST API.

Responsibilities of THIS file only:
  - Make the two HTTP calls we need (live fixtures, fixture events)
  - Track the daily request quota using the API's own response headers
  - Fail loudly and predictably when we're near/at the quota, so the
    poll loop (a later module) can decide what to do about it

This file does NOT touch Postgres, does NOT decide polling cadence,
and does NOT parse events into DB rows. Keeping it single-purpose
makes it easy to unit test with mocked HTTP responses.
"""

from dataclasses import dataclass
from typing import Any

import requests

from config.settings import settings
from utils.logger import get_logger

logger = get_logger(__name__, settings.LOG_LEVEL)


class QuotaExceededError(Exception):
    """Raised when the daily API-Football request quota is exhausted."""
    pass


@dataclass
class RateLimitStatus:
    """Snapshot of quota remaining, parsed from response headers."""
    limit_day: int | None
    remaining_day: int | None

    @property
    def is_low(self) -> bool:
        """True when we have 5 or fewer requests left today."""
        if self.remaining_day is None:
            return False
        return self.remaining_day <= 5

    @property
    def is_exhausted(self) -> bool:
        if self.remaining_day is None:
            return False
        return self.remaining_day <= 0


class ApiFootballClient:
    def __init__(self):
        self.base_url = settings.API_FOOTBALL_BASE_URL
        self.headers = {
            "x-apisports-key": settings.API_FOOTBALL_KEY,
        }
        self.last_rate_limit: RateLimitStatus | None = None

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """
        Internal helper: performs the GET, updates self.last_rate_limit
        from response headers, and raises QuotaExceededError if the
        response indicates we're out of requests for the day.
        """
        url = f"{self.base_url}{path}"
        response = requests.get(url, headers=self.headers, params=params, timeout=10)

        self.last_rate_limit = self._parse_rate_limit(response)
        if self.last_rate_limit.remaining_day is not None:
            logger.debug(
                f"API-Football quota: {self.last_rate_limit.remaining_day}"
                f"/{self.last_rate_limit.limit_day} requests remaining today"
            )

        if response.status_code == 429 or self.last_rate_limit.is_exhausted:
            raise QuotaExceededError(
                f"API-Football daily quota exhausted "
                f"(remaining={self.last_rate_limit.remaining_day})"
            )

        response.raise_for_status()
        payload = response.json()

        if payload.get("errors"):
            # API-Football returns HTTP 200 with an "errors" object for
            # things like a bad key or invalid params, so we check explicitly.
            raise RuntimeError(f"API-Football returned errors: {payload['errors']}")

        return payload

    @staticmethod
    def _parse_rate_limit(response: requests.Response) -> RateLimitStatus:
        limit = response.headers.get("x-ratelimit-requests-limit")
        remaining = response.headers.get("x-ratelimit-requests-remaining")
        return RateLimitStatus(
            limit_day=int(limit) if limit is not None else None,
            remaining_day=int(remaining) if remaining is not None else None,
        )

    def get_live_fixtures(self) -> list[dict]:
        """
        Returns all currently live fixtures across all leagues.
        Costs 1 request. Used once at startup to pick a match to track.
        """
        payload = self._get("/fixtures", params={"live": "all"})
        return payload.get("response", [])

    def get_fixture_events(self, fixture_id: int) -> list[dict]:
        """
        Returns the FULL list of events for a given fixture so far
        (not a delta since last call — API-Football always returns
        everything that's happened in the match up to now).
        Costs 1 request per call.
        """
        payload = self._get("/fixtures/events", params={"fixture": fixture_id})
        return payload.get("response", [])