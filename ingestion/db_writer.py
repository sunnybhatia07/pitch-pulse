"""
Writes one poll's worth of API-Football data into Postgres (Phase 3 schema).

The poller calls ONE function, save_poll(), with the snapshot it already
fetched. Everything for that poll is saved in a single transaction:

    1. teams + match      (dimensions -- facts point at these, so first)
    2. players + events   (new events only, already deduped by EventBuffer)
    3. team_stat_snapshots
    4. player_stat_snapshots

If anything fails, the whole poll is rolled back and logged -- the poller
keeps running instead of dying mid-match.

One connection is opened lazily on first use and reused for every poll.
"""

from typing import Any

import psycopg2
from psycopg2 import sql

from utils.logger import get_logger
from config.settings import settings

logger = get_logger(__name__, settings.LOG_LEVEL)

_conn = None


def _get_connection():
    """Returns the shared connection, (re)opening it if needed."""
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(
            host=settings.PG_HOST,
            port=settings.PG_PORT,
            user=settings.PG_USER,
            password=settings.PG_PASSWORD,
            dbname=settings.PG_DB,
        )
        logger.info("Connected to Postgres")
    return _conn


# ------------------------------------------------------------------
# Small helpers
# ------------------------------------------------------------------

def _to_int(value: Any) -> int | None:
    """API values arrive as int, None, or strings like '55%'. -> int/None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(float(str(value).strip().rstrip("%")))
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    """Ratings and xG arrive as strings like '7.2'. -> float/None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _insert(cur, table: str, row: dict[str, Any], suffix: str = "") -> None:
    """
    Builds and runs  INSERT INTO table (cols...) VALUES (...) <suffix>
    from a {column: value} dict, so column names and values can never
    get out of order.
    """
    cols = list(row.keys())
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) {}").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, cols)),
        sql.SQL(", ").join(sql.Placeholder() * len(cols)),
        sql.SQL(suffix),
    )
    cur.execute(query, list(row.values()))


# ------------------------------------------------------------------
# Dimensions: "make sure this exists" (does nothing if already there)
# ------------------------------------------------------------------

def _ensure_team(cur, team: dict[str, Any] | None) -> None:
    if not team or team.get("id") is None:
        return
    _insert(
        cur,
        "teams",
        {
            "team_id": team["id"],
            "name": team.get("name") or "Unknown",
            "logo_url": team.get("logo"),
        },
        "ON CONFLICT (team_id) DO NOTHING",
    )


def _ensure_player(cur, player_id: int | None, name: str | None, photo: str | None = None) -> None:
    if player_id is None:
        return
    _insert(
        cur,
        "players",
        {
            "player_id": player_id,
            "name": name or "Unknown",
            "photo_url": photo,
        },
        "ON CONFLICT (player_id) DO NOTHING",
    )


def _save_match(cur, snapshot: dict[str, Any]) -> None:
    """Inserts the match, or refreshes its live fields (status/minute/score)."""
    fx = snapshot.get("fixture", {}) or {}
    league = snapshot.get("league", {}) or {}
    teams = snapshot.get("teams", {}) or {}
    goals = snapshot.get("goals", {}) or {}
    status = fx.get("status", {}) or {}

    _ensure_team(cur, teams.get("home"))
    _ensure_team(cur, teams.get("away"))

    _insert(
        cur,
        "matches",
        {
            "fixture_id": fx["id"],
            "league_id": league.get("id"),
            "league_name": league.get("name"),
            "country": league.get("country"),
            "season": league.get("season"),
            "round": league.get("round"),
            "home_team_id": teams["home"]["id"],
            "away_team_id": teams["away"]["id"],
            "kickoff_utc": fx.get("date"),
            "status": status.get("short") or "SCHEDULED",
            "elapsed_minute": status.get("elapsed"),
            "home_score": goals.get("home"),
            "away_score": goals.get("away"),
            "venue": (fx.get("venue") or {}).get("name"),
            "referee": fx.get("referee"),
        },
        """ON CONFLICT (fixture_id) DO UPDATE SET
               status = EXCLUDED.status,
               elapsed_minute = EXCLUDED.elapsed_minute,
               home_score = EXCLUDED.home_score,
               away_score = EXCLUDED.away_score,
               updated_at = now()""",
    )


# ------------------------------------------------------------------
# Facts
# ------------------------------------------------------------------

def _save_events(cur, fixture_id: int, events: list[dict[str, Any]]) -> int:
    saved = 0
    for event in events:
        time_info = event.get("time", {}) or {}
        team = event.get("team", {}) or {}
        player = event.get("player", {}) or {}
        assist = event.get("assist", {}) or {}

        minute = time_info.get("elapsed")
        if minute is None or team.get("id") is None:
            logger.warning(f"Skipping event with no minute/team: {event}")
            continue

        _ensure_team(cur, team)
        _ensure_player(cur, player.get("id"), player.get("name"))
        _ensure_player(cur, assist.get("id"), assist.get("name"))

        _insert(
            cur,
            "match_events",
            {
                "fixture_id": fixture_id,
                "minute": minute,
                "extra_minute": time_info.get("extra"),
                "team_id": team["id"],
                "player_id": player.get("id"),
                "assist_id": assist.get("id"),
                "event_type": event.get("type") or "Unknown",
                "detail": event.get("detail"),
                "comments": event.get("comments"),
            },
            "ON CONFLICT DO NOTHING",
        )
        saved += 1
    return saved


