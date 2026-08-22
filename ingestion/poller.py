"""
The orchestrator: ties ApiFootballClient + EventBuffer + save_event
together into a running poll loop for ONE live match.

Sequence:
  1. Ask the API for currently live fixtures, pick one to track
  2. Every POLL_INTERVAL seconds:
       - fetch the full event list for that fixture
       - filter to only new events via EventBuffer
       - hand each new event to save_event() (stub for now)
  3. Stop when the match ends, the quota runs out, or on Ctrl+C
"""

import time

from ingestion.api_client import ApiFootballClient, QuotaExceededError
from ingestion.event_buffer import EventBuffer
from ingestion.db_writer import save_event
from utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__, settings.LOG_LEVEL)

# Fixture statuses that mean "match is over, stop polling"
FINISHED_STATUSES = {"FT", "AET", "PEN", "PST", "CANC", "ABD", "AWD", "WO"}


def select_match(client: ApiFootballClient) -> dict | None:
    """
    Fetches all currently live fixtures and lets the user pick which
    one to track. Costs 1 request total (the list call) regardless of
    how many fixtures are live -- the user is just choosing among the
    single response we already have.
    """
    live_fixtures = client.get_live_fixtures()
    if not live_fixtures:
        logger.warning("No live fixtures found right now.")
        return None

    print(f"\n{len(live_fixtures)} live match(es) found:\n")
    for i, fixture in enumerate(live_fixtures):
        home = fixture["teams"]["home"]["name"]
        away = fixture["teams"]["away"]["name"]
        league = fixture["league"]["name"]
        country = fixture["league"]["country"]
        elapsed = fixture["fixture"]["status"]["elapsed"]
        print(f"  [{i}] {home} vs {away}  -  {league} ({country})  -  {elapsed}'")

    while True:
        choice = input(f"\nSelect a match [0-{len(live_fixtures) - 1}]: ").strip()
        if choice.isdigit() and 0 <= int(choice) < len(live_fixtures):
            chosen = live_fixtures[int(choice)]
            break
        print("Invalid selection, try again.")

    fixture_id = chosen["fixture"]["id"]
    home = chosen["teams"]["home"]["name"]
    away = chosen["teams"]["away"]["name"]
    logger.info(f"Selected fixture {fixture_id}: {home} vs {away}")
    return chosen


def run_poll_loop(client: ApiFootballClient, buffer: EventBuffer, fixture_id: int) -> None:
    """
    Runs the poll loop for a single fixture until it ends, the quota
    is exhausted, or the process is interrupted.
    """
    interval = settings.API_FOOTBALL_POLL_INTERVAL_SECONDS

    while True:
        try:
            events = client.get_fixture_events(fixture_id)
        except QuotaExceededError as e:
            logger.warning(f"Stopping poll loop: {e}")
            break

        new_events = buffer.filter_new(events)
        logger.info(
            f"Poll complete: {len(events)} total events returned, "
            f"{len(new_events)} new"
        )
        for event in new_events:
            save_event(fixture_id, event)

        # Check match status by re-fetching live fixtures list would cost
        # another request, so instead we check the status embedded in the
        # events response's parent fixture data when available. Simplest
        # reliable approach for now: rely on QuotaExceededError or manual
        # stop; a cleaner status check is a good next refinement.

        if client.last_rate_limit and client.last_rate_limit.is_low:
            logger.warning(
                f"Quota running low: {client.last_rate_limit.remaining_day} "
                f"requests remaining today."
            )

        time.sleep(interval)


def main():
    client = ApiFootballClient()
    buffer = EventBuffer()

    match = select_match(client)
    if match is None:
        logger.info("Nothing to track. Exiting.")
        return

    fixture_id = match["fixture"]["id"]

    try:
        run_poll_loop(client, buffer, fixture_id)
    except KeyboardInterrupt:
        logger.info(f"Interrupted by user. Total events captured: {len(buffer)}")


if __name__ == "__main__":
    main()