#!/usr/bin/env python3
"""
将 Event Log + Comm Log 关键表迁移到 TimescaleDB。
流程：① 备份原表  ② 重建主键（如需）  ③ create_hypertable  ④ 开启压缩
"""
import sys, time
import psycopg2
from psycopg2 import sql
from datetime import date

# ── 数据库连接 ─────────────────────────────────────────────
DB = {
    "host":     "172.17.182.123",
    "port":     5432,
    "dbname":   "eco_ma",
    "user":     "hes",
    "password": "7L2wYCDWLQdqPr4bNsYBMr5nwutckP8Q",
    "options":  "-c statement_timeout=0",   # 备份/迁移可能很久
    "connect_timeout": 15,
}

# ── 目标表配置 ────────────────────────────────────────────
# pk_drop/pk_add: 若 PK 不含 time_column 则需要先重建
TABLES = [
    {
        "name":           "sys_fep_comm_log",
        "time_col":       "start_time",
        "chunk_interval": "1 day",
        "segmentby":      "device_id,device_type",
        "orderby":        "start_time DESC, comm_log_id",
        "compress_after": "7 days",
        "pk_drop":        "sys_fep_comm_log_pkey",
        "pk_add":         "(comm_log_id, start_time)",
    },
    {
        "name":           "d_standard_event_log",
        "time_col":       "event_time",
        "chunk_interval": "7 days",
        "segmentby":      "mp_id",
        "orderby":        "event_time DESC, event_code, sub_event_code, data_date",
        "compress_after": "30 days",
    },
    {
        "name":           "d_power_quality_event_log",
        "time_col":       "event_time",
        "chunk_interval": "7 days",
        "segmentby":      "mp_id",
        "orderby":        "event_time DESC, event_code, sub_event_code, data_date",
        "compress_after": "30 days",
    },
    {
        "name":           "d_fraud_event_log",
        "time_col":       "event_time",
        "chunk_interval": "7 days",
        "segmentby":      "mp_id",
        "orderby":        "event_time DESC, event_code, sub_event_code, data_date",
        "compress_after": "30 days",
    },
    {
        "name":           "d_disconnector_event_log",
        "time_col":       "event_time",
        "chunk_interval": "7 days",
        "segmentby":      "mp_id",
        "orderby":        "event_time DESC, event_code, sub_event_code, data_date",
        "compress_after": "30 days",
    },
    {
        "name":           "d_communication_event_log",
        "time_col":       "event_time",
        "chunk_interval": "7 days",
        "segmentby":      "mp_id",
        "orderby":        "event_time DESC, event_code, sub_event_code, data_date",
        "compress_after": "30 days",
    },
    {
        "name":           "d_power_failure_event_log",
        "time_col":       "event_time",
        "chunk_interval": "7 days",
        "segmentby":      "mp_id",
        "orderby":        "event_time DESC, event_code, sub_event_code, data_date",
        "compress_after": "30 days",
    },
    {
        "name":           "d_config_modification_event_log",
        "time_col":       "event_time",
        "chunk_interval": "7 days",
        "segmentby":      "mp_id",
        "orderby":        "event_time DESC, event_code, sub_event_code, data_date",
        "compress_after": "30 days",
    },
]

# ── 进度打印 ──────────────────────────────────────────────
def step(idx, total, tname, msg, icon="  "):
    print(f"[{idx}/{total}] {icon} {tname}: {msg}", flush=True)

def bar(label, current, total, width=30):
    filled = int(width * current / max(total, 1))
    b = "█" * filled + "░" * (width - filled)
    pct = int(100 * current / max(total, 1))
    print(f"  [{b}] {pct:3d}%  {label}", end="\r", flush=True)

def ok(msg):  print(f"  ✅  {msg}", flush=True)
def warn(msg):print(f"  ⚠️   {msg}", flush=True)
def err(msg): print(f"  ❌  {msg}", flush=True)

# ── 工具函数 ──────────────────────────────────────────────
def table_exists(cur, tname):
    cur.execute(
        "SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=%s", (tname,)
    )
    return cur.fetchone() is not None

def is_hypertable(cur, tname):
    cur.execute(
        "SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name=%s", (tname,)
    )
    return cur.fetchone() is not None

def row_count(cur, tname):
    cur.execute(f"SELECT reltuples::bigint FROM pg_class WHERE relname=%s", (tname,))
    r = cur.fetchone()
    return r[0] if r else 0

def compression_enabled(cur, tname):
    cur.execute(
        "SELECT compression_enabled FROM timescaledb_information.hypertables WHERE hypertable_name=%s",
        (tname,)
    )
    r = cur.fetchone()
    return r and r[0]

# ── 备份一张表 ────────────────────────────────────────────
def backup_table(conn, tname, bak_name):
    with conn.cursor() as cur:
        if table_exists(cur, bak_name):
            warn(f"备份表 {bak_name} 已存在，跳过")
            return
    print(f"  📦 备份 → {bak_name}  (可能需要几分钟，取决于数据量...)", flush=True)
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute(f'CREATE TABLE "{bak_name}" AS SELECT * FROM "{tname}"')
    conn.commit()
    elapsed = time.time() - t0
    ok(f"备份完成  耗时 {elapsed:.1f}s")

