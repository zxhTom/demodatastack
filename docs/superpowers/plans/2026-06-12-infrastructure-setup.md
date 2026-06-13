# Infrastructure Setup (Requirement #1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使用 docker-compose 搭建 PostgreSQL（主从复制 + TimescaleDB）、Kafka、Redis 基础设施，并初始化教务系统数据库表及数据。

**Architecture:** 使用单个 docker-compose.yml 管理所有服务；PostgreSQL 选用 timescale/timescaledb:latest-pg16 镜像同时获得时序功能和流式复制；Kafka 采用 Bitnami KRaft 模式（无 ZooKeeper）简化部署。

**Tech Stack:** Docker Compose 1.29.2 · TimescaleDB (pg16) · Kafka (Bitnami KRaft) · Redis 7

---

## File Map

| 路径 | 说明 |
|---|---|
| `docker-compose.yml` | 所有服务定义 |
| `docker/postgres/primary/postgresql.conf` | 主库 WAL 配置 |
| `docker/postgres/primary/pg_hba.conf` | 主库访问控制（含复制用户） |
| `docker/postgres/replica/init-replica.sh` | 副本启动时执行的基础备份脚本 |
| `docker/postgres/init/01-schema.sql` | 教务系统建表 DDL |
| `docker/postgres/init/02-data.sql` | 初始化数据 (DML) |
| `docker/postgres/init/03-timescale.sql` | 启用 TimescaleDB 扩展 |
| `scripts/verify.sh` | 验证脚本：检查所有服务健康状态 |

---

### Task 1: 创建项目目录结构

**Files:**
- Create: `docker/postgres/primary/`
- Create: `docker/postgres/replica/`
- Create: `docker/postgres/init/`
- Create: `scripts/`

- [ ] **Step 1: 创建目录**

```bash
cd /home/zxhtom/temp/github/claudecode
mkdir -p docker/postgres/primary
mkdir -p docker/postgres/replica
mkdir -p docker/postgres/init
mkdir -p scripts
```

- [ ] **Step 2: 验证目录存在**

```bash
ls -la docker/postgres/ scripts/
```
Expected: primary/ replica/ init/ 三个目录 + scripts/

---

### Task 2: PostgreSQL 主库配置文件

**Files:**
- Create: `docker/postgres/primary/postgresql.conf`
- Create: `docker/postgres/primary/pg_hba.conf`

- [ ] **Step 1: 写 postgresql.conf**

```conf
# /docker/postgres/primary/postgresql.conf
listen_addresses = '*'
wal_level = replica
max_wal_senders = 10
wal_keep_size = 256MB
hot_standby = on
max_connections = 200
shared_preload_libraries = 'timescaledb'
timescaledb.telemetry_level = off
```

- [ ] **Step 2: 写 pg_hba.conf**

```conf
# /docker/postgres/primary/pg_hba.conf
# TYPE  DATABASE        USER            ADDRESS                 METHOD
local   all             all                                     trust
host    all             all             0.0.0.0/0               md5
host    replication     replicator      0.0.0.0/0               md5
```

---

### Task 3: 副本初始化脚本

**Files:**
- Create: `docker/postgres/replica/init-replica.sh`

- [ ] **Step 1: 写 init-replica.sh**

```bash
#!/bin/bash
# docker/postgres/replica/init-replica.sh
set -e

# 等待主库就绪
until pg_isready -h postgres-primary -p 5432 -U postgres; do
  echo "Waiting for primary..."
  sleep 2
done

# 如果数据目录已存在则跳过基础备份
if [ -z "$(ls -A $PGDATA)" ]; then
  echo "Running pg_basebackup from primary..."
  PGPASSWORD=replicator_pass pg_basebackup \
    -h postgres-primary \
    -D $PGDATA \
    -U replicator \
    -v -P --wal-method=stream
  
  # 创建 standby.signal
  touch $PGDATA/standby.signal
  
  # 写入 primary_conninfo
  cat >> $PGDATA/postgresql.auto.conf <<EOF
primary_conninfo = 'host=postgres-primary port=5432 user=replicator password=replicator_pass application_name=replica1'
hot_standby = on
EOF
fi

exec docker-entrypoint.sh postgres
```

- [ ] **Step 2: 设置可执行权限**

```bash
chmod +x docker/postgres/replica/init-replica.sh
```

---

### Task 4: 教务系统数据库 DDL

**Files:**
- Create: `docker/postgres/init/01-schema.sql`

- [ ] **Step 1: 写建表 SQL**

