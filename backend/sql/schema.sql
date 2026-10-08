CREATE TABLE IF NOT EXISTS devices (
    imei VARCHAR(20) PRIMARY KEY,
    name VARCHAR(64),
    first_seen TIMESTAMPTZ NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL,
    last_gps_time TIMESTAMPTZ,
    last_valid BOOLEAN,
    last_lat DOUBLE PRECISION,
    last_lon DOUBLE PRECISION,
    last_altitude REAL,
    last_speed REAL,
    last_course REAL,
    last_satellites SMALLINT,
    last_hdop REAL,
    last_csq SMALLINT,
    last_wake_code SMALLINT,
    last_battery_mv INTEGER,
    last_battery_percent SMALLINT
);

CREATE TABLE IF NOT EXISTS track_points (
    id BIGSERIAL PRIMARY KEY,
    imei VARCHAR(20) NOT NULL,
    gps_time TIMESTAMPTZ NOT NULL,
    server_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid BOOLEAN NOT NULL,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    altitude REAL,
    speed REAL,
    course REAL,
    satellites SMALLINT,
    hdop REAL,
    csq SMALLINT,
    wake_code SMALLINT,
    raw_data TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_track_points_imei_gps_time
    ON track_points (imei, gps_time);

-- JT/T 808 custom extension 0xE0 carries a per-record identity, so the server
-- remains idempotent when a device re-sends after a lost acknowledgement.
-- Speed remains stored in knots for compatibility with existing application data.
ALTER TABLE track_points ADD COLUMN IF NOT EXISTS generation_id BIGINT;
ALTER TABLE track_points ADD COLUMN IF NOT EXISTS record_seq BIGINT;
ALTER TABLE track_points ADD COLUMN IF NOT EXISTS batch_id BIGINT;
ALTER TABLE track_points ADD COLUMN IF NOT EXISTS battery_mv INTEGER;
ALTER TABLE track_points ADD COLUMN IF NOT EXISTS battery_percent SMALLINT;
ALTER TABLE track_points ADD COLUMN IF NOT EXISTS time_valid BOOLEAN;

-- The device snapshot also exposes the latest battery voltage.
ALTER TABLE devices ADD COLUMN IF NOT EXISTS last_battery_mv INTEGER;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS last_battery_percent SMALLINT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_track_points_record_identity
    ON track_points (imei, generation_id, record_seq);