# API-Football stat label -> our column name
TEAM_STAT_COLUMNS = {
    "Shots on Goal": "shots_on_goal",
    "Shots off Goal": "shots_off_goal",
    "Total Shots": "total_shots",
    "Blocked Shots": "blocked_shots",
    "Shots insidebox": "shots_insidebox",
    "Shots outsidebox": "shots_outsidebox",
    "Fouls": "fouls",
    "Corner Kicks": "corner_kicks",
    "Offsides": "offsides",
    "Ball Possession": "ball_possession",
    "Yellow Cards": "yellow_cards",
    "Red Cards": "red_cards",
    "Goalkeeper Saves": "goalkeeper_saves",
    "Total passes": "total_passes",
    "Passes accurate": "passes_accurate",
    "Passes %": "passes_percentage",
    "expected_goals": "expected_goals",
}


def _save_team_stats(cur, fixture_id: int, statistics: list[dict[str, Any]]) -> int:
    saved = 0
    for block in statistics:
        team = block.get("team", {}) or {}
        if team.get("id") is None:
            continue
        _ensure_team(cur, team)

        row: dict[str, Any] = {"fixture_id": fixture_id, "team_id": team["id"]}
        for item in block.get("statistics", []) or []:
            column = TEAM_STAT_COLUMNS.get(item.get("type"))
            if column is None:
                continue
            value = item.get("value")
            row[column] = _to_float(value) if column == "expected_goals" else _to_int(value)

        _insert(cur, "team_stat_snapshots", row, "ON CONFLICT DO NOTHING")
        saved += 1
    return saved


def _save_player_stats(cur, fixture_id: int, players: list[dict[str, Any]]) -> int:
    saved = 0
    for block in players:
        team = block.get("team", {}) or {}
        if team.get("id") is None:
            continue
        _ensure_team(cur, team)

        for entry in block.get("players", []) or []:
            info = entry.get("player", {}) or {}
            stats_list = entry.get("statistics", []) or []
            if info.get("id") is None or not stats_list:
                continue
            stats = stats_list[0]

            games = stats.get("games", {}) or {}
            if not games.get("minutes"):
                continue  # hasn't played yet -- unused sub, all nulls

            shots = stats.get("shots", {}) or {}
            goals = stats.get("goals", {}) or {}
            passes = stats.get("passes", {}) or {}
            tackles = stats.get("tackles", {}) or {}
            duels = stats.get("duels", {}) or {}
            dribbles = stats.get("dribbles", {}) or {}
            fouls = stats.get("fouls", {}) or {}
            cards = stats.get("cards", {}) or {}
            penalty = stats.get("penalty", {}) or {}

            _ensure_player(cur, info["id"], info.get("name"), info.get("photo"))

            _insert(
                cur,
                "player_stat_snapshots",
                {
                    "fixture_id": fixture_id,
                    "player_id": info["id"],
                    "team_id": team["id"],
                    "minutes_played": _to_int(games.get("minutes")),
                    "position": games.get("position"),
                    "rating": _to_float(games.get("rating")),
                    "is_captain": games.get("captain"),
                    "is_substitute": games.get("substitute"),
                    "shots_total": _to_int(shots.get("total")),
                    "shots_on": _to_int(shots.get("on")),
                    "goals_total": _to_int(goals.get("total")),
                    "goals_conceded": _to_int(goals.get("conceded")),
                    "assists": _to_int(goals.get("assists")),
                    "goalkeeper_saves": _to_int(goals.get("saves")),
                    "passes_total": _to_int(passes.get("total")),
                    "passes_key": _to_int(passes.get("key")),
                    "passes_accuracy": _to_int(passes.get("accuracy")),
                    "tackles_total": _to_int(tackles.get("total")),
                    "tackles_blocks": _to_int(tackles.get("blocks")),
                    "tackles_interceptions": _to_int(tackles.get("interceptions")),
                    "duels_total": _to_int(duels.get("total")),
                    "duels_won": _to_int(duels.get("won")),
                    "dribbles_attempts": _to_int(dribbles.get("attempts")),
                    "dribbles_success": _to_int(dribbles.get("success")),
                    "dribbles_past": _to_int(dribbles.get("past")),
                    "fouls_drawn": _to_int(fouls.get("drawn")),
                    "fouls_committed": _to_int(fouls.get("committed")),
                    "yellow_cards": _to_int(cards.get("yellow")),
                    "red_cards": _to_int(cards.get("red")),
                    "penalty_won": _to_int(penalty.get("won")),
                    "penalty_committed": _to_int(penalty.get("commited")),  # API typo is real
                    "penalty_scored": _to_int(penalty.get("scored")),
                    "penalty_missed": _to_int(penalty.get("missed")),
                    "penalty_saved": _to_int(penalty.get("saved")),
                },
                "ON CONFLICT DO NOTHING",
            )
            saved += 1
    return saved


# ------------------------------------------------------------------
# The one function the poller calls
# ------------------------------------------------------------------

def save_poll(fixture_id: int, snapshot: dict[str, Any], new_events: list[dict[str, Any]]) -> bool:
    """
    Saves everything from one poll in a single transaction.
    Returns True on success, False if the poll was rolled back.
    """
    conn = None
    try:
        conn = _get_connection()
        with conn.cursor() as cur:
            _save_match(cur, snapshot)
            n_events = _save_events(cur, fixture_id, new_events)
            n_teams = _save_team_stats(cur, fixture_id, snapshot.get("statistics", []) or [])
            n_players = _save_player_stats(cur, fixture_id, snapshot.get("players", []) or [])
        conn.commit()
        logger.info(
            f"[DB] saved fixture={fixture_id}: {n_events} events, "
            f"{n_teams} team-stat rows, {n_players} player-stat rows"
        )
        return True
    except Exception:
        if conn is not None:
            conn.rollback()
        logger.exception(f"[DB] write failed for fixture={fixture_id}; poll rolled back")
        return False