```sql
-- docker/postgres/init/01-schema.sql

-- 启用 UUID 扩展
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- 部门表
CREATE TABLE IF NOT EXISTS departments (
    id          SERIAL PRIMARY KEY,
    code        VARCHAR(20) UNIQUE NOT NULL,
    name        VARCHAR(100) NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 教师表
CREATE TABLE IF NOT EXISTS teachers (
    id            SERIAL PRIMARY KEY,
    employee_no   VARCHAR(20) UNIQUE NOT NULL,
    name          VARCHAR(50) NOT NULL,
    email         VARCHAR(100) UNIQUE NOT NULL,
    phone         VARCHAR(20),
    department_id INT REFERENCES departments(id),
    title         VARCHAR(50),
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- 学生表
CREATE TABLE IF NOT EXISTS students (
    id            SERIAL PRIMARY KEY,
    student_no    VARCHAR(20) UNIQUE NOT NULL,
    name          VARCHAR(50) NOT NULL,
    email         VARCHAR(100) UNIQUE NOT NULL,
    phone         VARCHAR(20),
    gender        VARCHAR(10) CHECK (gender IN ('male','female','other')),
    birth_date    DATE,
    department_id INT REFERENCES departments(id),
    enrolled_year INT NOT NULL,
    status        VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active','graduated','suspended')),
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- 课程表
CREATE TABLE IF NOT EXISTS courses (
    id            SERIAL PRIMARY KEY,
    code          VARCHAR(20) UNIQUE NOT NULL,
    name          VARCHAR(100) NOT NULL,
    credits       NUMERIC(3,1) NOT NULL,
    department_id INT REFERENCES departments(id),
    description   TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- 班级表（课程的一次开班实例）
CREATE TABLE IF NOT EXISTS classes (
    id          SERIAL PRIMARY KEY,
    course_id   INT REFERENCES courses(id) NOT NULL,
    teacher_id  INT REFERENCES teachers(id),
    semester    VARCHAR(20) NOT NULL,  -- e.g. '2024-1'
    year        INT NOT NULL,
    room        VARCHAR(50),
    max_students INT DEFAULT 50,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 选课表（学生与班级的关联）
CREATE TABLE IF NOT EXISTS enrollments (
    id         SERIAL PRIMARY KEY,
    student_id INT REFERENCES students(id) NOT NULL,
    class_id   INT REFERENCES classes(id) NOT NULL,
    enrolled_at TIMESTAMPTZ DEFAULT NOW(),
    status     VARCHAR(20) DEFAULT 'enrolled' CHECK (status IN ('enrolled','dropped','completed')),
    UNIQUE (student_id, class_id)
);

-- 成绩表
CREATE TABLE IF NOT EXISTS grades (
    id            SERIAL PRIMARY KEY,
    enrollment_id INT REFERENCES enrollments(id) NOT NULL UNIQUE,
    score         NUMERIC(5,2) CHECK (score >= 0 AND score <= 100),
    grade_letter  VARCHAR(2),
    graded_at     TIMESTAMPTZ DEFAULT NOW(),
    remarks       TEXT
);

-- 考勤表
CREATE TABLE IF NOT EXISTS attendance (
    id         SERIAL PRIMARY KEY,
    student_id INT REFERENCES students(id) NOT NULL,
    class_id   INT REFERENCES classes(id) NOT NULL,
    date       DATE NOT NULL,
    status     VARCHAR(20) DEFAULT 'present' CHECK (status IN ('present','absent','late','excused')),
    recorded_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (student_id, class_id, date)
);

-- 系统用户表（登录用）
CREATE TABLE IF NOT EXISTS users (
    id           SERIAL PRIMARY KEY,
    username     VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role         VARCHAR(20) NOT NULL CHECK (role IN ('admin','teacher','student')),
    ref_id       INT,            -- 指向 teachers.id 或 students.id
    is_active    BOOLEAN DEFAULT TRUE,
    last_login   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_enrollments_student ON enrollments(student_id);
CREATE INDEX IF NOT EXISTS idx_enrollments_class ON enrollments(class_id);
CREATE INDEX IF NOT EXISTS idx_attendance_student_date ON attendance(student_id, date);
CREATE INDEX IF NOT EXISTS idx_grades_enrollment ON grades(enrollment_id);
```

---

### Task 5: 初始数据 DML

**Files:**
- Create: `docker/postgres/init/02-data.sql`

- [ ] **Step 1: 写初始数据 SQL**

