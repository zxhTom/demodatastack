-- 05_publication.sql: CDC 逻辑复制发布配置（Redis Stream 方案）

-- 清理旧 Debezium 方案遗留的 publication（FOR ALL TABLES 会阻止 create_hypertable）
DROP PUBLICATION IF EXISTS dbz_publication;

-- 只发布需要 CDC 的业务表，不含 kpi_events 等超表
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'cdc_pub') THEN
        CREATE PUBLICATION cdc_pub FOR TABLE
            departments, teachers, students, courses, semesters,
            enrollments, grades, attendance, course_schedules;
        RAISE NOTICE 'Publication cdc_pub created';
    ELSE
        RAISE NOTICE 'Publication cdc_pub already exists';
    END IF;
END $$;

-- REPLICA IDENTITY FULL：UPDATE/DELETE 的 WAL 携带完整旧行，
-- 消费端才能计算 changed_fields
ALTER TABLE departments      REPLICA IDENTITY FULL;
ALTER TABLE teachers         REPLICA IDENTITY FULL;
ALTER TABLE students         REPLICA IDENTITY FULL;
ALTER TABLE courses          REPLICA IDENTITY FULL;
ALTER TABLE semesters        REPLICA IDENTITY FULL;
ALTER TABLE enrollments      REPLICA IDENTITY FULL;
ALTER TABLE grades           REPLICA IDENTITY FULL;
ALTER TABLE attendance       REPLICA IDENTITY FULL;
ALTER TABLE course_schedules REPLICA IDENTITY FULL;

-- 确保 replicator 用户存在并有正确权限（流复制副本用）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'replicator') THEN
        CREATE USER replicator WITH REPLICATION ENCRYPTED PASSWORD 'replicator123';
        RAISE NOTICE 'User replicator created';
    ELSE
        RAISE NOTICE 'User replicator already exists';
    END IF;
END $$;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO replicator;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO replicator;

-- cdc-collector 使用 postgres 用户做逻辑复制
ALTER USER postgres WITH REPLICATION;
