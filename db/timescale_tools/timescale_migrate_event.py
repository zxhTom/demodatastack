#!/usr/bin/env python3
"""把事件表和曲线表转换为 TimescaleDB 超表（幂等，可重复执行）。

所有迁移均保留原表：
  • 首次迁移   → 原表重命名为 {table}_pg_old，新超表取原名
  • --redo     → 从 _pg_old 重建正确超表（修正分区列/补主键）

三组表：
  [event]  d_alarm_event、d_*_event_log 等 10 张事件表
  [curve]  d_load_*、d_read_curve*、d_demand_curve 等 12 张曲线表
  [log]    sys_fep_comm_log 等通讯日志表

用法：
  python3 timescale_migrate_event.py
  python3 timescale_migrate_event.py --group curve --migrate-partitioned -y
  python3 timescale_migrate_event.py --group curve --redo -y   # 修正已迁移的 9 张
  python3 timescale_migrate_event.py --tables d_load_angle,d_load_status,d_demand_curve
  python3 timescale_migrate_event.py --dry-run
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

# 曲线表 PK = (mp_id, data_date, data_time, profile_id, supply_id)
# 时间列用 data_date（存在于 PK 中），TimescaleDB 要求分区列必须出现在所有唯一约束里
CURVE_TABLES = [
    ("d_load_voltage",  "data_date", "7 days", "mp_id", "30 days"),
    ("d_load_current",  "data_date", "7 days", "mp_id", "30 days"),
    ("d_load_power",    "data_date", "7 days", "mp_id", "30 days"),
    ("d_load_power_r",  "data_date", "7 days", "mp_id", "30 days"),
    ("d_load_power_v",  "data_date", "7 days", "mp_id", "30 days"),
    ("d_load_instant",  "data_date", "7 days", "mp_id", "30 days"),
    ("d_load_angle",    "data_date", "7 days", "mp_id", "30 days"),
    ("d_load_status",   "data_date", "7 days", "mp_id", "30 days"),
    ("d_read_curve",    "data_date", "7 days", "mp_id", "30 days"),
    ("d_read_curve_r",  "data_date", "7 days", "mp_id", "30 days"),
    ("d_read_curve_v",  "data_date", "7 days", "mp_id", "30 days"),
    ("d_demand_curve",  "data_date", "7 days", "mp_id", "30 days"),
]

LOG_TABLES = [
    ("sys_fep_comm_log", "start_time", "1 day", "device_id", "7 days"),
]

ALL_TABLES = EVENT_TABLES + CURVE_TABLES + LOG_TABLES


# ── DB 状态检查 ──────────────────────────────────────────────────────────────

def is_hypertable(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM timescaledb_information.hypertables
            WHERE hypertable_schema = 'public' AND hypertable_name = %s
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


# ── 迁移核心 ─────────────────────────────────────────────────────────────────

def _exec(conn, desc, sql, verbose):
    if verbose:
        print(f"    SQL: {sql}")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    print(f"    ✓ {desc}")


def _build_hypertable(conn, tmp, src, time_col, chunk_interval, segmentby, compress_after, verbose):
    """
    创建 tmp 表（从 src LIKE INCLUDING ALL），建超表，压缩策略，复制数据。
    不做重命名，调用方负责处理原表和新表的命名。
    """
    _exec(conn, "清理残留临时表", f'DROP TABLE IF EXISTS "{tmp}";', verbose)
    _exec(conn, f"从 {src} 创建新普通表 {tmp}（含完整结构）",
          f'CREATE TABLE "{tmp}" (LIKE "{src}" INCLUDING ALL);', verbose)
    _exec(conn, f"create_hypertable({tmp}, {time_col}, chunk={chunk_interval})",
          f"SELECT create_hypertable('public.{tmp}', "
          f"by_range('{time_col}', INTERVAL '{chunk_interval}'), "
          f"migrate_data => false, if_not_exists => true);", verbose)
    _exec(conn, f"开启列存压缩（segmentby={segmentby}）",
          f'ALTER TABLE "{tmp}" SET ('
          f"timescaledb.compress, "
          f"timescaledb.compress_segmentby = '{segmentby}', "
          f"timescaledb.compress_orderby = '{time_col} DESC');", verbose)
    _exec(conn, f"添加自动压缩策略（{compress_after} 后压缩）",
          f"SELECT add_compression_policy('public.{tmp}', "
          f"INTERVAL '{compress_after}', if_not_exists => true);", verbose)
    print(f"    复制数据 {src} → {tmp}（全量，可能较慢）…", flush=True)
    with conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{tmp}" SELECT * FROM "{src}";')
        inserted = cur.rowcount
    conn.commit()
    print(f"    ✓ 数据复制完成，{inserted:,} 行")


def _print_dry_steps(steps):
    for line in steps:
        print(f"    -- {line}")


# ── 首次迁移（普通表或 PG 分区表）──────────────────────────────────────────

def migrate_table(conn, table, time_col, chunk_interval, segmentby, compress_after, dry_run, verbose):
    """
    流程：
      1. 创建 {table}_ts_new（从原表 LIKE INCLUDING ALL）
      2. create_hypertable / 压缩 / 复制数据
      3. 原表 → {table}_pg_old（备份）
      4. {table}_ts_new → {table}
    """
    tmp = f"{table}_ts_new"
    bak = f"{table}_pg_old"
    print(f"\n  [MIGRATE] {table} → TimescaleDB（原表备份: {bak}）")
    if dry_run:
        _print_dry_steps([
            f'DROP TABLE IF EXISTS "{tmp}";',
            f'CREATE TABLE "{tmp}" (LIKE "{table}" INCLUDING ALL);',
            f"SELECT create_hypertable('public.{tmp}', by_range('{time_col}', INTERVAL '{chunk_interval}'), ...);",
            f'ALTER TABLE "{tmp}" SET (timescaledb.compress, ...);',
            f"SELECT add_compression_policy('public.{tmp}', INTERVAL '{compress_after}', ...);",
            f'INSERT INTO "{tmp}" SELECT * FROM "{table}";',
            f'ALTER TABLE "{table}" RENAME TO "{bak}";',
            f'ALTER TABLE "{tmp}" RENAME TO "{table}";',
        ])
        return "ok"
    try:
        _build_hypertable(conn, tmp, table, time_col, chunk_interval, segmentby, compress_after, verbose)
        with conn.cursor() as cur:
            cur.execute(f'ALTER TABLE "{table}" RENAME TO "{bak}";')
            cur.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}";')
        conn.commit()
        print(f"    ✓ 重命名完成（原表备份为 {bak}）")
        print(f"  [OK]    {table}: 迁移完成")
        return "ok"
    except psycopg2.Error as e:
        conn.rollback()
        print(f"  [ERROR] {table}: 迁移失败 — {str(e).strip()}")
        print(f"          临时表 {tmp} 保留以供检查，可手动 DROP 后重试")
        return "error"


# ── 重新迁移（从 _pg_old 修正分区列 / 补主键）──────────────────────────────

def remigrate_from_backup(conn, table, time_col, chunk_interval, segmentby, compress_after, dry_run, verbose):
    """
    流程：
      1. 创建 {table}_ts_new（从 {table}_pg_old LIKE INCLUDING ALL）
      2. create_hypertable / 压缩 / 复制数据
      3. DROP 当前错误超表 {table}
      4. {table}_ts_new → {table}
      {table}_pg_old 全程保留不动。
    """
    tmp = f"{table}_ts_new"
    bak = f"{table}_pg_old"
    if not table_exists(conn, bak):
        print(f"  [WARN]  {table}: 找不到备份 {bak}，跳过 redo")
        return "skip"
    print(f"\n  [REDO]  {table}: 从 {bak} 重新迁移（分区列: {time_col}）")
    if dry_run:
        _print_dry_steps([
            f'DROP TABLE IF EXISTS "{tmp}";',
            f'CREATE TABLE "{tmp}" (LIKE "{bak}" INCLUDING ALL);',
            f"SELECT create_hypertable('public.{tmp}', by_range('{time_col}', INTERVAL '{chunk_interval}'), ...);",
            f'ALTER TABLE "{tmp}" SET (timescaledb.compress, ...);',
            f"SELECT add_compression_policy('public.{tmp}', INTERVAL '{compress_after}', ...);",
            f'INSERT INTO "{tmp}" SELECT * FROM "{bak}";',
            f'DROP TABLE "{table}";  -- 删除错误超表',
            f'ALTER TABLE "{tmp}" RENAME TO "{table}";  -- {bak} 保留为备份',
        ])
        return "ok"
    try:
        _build_hypertable(conn, tmp, bak, time_col, chunk_interval, segmentby, compress_after, verbose)
        with conn.cursor() as cur:
            cur.execute(f'DROP TABLE "{table}";')
            cur.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}";')
        conn.commit()
        print(f"    ✓ 重命名完成（{bak} 保留为原始备份）")
        print(f"  [OK]    {table}: 重新迁移完成")
        return "ok"
    except psycopg2.Error as e:
        conn.rollback()
        print(f"  [ERROR] {table}: 重新迁移失败 — {str(e).strip()}")
        return "error"


# ── 转换分发器 ───────────────────────────────────────────────────────────────

def convert_table(conn, table, time_col, chunk_interval, segmentby, compress_after,
                  dry_run, verbose, migrate_partitioned=False, redo=False):
    """返回 'skip' | 'ok' | 'error'"""
    if dry_run and conn is None:
        migrate_table(None, table, time_col, chunk_interval, segmentby, compress_after,
                      dry_run=True, verbose=False)
        return "ok"

    if not table_exists(conn, table):
        print(f"  [WARN]  {table}: 表不存在，跳过")
        return "skip"

    if is_hypertable(conn, table):
        if redo:
            return remigrate_from_backup(conn, table, time_col, chunk_interval,
                                         segmentby, compress_after, dry_run, verbose)
        print(f"  [SKIP]  {table}: 已是 TimescaleDB 超表（加 --redo 可从 _pg_old 重新迁移）")
        return "skip"

    if is_partitioned(conn, table) and not migrate_partitioned:
        print(f"  [SKIP]  {table}: 已是 PG 原生分区表，加 --migrate-partitioned 可迁移")
        return "skip"

    # 普通平表或 PG 分区表，统一走 migrate_table（备份原表 → 建超表）
    return migrate_table(conn, table, time_col, chunk_interval, segmentby, compress_after,
                         dry_run, verbose)


# ── main ─────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--group", choices=["event", "curve", "log", "all"], default="all",
                   help="只处理指定分组（默认 all）")
    p.add_argument("--tables",   help="逗号分隔的表名，只处理这些表")
    p.add_argument("--env-file", help="db.env 路径")
    p.add_argument("--dry-run",  action="store_true", help="只打印 SQL，不执行")
    p.add_argument("--migrate-partitioned", action="store_true",
                   help="将 PG 原生分区表迁移为 TimescaleDB 超表")
    p.add_argument("--redo", action="store_true",
                   help="对已是超表但有 _pg_old 备份的表，从备份重新迁移（修正分区列/补主键）")
    p.add_argument("-y", "--yes", action="store_true", help="跳过确认提示")
    p.add_argument("-v", "--verbose", action="store_true", help="打印每条 SQL")
    return p.parse_args()


def main():
    args = parse_args()

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
    redo = args.redo
    print(f"待处理: {len(targets)} 张表  dry_run={args.dry_run}  "
          f"migrate_partitioned={migrate_partitioned}  redo={redo}")
    print()

    if args.dry_run:
        for table, time_col, chunk, seg, comp in targets:
            convert_table(None, table, time_col, chunk, seg, comp,
                          dry_run=True, verbose=False,
                          migrate_partitioned=migrate_partitioned, redo=redo)
        print("\n[dry-run] 未连接数据库，未执行任何操作。")
        return

    # 需要用户确认的高风险操作
    if (migrate_partitioned or redo) and not args.yes:
        conn_tmp = psycopg2.connect(get_dsn(args.env_file))
        to_confirm = []
        for table, *_ in targets:
            if not table_exists(conn_tmp, table):
                continue
            if redo and is_hypertable(conn_tmp, table) and table_exists(conn_tmp, f"{table}_pg_old"):
                to_confirm.append((table, "redo（从 _pg_old 重新迁移）"))
            elif migrate_partitioned and is_partitioned(conn_tmp, table):
                to_confirm.append((table, "首次迁移 PG 分区表"))
        conn_tmp.close()
        if to_confirm:
            print("以下表将被迁移（原表备份为 _pg_old，操作耗时）：")
            for t, reason in to_confirm:
                print(f"  • {t}  [{reason}]")
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
                               migrate_partitioned=migrate_partitioned, redo=redo)
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
