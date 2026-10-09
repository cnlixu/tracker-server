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

-- Registration is completed before the first location report, so terminal
-- credentials are stored separately from the latest-location snapshot. The
-- plaintext authentication code is returned only in the 0x8100 response.
CREATE TABLE IF NOT EXISTS jt808_terminals (
    terminal_phone VARCHAR(20) PRIMARY KEY,
    imei VARCHAR(15) UNIQUE,
    auth_code_hash CHAR(64) NOT NULL,
    province_id INTEGER NOT NULL,
    city_id INTEGER NOT NULL,
    manufacturer_id VARCHAR(11) NOT NULL,
    terminal_model VARCHAR(30) NOT NULL,
    terminal_id VARCHAR(30) NOT NULL,
    plate_color SMALLINT NOT NULL,
    plate_number VARCHAR(32) NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_authenticated_at TIMESTAMPTZ,
    CHECK (terminal_phone ~ '^[0-9]{1,20}$'),
    CHECK (imei IS NULL OR imei ~ '^[0-9]{15}$'),
    CHECK (auth_code_hash ~ '^[0-9a-f]{64}$')
);

-- EPTR is the project-specific registration body carried by standard 0x0100.
-- Legacy vehicle-registration columns remain for migration compatibility only.
ALTER TABLE jt808_terminals ADD COLUMN IF NOT EXISTS profile_magic VARCHAR(4) NOT NULL DEFAULT 'EPTR';
ALTER TABLE jt808_terminals ADD COLUMN IF NOT EXISTS profile_version SMALLINT NOT NULL DEFAULT 1;
ALTER TABLE jt808_terminals ADD COLUMN IF NOT EXISTS terminal_type SMALLINT NOT NULL DEFAULT 4;
ALTER TABLE jt808_terminals ADD COLUMN IF NOT EXISTS capabilities BIGINT NOT NULL DEFAULT 0;
ALTER TABLE jt808_terminals ADD COLUMN IF NOT EXISTS report_interval_sec INTEGER NOT NULL DEFAULT 900;
ALTER TABLE jt808_terminals ADD COLUMN IF NOT EXISTS service_point_id VARCHAR(32) NOT NULL DEFAULT '';
ALTER TABLE jt808_terminals ADD COLUMN IF NOT EXISTS asset_id VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE jt808_terminals ADD COLUMN IF NOT EXISTS terminal_sn VARCHAR(32) NOT NULL DEFAULT '';
ALTER TABLE jt808_terminals ADD COLUMN IF NOT EXISTS iccid VARCHAR(22) NOT NULL DEFAULT '';
ALTER TABLE jt808_terminals ADD COLUMN IF NOT EXISTS hardware_version VARCHAR(16) NOT NULL DEFAULT '';
ALTER TABLE jt808_terminals ADD COLUMN IF NOT EXISTS software_version VARCHAR(32) NOT NULL DEFAULT '';
ALTER TABLE jt808_terminals ADD COLUMN IF NOT EXISTS organization_code VARCHAR(32) NOT NULL DEFAULT '';
ALTER TABLE jt808_terminals ADD COLUMN IF NOT EXISTS production_batch VARCHAR(32) NOT NULL DEFAULT '';
ALTER TABLE jt808_terminals ADD COLUMN IF NOT EXISTS profile_revision BIGINT NOT NULL DEFAULT 0;

-- Desired/applied copies make parameter delivery durable across PSM+ sessions.
-- A configuration is pending until the terminal returns 0x0001 success for
-- the exact 0x8103 platform serial and revision.
CREATE TABLE IF NOT EXISTS jt808_terminal_configs (
    terminal_phone VARCHAR(20) PRIMARY KEY
        REFERENCES jt808_terminals(terminal_phone) ON DELETE CASCADE,
    desired_revision BIGINT NOT NULL DEFAULT 0,
    applied_revision BIGINT NOT NULL DEFAULT 0,
    desired_parameters JSONB NOT NULL DEFAULT '{}'::JSONB,
    applied_parameters JSONB NOT NULL DEFAULT '{}'::JSONB,
    status VARCHAR(16) NOT NULL DEFAULT 'applied',
    last_error VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_sent_at TIMESTAMPTZ,
    last_applied_at TIMESTAMPTZ,
    CHECK (status IN ('pending', 'sent', 'applied', 'failed')),
    CHECK (desired_revision >= applied_revision)
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