```sql
-- docker/postgres/init/02-data.sql

-- 部门
INSERT INTO departments (code, name) VALUES
  ('CS',   '计算机科学与技术'),
  ('MATH', '数学与应用数学'),
  ('ENG',  '英语语言文学'),
  ('PHY',  '物理学'),
  ('MGMT', '工商管理')
ON CONFLICT (code) DO NOTHING;

-- 教师 (password_hash 是 'password123' 的 bcrypt 哈希，由后端程序生成；这里用占位明文，后端启动时会统一处理)
INSERT INTO teachers (employee_no, name, email, phone, department_id, title) VALUES
  ('T001', '张伟',   'zhangwei@edu.cn',   '13800000001', 1, '副教授'),
  ('T002', '李娜',   'lina@edu.cn',       '13800000002', 2, '教授'),
  ('T003', '王强',   'wangqiang@edu.cn',  '13800000003', 3, '讲师'),
  ('T004', '刘洋',   'liuyang@edu.cn',    '13800000004', 4, '副教授'),
  ('T005', '陈静',   'chenjing@edu.cn',   '13800000005', 5, '教授')
ON CONFLICT (employee_no) DO NOTHING;

-- 学生
INSERT INTO students (student_no, name, email, gender, birth_date, department_id, enrolled_year) VALUES
  ('S20240001', '赵磊',   'zhaolei@stu.edu.cn',   'male',   '2003-03-15', 1, 2024),
  ('S20240002', '孙芳',   'sunfang@stu.edu.cn',   'female', '2003-07-22', 1, 2024),
  ('S20240003', '周浩',   'zhouhao@stu.edu.cn',   'male',   '2002-11-01', 2, 2024),
  ('S20240004', '吴雪',   'wuxue@stu.edu.cn',     'female', '2003-05-30', 3, 2024),
  ('S20240005', '郑凯',   'zhengkai@stu.edu.cn',  'male',   '2002-09-18', 4, 2024),
  ('S20230001', '冯丽',   'fengli@stu.edu.cn',    'female', '2002-01-08', 1, 2023),
  ('S20230002', '褚伟',   'chuwei@stu.edu.cn',    'male',   '2001-12-25', 5, 2023),
  ('S20230003', '卫佳',   'weijia@stu.edu.cn',    'female', '2002-04-16', 2, 2023)
ON CONFLICT (student_no) DO NOTHING;

-- 课程
INSERT INTO courses (code, name, credits, department_id) VALUES
  ('CS101',   '程序设计基础',     3.0, 1),
  ('CS201',   '数据结构',         3.0, 1),
  ('CS301',   '数据库原理',       3.0, 1),
  ('MATH101', '高等数学',         4.0, 2),
  ('ENG101',  '大学英语',         3.0, 3),
  ('PHY101',  '大学物理',         3.0, 4),
  ('MGMT101', '管理学原理',       2.0, 5)
ON CONFLICT (code) DO NOTHING;

-- 班级（2024年第一学期）
INSERT INTO classes (course_id, teacher_id, semester, year, room) VALUES
  (1, 1, '2024-1', 2024, 'A101'),
  (2, 1, '2024-1', 2024, 'A102'),
  (3, 1, '2024-1', 2024, 'A103'),
  (4, 2, '2024-1', 2024, 'B201'),
  (5, 3, '2024-1', 2024, 'C301'),
  (6, 4, '2024-1', 2024, 'D101'),
  (7, 5, '2024-1', 2024, 'E201');

-- 选课
INSERT INTO enrollments (student_id, class_id, status) VALUES
  (1, 1, 'enrolled'), (1, 2, 'enrolled'), (1, 4, 'enrolled'),
  (2, 1, 'enrolled'), (2, 5, 'enrolled'),
  (3, 4, 'enrolled'), (3, 6, 'enrolled'),
  (4, 5, 'enrolled'), (4, 7, 'enrolled'),
  (5, 6, 'enrolled'), (5, 4, 'enrolled'),
  (6, 3, 'enrolled'), (6, 4, 'enrolled'),
  (7, 7, 'enrolled'), (7, 5, 'enrolled'),
  (8, 4, 'enrolled'), (8, 3, 'enrolled')
ON CONFLICT (student_id, class_id) DO NOTHING;

-- 成绩（已完成的部分）
INSERT INTO grades (enrollment_id, score, grade_letter) VALUES
  (1, 88.5, 'B+'), (2, 92.0, 'A'),
  (4, 76.0, 'C+'), (6, 85.0, 'B'),
  (11, 91.0, 'A')
ON CONFLICT (enrollment_id) DO NOTHING;

-- 考勤（最近3天样本）
INSERT INTO attendance (student_id, class_id, date, status) VALUES
  (1, 1, CURRENT_DATE - 2, 'present'),
  (1, 1, CURRENT_DATE - 1, 'present'),
  (2, 1, CURRENT_DATE - 2, 'absent'),
  (2, 1, CURRENT_DATE - 1, 'late'),
  (3, 4, CURRENT_DATE - 2, 'present'),
  (4, 5, CURRENT_DATE - 1, 'present')
ON CONFLICT (student_id, class_id, date) DO NOTHING;

-- 系统用户（密码统一 admin123，bcrypt hash）
-- $2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/lewKyDAkLEf/WlnWi 对应 admin123
INSERT INTO users (username, password_hash, role, ref_id) VALUES
  ('admin',    '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/lewKyDAkLEf/WlnWi', 'admin',   NULL),
  ('t_zhangwei','$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/lewKyDAkLEf/WlnWi', 'teacher', 1),
  ('t_lina',    '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/lewKyDAkLEf/WlnWi', 'teacher', 2),
  ('s_zhaolei', '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/lewKyDAkLEf/WlnWi', 'student', 1),
  ('s_sunfang', '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/lewKyDAkLEf/WlnWi', 'student', 2)
ON CONFLICT (username) DO NOTHING;
```

