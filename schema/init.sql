-- 1. Create the Raw Telemetry Firehose Table
CREATE TABLE match_events (
    id SERIAL PRIMARY KEY,
    match_id VARCHAR(50),
    event_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    team_name VARCHAR(100),
    player_name VARCHAR(100),
    event_type VARCHAR(50),
    location_x FLOAT,
    location_y FLOAT
);

-- 2. Create the Clean CDC Target Table
CREATE TABLE tactical_insights_cdc (
    id SERIAL PRIMARY KEY,
    match_id VARCHAR(50),
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    team_name VARCHAR(100),
    possession_percentage NUMERIC(5,2),
    avg_pitch_height NUMERIC(5,2),
    fatigue_weak_link VARCHAR(100),
    weak_link_turnovers INT
);

-- 3. The SQL Brain: Compute insights using CTEs
CREATE OR REPLACE FUNCTION compute_tactical_insight()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO tactical_insights_cdc (
        match_id,
        team_name,
        possession_percentage,
        avg_pitch_height,
        fatigue_weak_link,
        weak_link_turnovers
    )
    WITH team_stats AS (
        SELECT 
            team_name,
            -- Calculate Possession % up to this exact event
            COUNT(*) * 100.0 / NULLIF((SELECT COUNT(*) FROM match_events WHERE match_id = NEW.match_id), 0) AS possession_percentage,
            -- Calculate Vertical Velocity / Pitch Height
            AVG(location_x) AS avg_pitch_height
        FROM match_events
        WHERE match_id = NEW.match_id AND team_name = NEW.team_name
        GROUP BY team_name
    ),
    player_fatigue AS (
        SELECT 
            player_name,
            COUNT(*) AS turnovers
        FROM match_events
        WHERE match_id = NEW.match_id AND event_type = 'Turnover'
        GROUP BY player_name
        ORDER BY turnovers DESC
        LIMIT 1
    )
    SELECT 
        NEW.match_id,
        t.team_name,
        t.possession_percentage,
        t.avg_pitch_height,
        COALESCE(p.player_name, 'None'),
        COALESCE(p.turnovers, 0)
    FROM team_stats t
    LEFT JOIN player_fatigue p ON true;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 4. Attach the Trigger to fire on every new event
CREATE TRIGGER trigger_tactical_insight
AFTER INSERT ON match_events
FOR EACH ROW
EXECUTE FUNCTION compute_tactical_insight();