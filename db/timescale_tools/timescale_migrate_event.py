#!/usr/bin/env python3
"""把事件表和曲线表原地转换为 TimescaleDB 超表（幂等，可重复执行）。

对每张表：
  • 已是超表 → 跳过（打印 SKIP）
  • 普通表   → create_hypertable + 列存压缩 + 自动压缩策略

三组表：
  [event]  d_alarm_event、d_*_event_log 等 10 张事件表
  [curve]  d_load_*、d_read_curve*、d_demand_curve 等 12 张曲线表
  [log]    sys_fep_comm_log 等通讯日志表

用法：
  python3 timescale_migrate_event.py
  python3 timescale_migrate_event.py --dry-run
  python3 timescale_migrate_event.py --group curve
  python3 timescale_migrate_event.py --group log
  python3 timescale_migrate_event.py --tables d_load_voltage,sys_fep_comm_log
  python3 timescale_migrate_event.py --env-file ../meter_seed/db.env
"""
import argparse
import sys

import psycopg2

from dbconfig import get_dsn

# ── 表定义 ───────────────────────────────────────────────────────────────────
# 每项：(table_name, time_column, chunk_interval, segmentby, compress_after)

EVENT_TABLES = [
    ("d_alarm_event",                   "alarm_time",  "30 days", "device_id", "90 days"),
    ("d_communication_event_log",       "event_time",  "30 days", "mp_id",     "90 days"),
    ("d_config_modification_event_log", "event_time",  "30 days", "mp_id",     "90 days"),
    ("d_disconnector_event_log",        "event_time",  "30 days", "mp_id",     "90 days"),
    ("d_fraud_event_log",               "event_time",  "30 days", "mp_id",     "90 days"),
    ("d_power_failure_event_log",       "event_time",  "30 days", "mp_id",     "90 days"),
    ("d_power_quality_event_log",       "event_time",  "30 days", "mp_id",     "90 days"),
    ("d_recharge_event_log",            "event_time",  "30 days", "mp_id",     "90 days"),
    ("d_special_event_log",             "event_time",  "30 days", "mp_id",     "90 days"),
    ("d_standard_event_log",            "event_time",  "30 days", "mp_id",     "90 days"),
]

CURVE_TABLES = [
    ("d_load_voltage",  "coll_time", "7 days", "mp_id", "30 days"),
    ("d_load_current",  "coll_time", "7 days", "mp_id", "30 days"),
    ("d_load_power",    "coll_time", "7 days", "mp_id", "30 days"),
    ("d_load_power_r",  "coll_time", "7 days", "mp_id", "30 days"),
    ("d_load_power_v",  "coll_time", "7 days", "mp_id", "30 days"),
    ("d_load_instant",  "coll_time", "7 days", "mp_id", "30 days"),
    ("d_load_angle",    "coll_time", "7 days", "mp_id", "30 days"),
    ("d_load_status",   "coll_time", "7 days", "mp_id", "30 days"),
    ("d_read_curve",    "coll_time", "7 days", "mp_id", "30 days"),
    ("d_read_curve_r",  "coll_time", "7 days", "mp_id", "30 days"),
    ("d_read_curve_v",  "coll_time", "7 days", "mp_id", "30 days"),
    ("d_demand_curve",  "coll_time", "7 days", "mp_id", "30 days"),
]

LOG_TABLES = [
    ("sys_fep_comm_log", "start_time", "1 day", "device_id", "7 days"),
]

ALL_TABLES = EVENT_TABLES + CURVE_TABLES + LOG_TABLES


# ── 检查是否已是超表 ─────────────────────────────────────────────────────────

def is_hypertable(conn, table):
    """用官方 view 判断，避免 _timescaledb_catalog 残留记录导致误判。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM timescaledb_information.hypertables
            WHERE hypertable_schema = 'public'
              AND hypertable_name   = %s
            """,
            (table,),
        )
        return cur.fetchone()[0] > 0


def table_exists(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (table,),
        )
        return cur.fetchone()[0] > 0


# ── 转换单张表 ───────────────────────────────────────────────────────────────

