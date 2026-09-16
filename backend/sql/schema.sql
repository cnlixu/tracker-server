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
    last_wake_code SMALLINT
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

-- Future protocol versions may add nullable seq BIGINT and battery_mv INTEGER
-- columns. Add a composite uniqueness rule only after seq semantics are defined.
