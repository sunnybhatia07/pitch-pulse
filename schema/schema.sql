-- Dimensions: teams, players, matches
-- Facts:      match_events, team_stat_snapshots, player_stat_snapshots
--
-- ================================
-- DIMENSIONS
-- ============================================================

CREATE TABLE teams (
    team_id     BIGINT PRIMARY KEY,          -- API-Football team id
    name        TEXT NOT NULL,
    country     TEXT,
    logo_url    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE players (
    player_id    BIGINT PRIMARY KEY,         -- API-Football player id
    name         TEXT NOT NULL,
    nationality  TEXT,
    birth_date   DATE,
    position     TEXT,                       -- general position, not match-specific
    photo_url    TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE matches (
    fixture_id      BIGINT PRIMARY KEY,      -- API-Football fixture id
    league_id       BIGINT,
    league_name     TEXT,
    country         TEXT,
    season          INT,
    round           TEXT,
    home_team_id    BIGINT NOT NULL REFERENCES teams(team_id),
    away_team_id    BIGINT NOT NULL REFERENCES teams(team_id),
    kickoff_utc     TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'SCHEDULED',  -- mirrors match_status.py: ACTIVE / PAUSED / FINISHED / SCHEDULED
    elapsed_minute  INT,
    home_score      INT,
    away_score      INT,
    venue           TEXT,
    referee         TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_matches_status ON matches(status);

-- ============================================================
-- FACTS
-- ============================================================

-- Event-grain, append-only. Mirrors EventBuffer's composite dedup key.
CREATE TABLE match_events (
    id            BIGSERIAL PRIMARY KEY,
    fixture_id    BIGINT NOT NULL REFERENCES matches(fixture_id),
    minute        INT NOT NULL,
    extra_minute  INT,
    team_id       BIGINT NOT NULL REFERENCES teams(team_id),
    player_id     BIGINT REFERENCES players(player_id),   -- nullable: some VAR events have no player
    assist_id     BIGINT REFERENCES players(player_id),
    event_type    TEXT NOT NULL,             -- Goal / Card / subst / Var
    detail        TEXT,                      -- Normal Goal / Yellow Card / etc.
    comments      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (fixture_id, minute, team_id, player_id, event_type, detail)
);
-- NOTE: Postgres treats NULL as distinct in UNIQUE constraints, so two rows
-- with the same fixture/minute/team/type/detail but both NULL player_id
-- would NOT collide at the DB level. EventBuffer's in-memory dedup already
-- guards against this upstream; flagging so it's a known, accepted gap.

CREATE INDEX idx_match_events_fixture_minute ON match_events(fixture_id, minute);

-- Periodic snapshot fact: one row per (match, team, poll).
CREATE TABLE team_stat_snapshots (
    id                  BIGSERIAL PRIMARY KEY,
    fixture_id          BIGINT NOT NULL REFERENCES matches(fixture_id),
    team_id             BIGINT NOT NULL REFERENCES teams(team_id),
    captured_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    shots_on_goal       INT,
    shots_off_goal      INT,
    total_shots         INT,
    blocked_shots       INT,
    shots_insidebox     INT,
    shots_outsidebox    INT,
    fouls               INT,
    corner_kicks        INT,
    offsides            INT,
    ball_possession     INT,               -- integer percent, "55%" stripped on ingest
    yellow_cards        INT,
    red_cards           INT,
    goalkeeper_saves    INT,               -- team-level total
    total_passes        INT,
    passes_accurate     INT,
    passes_percentage   INT,               -- integer percent
    expected_goals      NUMERIC(4,2),      -- nullable, not every league returns xG
    UNIQUE (fixture_id, team_id, captured_at)
);

-- Periodic snapshot fact: one row per (match, player, poll).
CREATE TABLE player_stat_snapshots (
    id                       BIGSERIAL PRIMARY KEY,
    fixture_id               BIGINT NOT NULL REFERENCES matches(fixture_id),
    player_id                BIGINT NOT NULL REFERENCES players(player_id),
    team_id                  BIGINT NOT NULL REFERENCES teams(team_id),  -- side played for THIS match
    captured_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    minutes_played           INT,          -- fatigue calc input
    position                 TEXT,
    rating                   NUMERIC(3,1),
    is_captain               BOOLEAN,
    is_substitute            BOOLEAN,
    shots_total              INT,
    shots_on                 INT,
    goals_total              INT,
    goals_conceded           INT,       -- GK-relevant
    assists                  INT,
    goalkeeper_saves         INT,       -- lives under goals.saves in API-Football, not a top-level field
    passes_total             INT,
    passes_key               INT,
    passes_accuracy          INT,
    tackles_total            INT,
    tackles_blocks           INT,
    tackles_interceptions    INT,
    duels_total              INT,
    duels_won                INT,
    dribbles_attempts        INT,
    dribbles_success         INT,
    dribbles_past            INT,
    fouls_drawn              INT,
    fouls_committed          INT,
    yellow_cards             INT,
    red_cards                INT,
    penalty_won              INT,
    penalty_committed        INT,  -- API-Football source field is misspelled "commited" -- verify against a live payload before wiring the writer
    penalty_scored           INT,
    penalty_missed           INT,
    penalty_saved            INT,  -- GK-relevant
    UNIQUE (fixture_id, player_id, captured_at)
);