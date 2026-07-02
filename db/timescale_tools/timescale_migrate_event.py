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

  # PG 原生分区表迁移（d_load_current / d_load_power 等）
  python3 timescale_migrate_event.py --group curve --migrate-partitioned --dry-run
  python3 timescale_migrate_event.py --group curve --migrate-partitioned -y
  python3 timescale_migrate_event.py --tables d_load_current --migrate-partitioned -y
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


def is_partitioned(conn, table):
    """检查是否为 PostgreSQL 原生分区表（relkind='p'）。
    TimescaleDB 不支持对原生分区表调用 create_hypertable。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT relkind FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = %s AND n.nspname = 'public'
            """,
            (table,),
        )
        row = cur.fetchone()
        return row is not None and row[0] == 'p'


# ── 唯一索引冲突预处理 ──────────────────────────────────────────────────────

def drop_conflicting_unique_indexes(conn, table, time_col, verbose):
    """DROP 不含 time_col 的唯一索引（TimescaleDB 要求唯一索引必须包含分区列）。
    返回被删除的索引名列表，便于日志记录。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.relname AS idx_name, pg_get_indexdef(i.oid) AS idx_def
            FROM pg_index ix
            JOIN pg_class t  ON t.oid  = ix.indrelid
            JOIN pg_class i  ON i.oid  = ix.indexrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = 'public'
              AND t.relname  = %s
              AND ix.indisunique = true
              AND ix.indisprimary = false
            """,
            (table,),
        )
        rows = cur.fetchall()

    to_drop = [name for name, defn in rows if time_col not in defn]
    for idx_name in to_drop:
        sql = f'DROP INDEX "{idx_name}";'
        if verbose:
            print(f"    SQL: {sql}")
        with conn.cursor() as cur:
            cur.execute(sql)
        print(f"    ✓ DROP 唯一索引 {idx_name}（不含 {time_col}，与 TimescaleDB 不兼容）")
    return to_drop


# ── 分区表迁移 ───────────────────────────────────────────────────────────────

def migrate_partitioned_table(conn, table, time_col, chunk_interval, segmentby, compress_after, dry_run, verbose):
    """
    PG 原生分区表 → TimescaleDB 超表
      1. 创建 {table}_ts_new（普通表，同结构）
      2. create_hypertable
      3. 列存压缩 + 自动压缩策略
      4. INSERT INTO ts_new SELECT * FROM 原表（全量复制，可能较慢）
      5. 原表重命名为 {table}_pg_old（备份），ts_new 重命名为 {table}
    """
    tmp = f"{table}_ts_new"
    old_bak = f"{table}_pg_old"
    print(f"\n  [MIGRATE] {table}: PG 分区表 → TimescaleDB（备份: {old_bak}）")

    if dry_run:
        for line in [
            f'DROP TABLE IF EXISTS "{tmp}";',
            f'CREATE TABLE "{tmp}" (LIKE "{table}" INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING COMMENTS);',
            f"SELECT create_hypertable('public.{tmp}', by_range('{time_col}', INTERVAL '{chunk_interval}'), ...);",
            f'ALTER TABLE "{tmp}" SET (timescaledb.compress, ...);',
            f"SELECT add_compression_policy('public.{tmp}', INTERVAL '{compress_after}', ...);",
            f'INSERT INTO "{tmp}" SELECT * FROM "{table}";  -- 全量复制',
            f'ALTER TABLE "{table}" RENAME TO "{old_bak}"; ALTER TABLE "{tmp}" RENAME TO "{table}";',
        ]:
            print(f"    -- {line}")
        return "ok"

    def _exec(desc, sql):
        if verbose:
            print(f"    SQL: {sql}")
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        print(f"    ✓ {desc}")

    try:
        _exec("清理残留临时表", f'DROP TABLE IF EXISTS "{tmp}";')
        _exec(
            f"创建新普通表 {tmp}",
            f'CREATE TABLE "{tmp}" ('
            f'LIKE "{table}" INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING COMMENTS'
            f');',
        )
        _exec(
            f"create_hypertable({tmp}, {time_col}, chunk={chunk_interval})",
            f"SELECT create_hypertable("
            f"'public.{tmp}', "
            f"by_range('{time_col}', INTERVAL '{chunk_interval}'), "
            f"migrate_data => false, "
            f"if_not_exists => true"
            f");",
        )
        _exec(
            f"开启列存压缩（segmentby={segmentby}）",
            f'ALTER TABLE "{tmp}" SET ('
            f"timescaledb.compress, "
            f"timescaledb.compress_segmentby = '{segmentby}', "
            f"timescaledb.compress_orderby = '{time_col} DESC'"
            f");",
        )
        _exec(
            f"添加自动压缩策略（{compress_after} 后压缩）",
            f"SELECT add_compression_policy("
            f"'public.{tmp}', "
            f"INTERVAL '{compress_after}', "
            f"if_not_exists => true"
            f");",
        )
        print(f"    复制数据 {table} → {tmp}（全量，可能较慢）…", flush=True)
        with conn.cursor() as cur:
            cur.execute(f'INSERT INTO "{tmp}" SELECT * FROM "{table}";')
            inserted = cur.rowcount
        conn.commit()
        print(f"    ✓ 数据复制完成，{inserted:,} 行")
        with conn.cursor() as cur:
            cur.execute(f'ALTER TABLE "{table}" RENAME TO "{old_bak}";')
            cur.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}";')
        conn.commit()
        print(f"    ✓ 重命名完成（原分区表已备份为 {old_bak}）")
        print(f"  [OK]    {table}: 迁移完成（{old_bak} 可确认后手动 DROP）")
        return "ok"
    except psycopg2.Error as e:
        conn.rollback()
        print(f"  [ERROR] {table}: 迁移失败 — {str(e).strip()}")
        print(f"          临时表 {tmp} 保留以供检查，可手动 DROP 后重试")
        return "error"


