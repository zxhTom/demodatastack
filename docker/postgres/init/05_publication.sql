-- 05_publication.sql: CDC 复制发布配置

-- 创建 Debezium 所需的 publication（逻辑复制发布）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'dbz_publication') THEN
        CREATE PUBLICATION dbz_publication FOR ALL TABLES;
        RAISE NOTICE 'Publication dbz_publication created';
    ELSE
        RAISE NOTICE 'Publication dbz_publication already exists';
    END IF;
END $$;

-- 确保 replicator 用户存在并有正确权限
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

-- 允许 postgres 用户进行逻辑复制（Debezium 使用 postgres 用户）
ALTER USER postgres WITH REPLICATION;
