#!/usr/bin/env python3
"""把 TimescaleDB 超表转回普通 PostgreSQL 表（timescale_migrate_event.py 的逆操作）。

流程（每张表）：
  1. 创建 {table}_plain_new（LIKE 超表 INCLUDING ALL，普通表）
  2. 全量复制数据（压缩 chunk 会被透明解压，数据不丢）
  3. 行数校验，两边不一致则中止、不动原表
  4. 超表重命名为 {table}_ts_old（备份保留），新普通表取原名

三组表（与正向脚本一致）：
  [event]  d_alarm_event、d_*_event_log 等 10 张事件表
  [curve]  d_load_*、d_read_curve*、d_demand_curve 等 12 张曲线表
  [log]    sys_fep_comm_log 等通讯日志表

用法：
  python3 timescale_revert_event.py --dry-run
  python3 timescale_revert_event.py                       # 全部 23 张
  python3 timescale_revert_event.py --group curve -y
  python3 timescale_revert_event.py --tables d_alarm_event,sys_fep_comm_log

确认无误后可手动清理备份：DROP TABLE {table}_ts_old;（压缩策略等后台任务随之删除）
"""
import argparse
import sys

import psycopg2

from dbconfig import get_dsn

EVENT_TABLES = [
    "d_alarm_event",
    "d_communication_event_log",
    "d_config_modification_event_log",
    "d_disconnector_event_log",
    "d_fraud_event_log",
    "d_power_failure_event_log",
    "d_power_quality_event_log",
    "d_recharge_event_log",
    "d_special_event_log",
    "d_standard_event_log",
]

CURVE_TABLES = [
    "d_load_voltage",
    "d_load_current",
    "d_load_power",
    "d_load_power_r",
    "d_load_power_v",
    "d_load_instant",
    "d_load_angle",
    "d_load_status",
    "d_read_curve",
    "d_read_curve_r",
    "d_read_curve_v",
    "d_demand_curve",
]

LOG_TABLES = [
    "sys_fep_comm_log",
]

ALL_TABLES = EVENT_TABLES + CURVE_TABLES + LOG_TABLES

BACKUP_SUFFIX = "_ts_old"
TMP_SUFFIX = "_plain_new"


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


def count_rows(conn, table):
    with conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM "{table}";')
        return cur.fetchone()[0]


def _exec(conn, desc, sql, verbose):
    if verbose:
        print(f"    SQL: {sql}")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    print(f"    ✓ {desc}")


def reown_shared_sequences(conn, table, old_table, verbose):
    """serial 列的序列仍归属旧表，改挂到新表上，避免 DROP 备份表时把序列带走。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.attname, s.relname
            FROM pg_attribute a
            JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            JOIN pg_depend dep ON dep.refobjid = ('public.' || quote_ident(%s))::regclass
                 AND dep.deptype = 'a' AND dep.classid = 'pg_class'::regclass
            JOIN pg_class s ON s.oid = dep.objid AND s.relkind = 'S'
            WHERE a.attrelid = ('public.' || quote_ident(%s))::regclass
              AND pg_get_expr(d.adbin, d.adrelid) LIKE 'nextval(''' || s.relname || '%%'
            """,
            (old_table, table),
        )
        rows = cur.fetchall()
    for col, seq in rows:
        _exec(conn, f"序列 {seq} 改挂到 {table}.{col}",
              f'ALTER SEQUENCE "{seq}" OWNED BY "{table}"."{col}";', verbose)


