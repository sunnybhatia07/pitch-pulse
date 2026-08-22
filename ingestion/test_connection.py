"""
One-off script to sanity-check the API-Football connection.

Does NOT loop. Costs at most 2 requests:
  1. GET /fixtures?live=all   -- list live matches, let you pick one
  2. GET /fixtures/events     -- see the real shape of one event payload

Reuses select_match() from ingestion.poller so match selection behaves
identically here and in the real poll loop -- one place to maintain,
not two copies of the same "list and pick" logic.

Run this any time you want to eyeball raw event JSON for a specific
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

print("\nFetching events for this fixture...")
events = client.get_fixture_events(fixture_id)
print(f"Quota after events call: {client.last_rate_limit}")
print(f"Events returned: {len(events)}")

print("\n--- Raw event payload (first event, if any) ---")
if events:
    print(json.dumps(events[0], indent=2))
else:
    print("No events yet in this match (early in the game, or a slow one).")