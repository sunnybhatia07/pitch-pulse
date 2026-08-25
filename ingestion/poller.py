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
from ingestion import match_status
from utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__, settings.LOG_LEVEL)

# During ACTIVE play, spend 1 extra request on a status check every
# this-many event polls, so we notice a transition to HT/FT reasonably
# quickly without checking status on every single cycle.
STATUS_CHECK_EVERY_N_POLLS = 5

# While PAUSED (halftime, suspension, etc.), how long to wait between
# cheap status-only checks. Deliberately much longer than the normal
# poll interval, since nothing new can happen during a pause.
PAUSE_RECHECK_INTERVAL_SECONDS = 180


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
    status = chosen["fixture"]["status"]["short"]
    logger.info(f"Selected fixture {fixture_id}: {home} vs {away} (status={status})")
    return chosen


def _handle_paused(client: ApiFootballClient, fixture_id: int) -> str | None:
    """
    Called once per cycle while the match is PAUSED (HT, BT, SUSP, INT).

    Does NOT call get_fixture_events() -- nothing new can happen during
    a break, so polling events here would just re-fetch identical data
    and waste quota. Instead sleeps a long interval and checks status,
    returning the fresh status so the caller can decide what to do next.
    """
    logger.info(
        f"Match paused -- skipping event poll, "
        f"rechecking status in {PAUSE_RECHECK_INTERVAL_SECONDS}s"
    )
    time.sleep(PAUSE_RECHECK_INTERVAL_SECONDS)
    return client.get_fixture_status(fixture_id)


def run_poll_loop(
    client: ApiFootballClient,
    buffer: EventBuffer,
    fixture_id: int,
    initial_status: str,
) -> None:
    """
    Runs the poll loop for a single fixture until it ends, the quota
    is exhausted, or the process is interrupted.

    State machine:
      ACTIVE   -> poll events at normal cadence; every Nth cycle, also
                  spend one request confirming status hasn't changed
      PAUSED   -> stop polling events; check status on a long interval
                  until play resumes
      FINISHED -> stop the loop

    initial_status comes from select_match()'s already-fetched fixture
    data, so we don't waste a request re-checking status we already know.
    """
    interval = settings.API_FOOTBALL_POLL_INTERVAL_SECONDS
    poll_count = 0
    status = initial_status

    while True:
        try:
            if match_status.is_paused(status):
                status = _handle_paused(client, fixture_id)

            else:
                events = client.get_fixture_events(fixture_id)
                new_events = buffer.filter_new(events)
                logger.info(
                    f"Poll complete: {len(events)} total events returned, "
                    f"{len(new_events)} new"
                )
                for event in new_events:
                    save_event(fixture_id, event)

                poll_count += 1
                # Piggyback a status check every Nth cycle so we notice
                # HT/FT reasonably quickly without checking every poll.
                if poll_count % STATUS_CHECK_EVERY_N_POLLS == 0:
                    status = client.get_fixture_status(fixture_id)

                if client.last_rate_limit and client.last_rate_limit.is_low:
                    logger.warning(
                        f"Quota running low: {client.last_rate_limit.remaining_day} "
                        f"requests remaining today."
                    )

                time.sleep(interval)

        except QuotaExceededError as e:
            logger.warning(f"Stopping poll loop: {e}")
            break

        if match_status.is_finished(status):
            logger.info(f"Match status is '{status}' -- match over, stopping poll loop.")
            break


def main():
    client = ApiFootballClient()
    buffer = EventBuffer()

    match = select_match(client)
    if match is None:
        logger.info("Nothing to track. Exiting.")
        return

    fixture_id = match["fixture"]["id"]
    initial_status = match["fixture"]["status"]["short"]

    try:
        run_poll_loop(client, buffer, fixture_id, initial_status)
    except KeyboardInterrupt:
        logger.info(f"Interrupted by user. Total events captured: {len(buffer)}")


if __name__ == "__main__":
    main()