def revert_table(conn, table, dry_run, verbose):
    """返回 'skip' | 'ok' | 'error'"""
    tmp = f"{table}{TMP_SUFFIX}"
    bak = f"{table}{BACKUP_SUFFIX}"

    if dry_run:
        print(f"\n  [REVERT] {table} → 普通表（超表备份: {bak}）")
        for line in [
            f'DROP TABLE IF EXISTS "{tmp}";',
            f'CREATE TABLE "{tmp}" (LIKE "{table}" INCLUDING ALL);',
            f'INSERT INTO "{tmp}" SELECT * FROM "{table}";  -- 压缩数据透明解压',
            f'-- 行数校验：count({table}) == count({tmp})，不一致则中止',
            f'ALTER TABLE "{table}" RENAME TO "{bak}";',
            f'ALTER TABLE "{tmp}" RENAME TO "{table}";',
        ]:
            print(f"    -- {line}")
        return "ok"

    if not table_exists(conn, table):
        print(f"  [WARN]  {table}: 表不存在，跳过")
        return "skip"

    if not is_hypertable(conn, table):
        print(f"  [SKIP]  {table}: 已是普通表，无需转换")
        return "skip"

    if table_exists(conn, bak):
        print(f"  [ERROR] {table}: 备份名 {bak} 已被占用，请先处理（重命名或 DROP）后重试")
        return "error"

    print(f"\n  [REVERT] {table} → 普通表（超表备份: {bak}）")
    try:
        _exec(conn, "清理残留临时表", f'DROP TABLE IF EXISTS "{tmp}";', verbose)
        _exec(conn, f"按超表结构创建普通表 {tmp}",
              f'CREATE TABLE "{tmp}" (LIKE "{table}" INCLUDING ALL);', verbose)

        print(f"    复制数据 {table} → {tmp}（全量，压缩 chunk 自动解压，可能较慢）…", flush=True)
        with conn.cursor() as cur:
            cur.execute(f'INSERT INTO "{tmp}" SELECT * FROM "{table}";')
            inserted = cur.rowcount
        print(f"    ✓ 数据复制完成，{inserted:,} 行")

        src_n = count_rows(conn, table)
        dst_n = count_rows(conn, tmp)
        if src_n != dst_n:
            conn.rollback()
            print(f"  [ERROR] {table}: 行数不一致（超表 {src_n:,} ≠ 新表 {dst_n:,}），"
                  f"已回滚，原表未改动（复制期间可能有写入，请停写后重试）")
            return "error"
        print(f"    ✓ 行数校验通过：{src_n:,} 行")

        with conn.cursor() as cur:
            cur.execute(f'ALTER TABLE "{table}" RENAME TO "{bak}";')
            cur.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}";')
        conn.commit()
        print(f"    ✓ 重命名完成（超表备份为 {bak}）")

        reown_shared_sequences(conn, table, bak, verbose)
        print(f"  [OK]    {table}: 已转回普通表")
        return "ok"
    except psycopg2.Error as e:
        conn.rollback()
        print(f"  [ERROR] {table}: 转换失败 — {str(e).strip()}")
        print(f"          原超表未改动；临时表 {tmp} 如残留可手动 DROP 后重试")
        return "error"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--group", choices=["event", "curve", "log", "all"], default="all",
                   help="只处理指定分组（默认 all）")
    p.add_argument("--tables",   help="逗号分隔的表名，只处理这些表")
    p.add_argument("--env-file", help="db.env 路径")
    p.add_argument("--dry-run",  action="store_true", help="只打印 SQL，不执行")
    p.add_argument("-y", "--yes", action="store_true", help="跳过确认提示")
    p.add_argument("-v", "--verbose", action="store_true", help="打印每条 SQL")
    return p.parse_args()


def main():
    args = parse_args()

    if args.tables:
        name_set = {t.strip() for t in args.tables.split(",")}
        targets = [t for t in ALL_TABLES if t in name_set]
        unknown = name_set - set(ALL_TABLES)
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

    if args.dry_run:
        for table in targets:
            revert_table(None, table, dry_run=True, verbose=False)
        print("\n[dry-run] 未连接数据库，未执行任何操作。")
        return

    conn = psycopg2.connect(get_dsn(args.env_file))
    conn.autocommit = False

    if not args.yes:
        pending = [t for t in targets if table_exists(conn, t) and is_hypertable(conn, t)]
        conn.rollback()
        if not pending:
            print("没有需要转换的超表（均不存在或已是普通表）。")
            conn.close()
            return
        print("以下超表将被转回普通表（原超表重命名为 *_ts_old 保留）：")
        for t in pending:
            print(f"  • {t}")
        ans = input("\n确认继续？[y/N] ").strip().lower()
        if ans != "y":
            conn.close()
            sys.exit("已取消。")

    ok = skip = error = 0
    try:
        for table in targets:
            result = revert_table(conn, table, dry_run=False, verbose=args.verbose)
            if result == "ok":
                ok += 1
            elif result == "skip":
                skip += 1
            else:
                error += 1
    finally:
        conn.close()

    print(f"\n完成: 转换 {ok} 张，跳过 {skip} 张，失败 {error} 张")
    if ok:
        print("确认数据无误后，可手动 DROP 各 *_ts_old 备份表释放空间。")
    sys.exit(1 if error else 0)


if __name__ == "__main__":
    main()
