"""
Classifies API-Football fixture status short codes into three groups
so the poll loop can decide how to behave:

  ACTIVE   -> match clock is running, poll events at normal cadence
  PAUSED   -> no new events possible right now, back off hard
  FINISHED -> match is over, stop polling entirely

This file holds ONLY classification data + lookup helpers — no
polling logic, no HTTP calls. Keeping the status table here (rather
than scattering string comparisons through the poll loop) makes it
a single place to update if API-Football adds/changes statuses.
"""

ACTIVE_STATUSES = {
    "1H",    # First half in play
    "2H",    # Second half in play
    "ET",    # Extra time in play
    "P",     # Penalty shootout in progress
    "LIVE",  # In progress, half/elapsed unavailable (rare)
}

PAUSED_STATUSES = {
    "HT",    # Halftime
    "BT",    # Break time (during extra time)
    "SUSP",  # Suspended by referee, may resume same day
    "INT",   # Interrupted, should resume shortly
}

FINISHED_STATUSES = {
    "FT",    # Finished, regular time
    "AET",   # Finished after extra time
    "PEN",   # Finished after penalty shootout
    "CANC",  # Cancelled
    "ABD",   # Abandoned
    "PST",   # Postponed
    "AWD",   # Technical loss / not played
    "WO",    # Walkover / not played
}


def is_active(status: str | None) -> bool:
    return status in ACTIVE_STATUSES


def is_paused(status: str | None) -> bool:
    return status in PAUSED_STATUSES


def is_finished(status: str | None) -> bool:
    return status in FINISHED_STATUSES