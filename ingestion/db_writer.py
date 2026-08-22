"""
Placeholder for writing new match events to Postgres.

Right now this just logs each event's fields clearly, so we can watch
real API-Football payloads and use that to design the match_events
table in Phase 3. Once that schema exists, save_event() gets replaced
with a real INSERT — nothing else in the poll loop needs to change,
since the loop only calls this one function and doesn't care what's
inside it.
"""

from typing import Any

from utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__, settings.LOG_LEVEL)


def save_event(fixture_id: int, event: dict[str, Any]) -> None:
    """
    STUB: will become a Postgres INSERT into match_events in Phase 3.

    For now, logs the fields we'd need to store, so we can confirm the
    real shape of API-Football's event payloads before designing columns.
    """
    minute = event.get("time", {}).get("elapsed")
    extra_minute = event.get("time", {}).get("extra")
    team = event.get("team", {}).get("name")
    player = event.get("player", {}).get("name")
    assist = event.get("assist", {}).get("name")
    event_type = event.get("type")
    detail = event.get("detail")
    comments = event.get("comments")

    logger.info(
        f"[STUB SAVE] fixture={fixture_id} minute={minute}+{extra_minute or 0} "
        f"team={team} player={player} assist={assist} "
        f"type={event_type} detail={detail} comments={comments}"
    )