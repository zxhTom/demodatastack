-- 04_timeseries.sql: 时序表与 KPI 表创建

-- 普通日志表（用于性能对比基准）
CREATE TABLE IF NOT EXISTS system_logs (
    id            BIGSERIAL    PRIMARY KEY,
    log_time      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    level         VARCHAR(10)  NOT NULL DEFAULT 'INFO',
    service       VARCHAR(50),
    user_id       INTEGER,
    action        VARCHAR(100),
    resource_type VARCHAR(50),
    resource_id   INTEGER,
    ip_address    INET,
    duration_ms   INTEGER,
    message       TEXT,
    metadata      JSONB
);

CREATE INDEX IF NOT EXISTS idx_system_logs_time    ON system_logs(log_time DESC);
CREATE INDEX IF NOT EXISTS idx_system_logs_level   ON system_logs(level);
CREATE INDEX IF NOT EXISTS idx_system_logs_service ON system_logs(service);

-- 时序日志表（TimescaleDB hypertable，用于性能对比）
CREATE TABLE IF NOT EXISTS system_logs_ts (
    log_time      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    level         VARCHAR(10)  NOT NULL DEFAULT 'INFO',
    service       VARCHAR(50),
    user_id       INTEGER,
    action        VARCHAR(100),
    resource_type VARCHAR(50),
    resource_id   INTEGER,
    ip_address    INET,
    duration_ms   INTEGER,
    message       TEXT,
    metadata      JSONB
);

-- 转换为 hypertable，按天分区
SELECT create_hypertable(
    'system_logs_ts', 'log_time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- TimescaleDB 自动压缩（仅社区版支持，忽略错误）
-- SELECT add_compression_policy('system_logs_ts', INTERVAL '7 days', if_not_exists => TRUE);

-- KPI 事件表（时序）
CREATE TABLE IF NOT EXISTS kpi_events (
    event_time    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    metric_name   VARCHAR(100) NOT NULL,
    metric_value  DECIMAL(15,4),
    dimension     JSONB,
    tags          JSONB
);

SELECT create_hypertable(
    'kpi_events', 'event_time',
    chunk_time_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);

-- KPI 连续聚合视图（每分钟统计）
CREATE MATERIALIZED VIEW IF NOT EXISTS kpi_summary_1min
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', event_time) AS bucket,
    metric_name,
    AVG(metric_value)  AS avg_value,
    MAX(metric_value)  AS max_value,
    MIN(metric_value)  AS min_value,
    COUNT(*)           AS event_count
FROM kpi_events
GROUP BY bucket, metric_name
WITH NO DATA;

-- 连续聚合刷新策略
SELECT add_continuous_aggregate_policy(
    'kpi_summary_1min',
    start_offset      => INTERVAL '1 hour',
    end_offset        => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists     => TRUE
);

-- 插入初始 KPI 基准数据（模拟历史数据）
INSERT INTO kpi_events (event_time, metric_name, metric_value, dimension, tags)
SELECT
    NOW() - (generate_series * INTERVAL '1 minute'),
    metric,
    (random() * 100)::DECIMAL(15,4),
    jsonb_build_object('source', 'init'),
    jsonb_build_object('env', 'production')
FROM generate_series(1, 60) AS generate_series,
     (VALUES ('student_count'),('enrollment_count'),('grade_count'),('active_users')) AS m(metric);