# ── TimescaleDB 迁移 ──────────────────────────────────────
def migrate_to_hypertable(conn, cfg):
    tname = cfg["name"]
    time_col = cfg["time_col"]

    with conn.cursor() as cur:
        if is_hypertable(cur, tname):
            warn("已是 hypertable，跳过 create_hypertable")
            conn.rollback()
            # 仍尝试补压缩
            _ensure_compression(conn, cfg)
            return

    # Step A: 重建主键（仅部分表需要）
    if cfg.get("pk_drop"):
        print(f"  🔧 Step A: 重建主键 (纳入 {time_col})", flush=True)
        with conn.cursor() as cur:
            cur.execute(f'ALTER TABLE "{tname}" DROP CONSTRAINT IF EXISTS "{cfg["pk_drop"]}"')
            cur.execute(f"""
                DO $$ BEGIN
                  IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = '{tname}'::regclass AND contype = 'p'
                  ) THEN
                    ALTER TABLE "{tname}" ADD PRIMARY KEY {cfg["pk_add"]};
                  END IF;
                END $$;
            """)
        conn.commit()
        ok("主键重建完成")

    # Step B: create_hypertable（migrate_data 会迁移现有数据到 chunks）
    print(f"  🕐 Step B: create_hypertable (migrate_data=true，大表较慢)...", flush=True)
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT create_hypertable(
                '{tname}', '{time_col}',
                migrate_data      => true,
                chunk_time_interval => INTERVAL '{cfg["chunk_interval"]}',
                if_not_exists     => TRUE
            )
        """)
    conn.commit()
    ok(f"create_hypertable 完成  耗时 {time.time()-t0:.1f}s")

    _ensure_compression(conn, cfg)

def _ensure_compression(conn, cfg):
    tname = cfg["name"]
    with conn.cursor() as cur:
        if compression_enabled(cur, tname):
            warn("压缩已启用，跳过")
            conn.rollback()
            return

    # Step C: 开启压缩
    print(f"  🗜  Step C: 开启压缩 (segmentby={cfg['segmentby']})", flush=True)
    with conn.cursor() as cur:
        cur.execute(f"""
            ALTER TABLE "{tname}" SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = '{cfg["segmentby"]}',
                timescaledb.compress_orderby   = '{cfg["orderby"]}'
            )
        """)
    conn.commit()
    ok("压缩参数设置完成")

    # Step D: 压缩策略
    print(f"  ⏱  Step D: 设置压缩策略 (compress_after={cfg['compress_after']})", flush=True)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT add_compression_policy(
                '{tname}', INTERVAL '{cfg["compress_after"]}', if_not_exists => true
            )
        """)
    conn.commit()
    ok(f"压缩策略已设置：{cfg['compress_after']} 后自动压缩")

# ── 汇总 ──────────────────────────────────────────────────
def print_summary(results):
    print("\n" + "═" * 60, flush=True)
    print("  迁移汇总", flush=True)
    print("═" * 60, flush=True)
    for r in results:
        status = "✅ 完成" if r["ok"] else f"❌ 失败: {r['error']}"
        rows = f"{r['rows']:,}" if r["rows"] >= 0 else "?"
        bak  = r["bak"] or "（已存在/跳过）"
        print(f"  {r['name']}", flush=True)
        print(f"    行数  ≈ {rows}  |  备份 → {bak}  |  {status}", flush=True)
    print("═" * 60, flush=True)

# ── 主流程 ────────────────────────────────────────────────
def main():
    today = date.today().strftime("%Y%m%d")
    total = len(TABLES)

    print(f"\n{'═'*60}", flush=True)
    print(f"  HES Event/Comm Log → TimescaleDB 迁移", flush=True)
    print(f"  目标：{DB['host']}:{DB['port']}/{DB['dbname']}", flush=True)
    print(f"  表数：{total}  |  备份后缀：_bak_{today}", flush=True)
    print(f"{'═'*60}\n", flush=True)

    try:
        conn = psycopg2.connect(**DB)
        conn.autocommit = False
        ok(f"数据库连接成功")
    except Exception as e:
        err(f"无法连接数据库: {e}")
        sys.exit(1)

    results = []
    for idx, cfg in enumerate(TABLES, 1):
        tname = cfg["name"]
        bak_name = f"{tname}_bak_{today}"
        print(f"\n{'─'*60}", flush=True)
        print(f"[{idx}/{total}] 开始处理: {tname}", flush=True)
        print(f"{'─'*60}", flush=True)

        result = {"name": tname, "ok": False, "bak": None, "rows": -1, "error": ""}
        t_start = time.time()

        try:
            with conn.cursor() as cur:
                if not table_exists(cur, tname):
                    conn.rollback()
                    warn(f"表 {tname} 不存在，跳过")
                    result["error"] = "表不存在"
                    results.append(result)
                    continue

                approx = row_count(cur, tname)
                conn.rollback()
            result["rows"] = approx
            print(f"  📊 约 {approx:,} 行", flush=True)

            # ① 备份
            print(f"\n  ── ① 备份 ──────────────────────", flush=True)
            backup_table(conn, tname, bak_name)
            result["bak"] = bak_name

            # ② 迁移
            print(f"\n  ── ② TimescaleDB 迁移 ─────────", flush=True)
            migrate_to_hypertable(conn, cfg)

            elapsed = time.time() - t_start
            ok(f"[{idx}/{total}] {tname} 全部完成  总耗时 {elapsed:.1f}s")
            result["ok"] = True

        except Exception as e:
            conn.rollback()
            err(str(e))
            result["error"] = str(e)

        results.append(result)

    conn.close()
    print_summary(results)

if __name__ == "__main__":
    main()