def convert_table(conn, table, time_col, chunk_interval, segmentby, compress_after, dry_run, verbose):
    """
    返回 'skip' | 'ok' | 'error'
    """
    if not table_exists(conn, table):
        print(f"  [WARN]  {table}: 表不存在，跳过")
        return "skip"

    if is_hypertable(conn, table):
        print(f"  [SKIP]  {table}: 已是超表")
        return "skip"

    stmts = [
        (
            f"create_hypertable({table}, {time_col}, chunk={chunk_interval})",
            f"SELECT create_hypertable("
            f"'public.{table}', "
            f"by_range('{time_col}', INTERVAL '{chunk_interval}'), "
            f"migrate_data => true, "
            f"if_not_exists => true"
            f");",
        ),
        (
            f"开启列存压缩（segmentby={segmentby}）",
            f"ALTER TABLE \"{table}\" SET ("
            f"timescaledb.compress, "
            f"timescaledb.compress_segmentby = '{segmentby}', "
            f"timescaledb.compress_orderby = '{time_col} DESC'"
            f");",
        ),
        (
            f"添加自动压缩策略（{compress_after} 后压缩）",
            f"SELECT add_compression_policy("
            f"'public.{table}', "
            f"INTERVAL '{compress_after}', "
            f"if_not_exists => true"
            f");",
        ),
    ]

    if dry_run:
        print(f"  [DRY]   {table}:")
        for desc, sql in stmts:
            print(f"    -- {desc}")
            print(f"    {sql}")
        return "ok"

    try:
        for desc, sql in stmts:
            if verbose:
                print(f"    SQL: {sql}")
            with conn.cursor() as cur:
                cur.execute(sql)
            print(f"    ✓ {desc}")
        conn.commit()
        print(f"  [OK]    {table}: 转换完成")
        return "ok"
    except psycopg2.Error as e:
        conn.rollback()
        print(f"  [ERROR] {table}: {str(e).strip()}")
        return "error"


# ── main ─────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--group", choices=["event", "curve", "log", "all"], default="all",
                   help="只处理指定分组（默认 all）")
    p.add_argument("--tables",   help="逗号分隔的表名，只处理这些表")
    p.add_argument("--env-file", help="db.env 路径")
    p.add_argument("--dry-run",  action="store_true",
                   help="只打印将执行的 SQL，不真正执行")
    p.add_argument("-v", "--verbose", action="store_true", help="打印每条 SQL")
    return p.parse_args()


def main():
    args = parse_args()

    # 确定要处理的表
    if args.tables:
        name_set = {t.strip() for t in args.tables.split(",")}
        targets = [row for row in ALL_TABLES if row[0] in name_set]
        unknown = name_set - {row[0] for row in ALL_TABLES}
        if unknown:
            sys.exit(f"未知表名: {unknown}")
    elif args.group == "event":
        targets = EVENT_TABLES
    elif args.group == "curve":
        targets = CURVE_TABLES
    elif args.group == "log":
        targets = LOG_TABLES
    else:
        targets = ALL_TABLES

    print(f"待处理: {len(targets)} 张表  dry_run={args.dry_run}")
    print()

    if args.dry_run:
        for table, time_col, chunk, seg, comp in targets:
            convert_table(None, table, time_col, chunk, seg, comp, dry_run=True, verbose=False)
        print("\n[dry-run] 未连接数据库，未执行任何操作。")
        return

    conn = psycopg2.connect(get_dsn(args.env_file))
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")
        conn.commit()
    except psycopg2.Error as e:
        print(f"[ERROR] 无法启用 timescaledb 扩展: {e}")
        conn.close()
        sys.exit(1)

    ok = skip = error = 0
    for table, time_col, chunk, seg, comp in targets:
        result = convert_table(conn, table, time_col, chunk, seg, comp,
                               dry_run=False, verbose=args.verbose)
        if result == "ok":
            ok += 1
        elif result == "skip":
            skip += 1
        else:
            error += 1

    conn.close()
    print(f"\n完成: 转换 {ok} 张，跳过 {skip} 张，失败 {error} 张")
    sys.exit(1 if error else 0)


if __name__ == "__main__":
    main()
