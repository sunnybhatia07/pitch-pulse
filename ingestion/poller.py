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
from ingestion.db_writer import save_poll
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


def _summarize_statistics(statistics: list[dict], elapsed: int | None = None) -> str:
    """
    Formats the raw statistics payload (list of one dict per team, each
    holding its own list of {"type": ..., "value": ...} pairs) into a
    single readable log line -- ALL stat types returned by the API,
    not a curated subset, so nothing is lost before Phase 3 decides
    what to keep.

    Statistics themselves carry no minute field (they're whole-match
    running totals, not timestamped like events), so the current match
    clock is passed in separately from the fixture's status.elapsed --
    same idea as the min= shown on each player line, just sourced from
    the fixture rather than from the stat itself.
    """
    if not statistics:
        return "(no statistics available)"

    prefix = f"minute={elapsed} | " if elapsed is not None else ""

    team_summaries = []
    for team_block in statistics:
        team_name = team_block.get("team", {}).get("name", "Unknown")
        stat_list = team_block.get("statistics", []) or []
        values = {s.get("type"): s.get("value") for s in stat_list}
        formatted = ", ".join(f"{k}={v}" for k, v in values.items())
        team_summaries.append(f"{team_name}: {formatted}")

    return prefix + " | ".join(team_summaries)


def _summarize_players(players: list[dict]) -> str:
    """
    Formats the raw players payload into one readable log line PER
    PLAYER -- ALL statistic categories the API returns, including the
    ones outfield-only formatting used to miss (goalkeeper saves live
    under goals.saves, not under shots/dribbles/tackles), plus
    offsides and penalty stats.

    Players who haven't recorded any minutes yet are still skipped --
    an unused substitute's stats are all null/zero and add nothing
    but noise. Everyone who's actually been on the pitch, including
    the goalkeeper, gets every field the API provides for them.
    """
    if not players:
        return "(no player data available)"

    lines = []
    for team_block in players:
        team_name = team_block.get("team", {}).get("name", "Unknown")
        for entry in team_block.get("players", []) or []:
            info = entry.get("player", {}) or {}
            name = info.get("name", "Unknown")

            stats_list = entry.get("statistics", []) or []
            if not stats_list:
                continue
            stats = stats_list[0]

            games = stats.get("games", {}) or {}
            minutes = games.get("minutes")
            if not minutes:
                continue  # hasn't played yet -- skip the noise

            position = games.get("position")
            rating = games.get("rating")
            captain = games.get("captain")
            offsides = stats.get("offsides")
            shots = stats.get("shots", {}) or {}
            goals = stats.get("goals", {}) or {}
            passes = stats.get("passes", {}) or {}
            tackles = stats.get("tackles", {}) or {}
            duels = stats.get("duels", {}) or {}
            dribbles = stats.get("dribbles", {}) or {}
            fouls = stats.get("fouls", {}) or {}
            cards = stats.get("cards", {}) or {}
            penalty = stats.get("penalty", {}) or {}

            lines.append(
                f"  [{team_name}] {name} ({position}{' C' if captain else ''}) "
                f"min={minutes} rating={rating} offsides={offsides} | "
                f"shots={shots.get('total')}/on={shots.get('on')} "
                f"goals={goals.get('total')} assists={goals.get('assists')} "
                f"conceded={goals.get('conceded')} saves={goals.get('saves')} | "
                f"passes={passes.get('total')} key={passes.get('key')} acc={passes.get('accuracy')} | "
                f"tackles={tackles.get('total')} blocks={tackles.get('blocks')} "
                f"interceptions={tackles.get('interceptions')} "
                f"duels_won={duels.get('won')}/{duels.get('total')} "
                f"dribbles={dribbles.get('success')}/{dribbles.get('attempts')} past={dribbles.get('past')} | "
                f"fouls_drawn={fouls.get('drawn')} fouls_committed={fouls.get('committed')} "
                f"cards=Y{cards.get('yellow')}/R{cards.get('red')} | "
                f"pen_won={penalty.get('won')} pen_committed={penalty.get('commited')} "
                f"pen_scored={penalty.get('scored')} pen_missed={penalty.get('missed')} pen_saved={penalty.get('saved')}"
            )

    return "\n".join(lines) if lines else "(no players with recorded minutes yet)"


def _handle_paused(client: ApiFootballClient, buffer: EventBuffer, fixture_id: int) -> str | None:
    """
    Called once per cycle while the match is PAUSED (HT, BT, SUSP, INT).

    Still pulls the FULL snapshot (events, statistics, players), not
    just status -- the combined endpoint returns all of it for the
    same 1-request cost regardless of what we read out of it, so
    discarding events/statistics/players here just to check status
    was throwing away data for free. This closes the gap where no
    data got logged during a pause window.

    Any genuinely new event that lands right at the pause boundary
    (rare, but possible) still gets deduped and saved normally.
    """
    snapshot = client.get_fixture_snapshot(fixture_id)
    if snapshot is None:
        logger.warning("Snapshot returned no data while paused, will retry.")
        time.sleep(PAUSE_RECHECK_INTERVAL_SECONDS)
        return None

    status = snapshot.get("fixture", {}).get("status", {}).get("short")
    elapsed = snapshot.get("fixture", {}).get("status", {}).get("elapsed")
    events = snapshot.get("events", []) or []
    statistics = snapshot.get("statistics", []) or []
    players = snapshot.get("players", []) or []

    new_events = buffer.filter_new(events)
    logger.info(
        f"[PAUSED check] status={status} | {len(events)} total events, "
        f"{len(new_events)} new"
    )
    logger.info(f"Statistics -> {_summarize_statistics(statistics, elapsed)}")
    logger.info(f"Players ->\n{_summarize_players(players)}")
    save_poll(fixture_id, snapshot, new_events)

    logger.info(f"Rechecking again in {PAUSE_RECHECK_INTERVAL_SECONDS}s")
    time.sleep(PAUSE_RECHECK_INTERVAL_SECONDS)
    return status


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
                status = _handle_paused(client, buffer, fixture_id)

            else:
                snapshot = client.get_fixture_snapshot(fixture_id)
                if snapshot is None:
                    logger.warning("Snapshot returned no data, will retry next cycle.")
                    time.sleep(interval)
                    continue

                status = snapshot.get("fixture", {}).get("status", {}).get("short")
                elapsed = snapshot.get("fixture", {}).get("status", {}).get("elapsed")
                events = snapshot.get("events", []) or []
                statistics = snapshot.get("statistics", []) or []
                players = snapshot.get("players", []) or []

                new_events = buffer.filter_new(events)
                logger.info(
                    f"Poll complete: {len(events)} total events returned, "
                    f"{len(new_events)} new"
                )
                logger.info(f"Statistics -> {_summarize_statistics(statistics, elapsed)}")
                logger.info(f"Players ->\n{_summarize_players(players)}")

                save_poll(fixture_id, snapshot, new_events)


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