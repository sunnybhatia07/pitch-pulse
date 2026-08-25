"""
One-off script to sanity-check the API-Football connection.

Does NOT loop. Costs at most 2 requests:
  1. GET /fixtures?live=all   -- list live matches, let you pick one
  2. GET /fixtures?id={id}    -- combined snapshot: events, statistics,
                                  lineups, and players all in one response

Reuses select_match() from ingestion.poller so match selection behaves
identically here and in the real poll loop -- one place to maintain,
not two copies of the same "list and pick" logic.

Run this any time you want to eyeball raw payload shapes for a specific
match without starting a full poll loop.
"""

import json

from ingestion.api_client import ApiFootballClient, QuotaExceededError
from ingestion.poller import select_match

client = ApiFootballClient()

print("Fetching live fixtures...")
try:
    match = select_match(client)
except QuotaExceededError as e:
    print(f"Quota already exhausted: {e}")
    raise SystemExit(1)

print(f"\nQuota after fixture list call: {client.last_rate_limit}")

if match is None:
    print("No live matches right now -- nothing more to test. "
          "Try again during an active match.")
    raise SystemExit(0)

fixture_id = match["fixture"]["id"]

print("\nFetching combined snapshot for this fixture...")
snapshot = client.get_fixture_snapshot(fixture_id)
print(f"Quota after snapshot call: {client.last_rate_limit}")

if snapshot is None:
    print("No snapshot data returned -- fixture ID may be invalid.")
    raise SystemExit(0)

status = snapshot.get("fixture", {}).get("status", {}).get("short")
events = snapshot.get("events", []) or []
statistics = snapshot.get("statistics", []) or []
lineups = snapshot.get("lineups", []) or []
players = snapshot.get("players", []) or []

print(f"\nStatus: {status}")
print(f"Events: {len(events)} | Statistics entries: {len(statistics)} | "
      f"Lineups: {len(lineups)} | Players entries: {len(players)}")

print("\n--- Raw event payload (first event, if any) ---")
if events:
    print(json.dumps(events[0], indent=2))
else:
    print("No events yet in this match (early in the game, or a slow one).")

print("\n--- Raw statistics payload (first team, if any) ---")
if statistics:
    print(json.dumps(statistics[0], indent=2))
else:
    print("No statistics available yet for this match/competition.")

print("\n--- Raw players payload (first team, first player, if any) ---")
if players and players[0].get("players"):
    print(json.dumps(players[0]["players"][0], indent=2))
else:
    print("No player statistics available yet for this match/competition.")