"""
Holds the "already seen" record for one match's live events, and
performs deduplication against it on each poll.

This file does NOT call the API and does NOT touch Postgres — it only
knows how to fingerprint a raw API-Football event dict and remember
which fingerprints it has already returned as "new".

One EventBuffer instance = one match. Create a fresh one each time
you start tracking a new fixture.
"""

from typing import Any

from utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__, settings.LOG_LEVEL)

# A composite key uniquely (in practice) identifying one match event.
EventKey = tuple[int, int | None, int | None, str, str | None]


class EventBuffer:
    def __init__(self):
        # The "notepad": every composite key we've already handed off.
        self._seen: set[EventKey] = set()

    @staticmethod
    def _build_key(event: dict[str, Any]) -> EventKey:
        """
        Builds the composite key (minute, team_id, player_id, type, detail)
        from one raw event object as returned by /fixtures/events.

        Missing player_id (e.g. some VAR events) is kept as None rather
        than faked, since "no player" is itself a meaningful, consistent
        part of the fingerprint for that event type.
        """
        minute = event.get("time", {}).get("elapsed")
        team_id = event.get("team", {}).get("id")
        player_id = event.get("player", {}).get("id")
        event_type = event.get("type", "")
        detail = event.get("detail")

        return (minute, team_id, player_id, event_type, detail)

    def filter_new(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Given the FULL event list from the latest poll, returns only the
        events not seen in a previous call, and marks them as seen.

        Order is preserved from the input list.
        """
        new_events = []

        for event in events:
            key = self._build_key(event)
            if key in self._seen:
                continue
            self._seen.add(key)
            new_events.append(event)

        if new_events:
            logger.info(f"EventBuffer: {len(new_events)} new event(s) this poll")
        else:
            logger.debug("EventBuffer: no new events this poll")

        return new_events

    def __len__(self) -> int:
        """Total distinct events seen so far this match."""
        return len(self._seen)