---

### Task 6: docker-compose.yml

**Files:**
- Create: `docker-compose.yml`

- [ ] **Step 1: 写 docker-compose.yml**

```yaml
version: '3.8'

networks:
  edunet:
    driver: bridge

volumes:
  pg_primary_data:
  pg_replica_data:
  kafka_data:
  redis_data:

services:

  # ── PostgreSQL 主库（TimescaleDB）──────────────────────────────
  postgres-primary:
    image: timescale/timescaledb:latest-pg16
    container_name: postgres-primary
    restart: unless-stopped
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres123
      POSTGRES_DB: edudb
      PGDATA: /var/lib/postgresql/data
    volumes:
      - pg_primary_data:/var/lib/postgresql/data
      - ./docker/postgres/primary/postgresql.conf:/etc/postgresql/postgresql.conf
      - ./docker/postgres/primary/pg_hba.conf:/etc/postgresql/pg_hba.conf
      - ./docker/postgres/init:/docker-entrypoint-initdb.d
    command: postgres -c config_file=/etc/postgresql/postgresql.conf -c hba_file=/etc/postgresql/pg_hba.conf
    ports:
      - "5432:5432"
    networks:
      - edunet
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d edudb"]
      interval: 10s
      timeout: 5s
      retries: 10

  # ── PostgreSQL 副库（流式复制）────────────────────────────────
  postgres-replica:
    image: timescale/timescaledb:latest-pg16
    container_name: postgres-replica
    restart: unless-stopped
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres123
      PGDATA: /var/lib/postgresql/data
    volumes:
      - pg_replica_data:/var/lib/postgresql/data
      - ./docker/postgres/replica/init-replica.sh:/docker-entrypoint-initdb.d/init-replica.sh
    entrypoint: ["/docker-entrypoint-initdb.d/init-replica.sh"]
    ports:
      - "5433:5432"
    networks:
      - edunet
    depends_on:
      postgres-primary:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 10s
      timeout: 5s
      retries: 10

  # ── Kafka（KRaft 模式，无 ZooKeeper）──────────────────────────
  kafka:
    image: bitnami/kafka:3.7
    container_name: kafka
    restart: unless-stopped
    environment:
      KAFKA_CFG_NODE_ID: 1
      KAFKA_CFG_PROCESS_ROLES: controller,broker
      KAFKA_CFG_LISTENERS: PLAINTEXT://:9092,CONTROLLER://:9093
      KAFKA_CFG_ADVERTISED_LISTENERS: PLAINTEXT://kafka:9092
      KAFKA_CFG_LISTENER_SECURITY_PROTOCOL_MAP: CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT
      KAFKA_CFG_CONTROLLER_LISTENER_NAMES: CONTROLLER
      KAFKA_CFG_CONTROLLER_QUORUM_VOTERS: 1@kafka:9093
      ALLOW_PLAINTEXT_LISTENER: "yes"
      KAFKA_KRAFT_CLUSTER_ID: "MkU3OEVBNTcwNTJENDM2Qg"
      KAFKA_CFG_AUTO_CREATE_TOPICS_ENABLE: "true"
    volumes:
      - kafka_data:/bitnami/kafka
    ports:
      - "9092:9092"
    networks:
      - edunet
    healthcheck:
      test: ["CMD-SHELL", "kafka-topics.sh --bootstrap-server localhost:9092 --list"]
      interval: 15s
      timeout: 10s
      retries: 10

  # ── Redis ─────────────────────────────────────────────────────
  redis:
    image: redis:7-alpine
    container_name: redis
    restart: unless-stopped
    command: redis-server --requirepass redis123 --appendonly yes
    volumes:
      - redis_data:/data
    ports:
      - "6379:6379"
    networks:
      - edunet
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "redis123", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
```

