"""
The orchestrator: ties ApiFootballClient + EventBuffer + save_event
together into a running poll loop for ONE live match.

Sequence:
  1. Ask the API for currently live fixtures, pick one to track
  2. Every POLL_INTERVAL seconds while the match is active:
       - pull one combined snapshot (status + events + statistics +
         players) from /fixtures?id={id}
       - filter events to only new ones via EventBuffer
       - hand each new event to save_event() (stub for now)
  3. While paused (e.g. halftime), skip snapshots and just check
     status on a long interval until play resumes
  4. Stop when the match ends, the quota runs out, or on Ctrl+C
"""

import time

from ingestion.api_client import ApiFootballClient, QuotaExceededError
from ingestion.event_buffer import EventBuffer
from ingestion.db_writer import save_event
from ingestion import match_status
from utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__, settings.LOG_LEVEL)

# While PAUSED (halftime, suspension, etc.), how long to wait between
# snapshot checks. Deliberately much longer than the normal poll
# interval, since nothing new can happen during a pause -- we're only
# checking to see whether status has changed yet.
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


# Stat types worth watching poll-to-poll for tactical signal. API-Football
# returns many more than this per team (Fouls, Offsides, Corners, etc.) --
# keeping this list short so the log stays readable; add to it as Phase 5
# metric needs become clearer.
TRACKED_STAT_TYPES = {"Ball Possession", "Total Shots", "Shots on Goal", "Passes %"}


def _summarize_statistics(statistics: list[dict]) -> str:
    """
    Formats the raw statistics payload (list of one dict per team, each
    holding its own list of {"type": ..., "value": ...} pairs) into a
    single readable log line, e.g.:

      "Abha: Ball Possession=48%, Total Shots=9 | Al Khaleej Saihat: ..."

    This is display-only -- it doesn't change what's stored anywhere,
    just makes it possible to visually confirm values are actually
    changing poll-to-poll rather than only seeing a static entry count.
    """
    if not statistics:
        return "(no statistics available)"

    team_summaries = []
    for team_block in statistics:
        team_name = team_block.get("team", {}).get("name", "Unknown")
        stat_list = team_block.get("statistics", []) or []
        values = {
            s.get("type"): s.get("value")
            for s in stat_list
            if s.get("type") in TRACKED_STAT_TYPES
        }
        formatted = ", ".join(f"{k}={v}" for k, v in values.items())
        team_summaries.append(f"{team_name}: {formatted}")

    return " | ".join(team_summaries)


def _handle_paused(client: ApiFootballClient, fixture_id: int) -> str | None:
    """
    Called once per cycle while the match is PAUSED (HT, BT, SUSP, INT).

    Sleeps a long interval, then pulls a snapshot purely to read its
    status back out -- the events/statistics/players in that response
    are ignored here since nothing new can happen during a break.
    """
    logger.info(
        f"Match paused -- waiting {PAUSE_RECHECK_INTERVAL_SECONDS}s "
        f"before rechecking status"
    )
    time.sleep(PAUSE_RECHECK_INTERVAL_SECONDS)
    snapshot = client.get_fixture_snapshot(fixture_id)
    if snapshot is None:
        return None
    return snapshot.get("fixture", {}).get("status", {}).get("short")


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
      ACTIVE   -> pull a full snapshot (status + events + statistics +
                  players, one request) every poll interval
      PAUSED   -> skip the normal cadence; check a snapshot on a long
                  interval until play resumes
      FINISHED -> stop the loop

    initial_status comes from select_match()'s already-fetched fixture
    data, so we don't waste a request re-checking status we already know.
    """
    interval = settings.API_FOOTBALL_POLL_INTERVAL_SECONDS
    status = initial_status

    while True:
        try:
            if match_status.is_paused(status):
                status = _handle_paused(client, fixture_id)

            else:
                snapshot = client.get_fixture_snapshot(fixture_id)
                if snapshot is None:
                    logger.warning("Snapshot returned no data, will retry next cycle.")
                    time.sleep(interval)
                    continue

                status = snapshot.get("fixture", {}).get("status", {}).get("short")
                events = snapshot.get("events", []) or []
                statistics = snapshot.get("statistics", []) or []
                players = snapshot.get("players", []) or []

                new_events = buffer.filter_new(events)
                logger.info(
                    f"Poll complete: {len(events)} total events returned, "
                    f"{len(new_events)} new | players entries: {len(players)}"
                )
                logger.info(f"Statistics -> {_summarize_statistics(statistics)}")
                for event in new_events:
                    save_event(fixture_id, event)

                # statistics/players aren't persisted yet -- no schema
                # for them until Phase 3. Logged now purely so we can
                # see their real shape while designing that schema.

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