# ── 转换单张表 ───────────────────────────────────────────────────────────────

def convert_table(conn, table, time_col, chunk_interval, segmentby, compress_after,
                  dry_run, verbose, migrate_partitioned=False):
    """
    返回 'skip' | 'ok' | 'error'
    """
    if dry_run and conn is None:
        # 无数据库连接时只打印 SQL，跳过状态检查
        if migrate_partitioned:
            migrate_partitioned_table(conn, table, time_col, chunk_interval,
                                      segmentby, compress_after, dry_run=True, verbose=verbose)
        else:
            stmts_dry = [
                (f"create_hypertable({table}, {time_col})",
                 f"SELECT create_hypertable('public.{table}', by_range('{time_col}', INTERVAL '{chunk_interval}'), migrate_data => true, if_not_exists => true);"),
                (f"列存压缩",
                 f"ALTER TABLE \"{table}\" SET (timescaledb.compress, timescaledb.compress_segmentby = '{segmentby}', timescaledb.compress_orderby = '{time_col} DESC');"),
                (f"自动压缩策略",
                 f"SELECT add_compression_policy('public.{table}', INTERVAL '{compress_after}', if_not_exists => true);"),
            ]
            print(f"  [DRY]   {table}:")
            for desc, sql in stmts_dry:
                print(f"    -- {desc}")
                print(f"    {sql}")
        return "ok"

    if not table_exists(conn, table):
        print(f"  [WARN]  {table}: 表不存在，跳过")
        return "skip"

    if is_hypertable(conn, table):
        print(f"  [SKIP]  {table}: 已是 TimescaleDB 超表")
        return "skip"

    if is_partitioned(conn, table):
        if not migrate_partitioned:
            print(f"  [SKIP]  {table}: 已是 PG 原生分区表，加 --migrate-partitioned 可迁移")
            return "skip"
        return migrate_partitioned_table(
            conn, table, time_col, chunk_interval, segmentby, compress_after, dry_run, verbose
        )

    if not dry_run:
        dropped = drop_conflicting_unique_indexes(conn, table, time_col, verbose)
        if dropped:
            conn.commit()

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
    p.add_argument("--migrate-partitioned", action="store_true",
                   help="将 PG 原生分区表迁移为 TimescaleDB 超表（创建新表 → 复制数据 → 重命名）")
    p.add_argument("-y", "--yes", action="store_true",
                   help="迁移分区表时跳过确认提示")
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

    migrate_partitioned = args.migrate_partitioned
    print(f"待处理: {len(targets)} 张表  dry_run={args.dry_run}  migrate_partitioned={migrate_partitioned}")
    print()

    if args.dry_run:
        for table, time_col, chunk, seg, comp in targets:
            convert_table(None, table, time_col, chunk, seg, comp,
                          dry_run=True, verbose=False,
                          migrate_partitioned=migrate_partitioned)
        print("\n[dry-run] 未连接数据库，未执行任何操作。")
        return

    # 迁移分区表前需要确认（数据量大，操作不可逆）
    if migrate_partitioned and not args.yes:
        partitioned = []
        conn_tmp = psycopg2.connect(get_dsn(args.env_file))
        for table, *_ in targets:
            if table_exists(conn_tmp, table) and is_partitioned(conn_tmp, table):
                partitioned.append(table)
        conn_tmp.close()
        if partitioned:
            print("以下 PG 原生分区表将被迁移（原表备份为 _pg_old，操作耗时较长）：")
            for t in partitioned:
                print(f"  • {t}")
            ans = input("\n确认继续？[y/N] ").strip().lower()
            if ans != "y":
                sys.exit("已取消。")

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
                               dry_run=False, verbose=args.verbose,
                               migrate_partitioned=migrate_partitioned)
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