---

### Task 7: 创建复制用户初始化 SQL

**Files:**
- Create: `docker/postgres/init/00-replication-user.sql`

（注意：文件名以 00 开头，确保在 schema 之前执行）

- [ ] **Step 1: 写复制用户创建 SQL**

```sql
-- docker/postgres/init/00-replication-user.sql
-- 创建流复制专用用户
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'replicator') THEN
    CREATE USER replicator WITH REPLICATION ENCRYPTED PASSWORD 'replicator_pass';
  END IF;
END
$$;
```

---

### Task 8: 验证脚本

**Files:**
- Create: `scripts/verify.sh`

- [ ] **Step 1: 写验证脚本**

```bash
#!/bin/bash
# scripts/verify.sh - 验证所有服务健康状态
set -e
PASS=0
FAIL=0

check() {
  local name=$1; local cmd=$2
  if eval "$cmd" > /dev/null 2>&1; then
    echo "✅  $name"
    ((PASS++))
  else
    echo "❌  $name"
    ((FAIL++))
  fi
}

echo "=== 服务健康检查 ==="
check "PostgreSQL 主库可连接" "docker exec postgres-primary pg_isready -U postgres -d edudb"
check "PostgreSQL 副库可连接" "docker exec postgres-replica pg_isready -U postgres"
check "Redis 可连接" "docker exec redis redis-cli -a redis123 ping"
check "Kafka 可连接" "docker exec kafka kafka-topics.sh --bootstrap-server localhost:9092 --list"

echo ""
echo "=== 数据库表检查 ==="
check "departments 表存在" "docker exec postgres-primary psql -U postgres -d edudb -c '\dt departments' | grep departments"
check "students 表存在" "docker exec postgres-primary psql -U postgres -d edudb -c '\dt students' | grep students"
check "初始数据已插入" "docker exec postgres-primary psql -U postgres -d edudb -t -c 'SELECT COUNT(*) FROM students' | grep -E '[1-9]'"

echo ""
echo "=== 主从复制检查 ==="
check "复制槽/WAL 发送进程活跃" "docker exec postgres-primary psql -U postgres -d edudb -t -c 'SELECT COUNT(*) FROM pg_stat_replication' | grep -E '[1-9]'"

echo ""
echo "=== TimescaleDB 检查 ==="
check "timescaledb 扩展已安装" "docker exec postgres-primary psql -U postgres -d edudb -t -c \"SELECT extname FROM pg_extension WHERE extname='timescaledb'\" | grep timescaledb"

echo ""
echo "=== 结果 ==="
echo "通过: $PASS  失败: $FAIL"
[ $FAIL -eq 0 ] && echo "✅ 全部检查通过！" || echo "❌ 存在失败项，请检查日志"
exit $FAIL
```

- [ ] **Step 2: 设置可执行权限**

```bash
chmod +x scripts/verify.sh
```

---

### Task 9: 启动并验证

- [ ] **Step 1: 拉取镜像并启动所有服务**

```bash
cd /home/zxhtom/temp/github/claudecode
docker-compose pull
docker-compose up -d
```

- [ ] **Step 2: 等待服务就绪（约 60 秒）**

```bash
sleep 60
docker-compose ps
```

Expected: 所有服务 State 为 Up

- [ ] **Step 3: 运行验证脚本**

```bash
bash scripts/verify.sh
```

Expected: 全部检查通过

- [ ] **Step 4: 查看复制状态**

```bash
docker exec postgres-primary psql -U postgres -d edudb -c "SELECT client_addr, state, sent_lsn, write_lsn, flush_lsn, replay_lsn FROM pg_stat_replication;"
```

Expected: 至少一行 state = 'streaming'

- [ ] **Step 5: 提交**

```bash
cd /home/zxhtom/temp/github/claudecode
git add docker/ docker-compose.yml scripts/
git commit -m "feat: infrastructure setup - postgres primary/replica + timescaledb + kafka + redis"
```

---

## 服务连接信息

| 服务 | 地址 | 用户名 | 密码 |
|---|---|---|---|
| PostgreSQL 主库 | localhost:5432/edudb | postgres | postgres123 |
| PostgreSQL 副库 | localhost:5433 | postgres | postgres123 |
| Kafka Broker | localhost:9092 | — | — |
| Redis | localhost:6379 | — | redis123 |
