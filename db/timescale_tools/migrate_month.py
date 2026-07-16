#!/usr/bin/env python3
"""把某个月的数据整体平移到另一个月（curve 超表专用，兼容压缩状态）。

场景：曲线表 ~71% 的数据都堆在 2025-06 这一个月，做窗口查询时 chunk 裁剪几乎
没效果。把 6 月的数据搬到别的月份，可以把数据摊开，或对齐到你要压测的时间窗口。

做的事：对 WHERE data_date ∈ [源月, 源月+1) 的每一行，把它的【分区列 + 所有时间戳列】
整体平移 N 个月（源月→目标月），其余列原样保留；然后把源月清空（可用 --keep-source
改成"复制不删源"）。TimescaleDB 2.11+ 支持对压缩块直接 DML，脚本会自动
解压涉及的块、搬完再重新压缩。

目标月如果已有数据，用 --on-conflict 控制主键冲突：
  skip  (兼容)：ON CONFLICT DO NOTHING —— 保留目标已有行，冲突的源行丢弃；
  merge (合并)：ON CONFLICT DO UPDATE —— 用源行的值覆盖目标已有行。

默认 --dry-run：只打印计划（涉及哪些块、多大、要跑的 SQL），不动库。
真正执行必须显式加 --yes 且去掉 --dry-run。
"""
import argparse
import configparser
import os
import signal
import sys
from datetime import date, datetime

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dbconfig import get_dsn

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tables.ini")

_stop = {"requested": False}


def _install_signal_handlers():
    def _handler(signum, _frame):
        name = signal.Signals(signum).name
        if _stop["requested"]:
            print(f"\n[!] 再次收到 {name}，立即强制退出（未提交的事务数据库会自动回滚）。", flush=True)
            os._exit(130)
        _stop["requested"] = True
        print(f"\n[!] 收到 {name}：当前这张表处理完再停，不会停在半路。再按一次立即退出。", flush=True)
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


# ── 配置 / 元数据 ────────────────────────────────────────────────────────────

def curve_tables_from_ini(path, group, tables_arg):
    parser = configparser.ConfigParser()
    if not parser.read(path, encoding="utf-8"):
        sys.exit(f"[错误] 读不到配置文件: {path}")
    if tables_arg:
        return [t.strip() for t in tables_arg.split(",") if t.strip()]
    picked = [s for s in parser.sections()
              if group == "all" or parser[s].get("group", "").strip() == group]
    if not picked:
        sys.exit(f"[错误] tables.ini 里没有 group={group} 的表")
    return picked


def month_start(s):
    """'2025-06' 或 '2025-06-01' → date(2025,6,1)。"""
    parts = s.split("-")
    if len(parts) < 2:
        sys.exit(f"[错误] 月份格式应为 YYYY-MM，收到: {s}")
    return date(int(parts[0]), int(parts[1]), 1)


def next_month(d):
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def months_between(src, tgt):
    return (tgt.year - src.year) * 12 + (tgt.month - src.month)


def month_days(d):
    return (next_month(d) - d).days


def table_meta(cur, table):
    cur.execute("""
        SELECT column_name, data_type, ordinal_position
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        ORDER BY ordinal_position
    """, (table,))
    cols = [(r[0], r[1]) for r in cur.fetchall()]
    if not cols:
        sys.exit(f"[错误] 表不存在或没有列: {table}")
    cur.execute("""
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = %s::regclass AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
    """, (table,))
    pk = [r[0] for r in cur.fetchall()]
    if not pk:
        sys.exit(f"[错误] {table} 没有主键，无法安全做冲突处理")
    cur.execute("""
        SELECT column_name FROM timescaledb_information.dimensions
        WHERE hypertable_name=%s AND dimension_type='Time'
        ORDER BY dimension_number LIMIT 1
    """, (table,))
    row = cur.fetchone()
    if not row:
        sys.exit(f"[错误] {table} 不是超表（没有时间维度），本脚本只处理超表")
    part_col = row[0]
    return {
        "cols": [c for c, _ in cols],
        "types": dict(cols),
        "pk": pk,
        "part_col": part_col,
    }


def chunks_overlapping(cur, table, start, end):
    cur.execute("""
        SELECT chunk_schema, chunk_name, range_start, range_end, is_compressed
        FROM timescaledb_information.chunks
        WHERE hypertable_name=%s AND range_start < %s AND range_end > %s
        ORDER BY range_start
    """, (table, end, start))
    return cur.fetchall()


def qname(sch, name):
    return '"{}"."{}"'.format(sch.replace('"', '""'), name.replace('"', '""'))


def chunk_after_bytes(cur, table):
    cur.execute("""
        SELECT chunk_name,
               coalesce(after_compression_total_bytes, before_compression_total_bytes, 0)
        FROM chunk_compression_stats(%s)
    """, (table,))
    return {r[0]: r[1] for r in cur.fetchall()}


# ── SQL 构造 ─────────────────────────────────────────────────────────────────

def shift_expr(col, dtype, n_months, days):
    if days is not None:
        delta = f"({col} + make_interval(days => {days}))"
    else:
        delta = f"({col} + make_interval(months => {n_months}))"
    return f"{delta}::date" if dtype == "date" else delta


def build_select_list(meta, shift_cols, n_months, days):
    out = []
    for c in meta["cols"]:
        if c in shift_cols:
            out.append(shift_expr(c, meta["types"][c], n_months, days))
        else:
            out.append(c)
    return out


def build_insert_sql(table, meta, select_list, on_conflict):
    collist = ", ".join(meta["cols"])
    sel = ", ".join(select_list)
    pcol = meta["part_col"]
    conflict_cols = ", ".join(meta["pk"])
    if on_conflict == "skip":
        conflict = f"ON CONFLICT ({conflict_cols}) DO NOTHING"
    else:
        nonpk = [c for c in meta["cols"] if c not in meta["pk"]]
        sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in nonpk)
        conflict = f"ON CONFLICT ({conflict_cols}) DO UPDATE SET {sets}"
    return (
        f"INSERT INTO {table} ({collist})\n"
        f"SELECT {sel}\n"
        f"FROM {table}\n"
        f"WHERE {pcol} >= %s AND {pcol} < %s\n"
        f"{conflict}"
    )


# ── 执行 ─────────────────────────────────────────────────────────────────────

def resolve_shift_cols(meta, arg):
    if arg and arg != "auto":
        want = [c.strip() for c in arg.split(",") if c.strip()]
        unknown = [c for c in want if c not in meta["cols"]]
        if unknown:
            sys.exit(f"[错误] --shift-cols 里有不存在的列: {unknown}")
        return want
    cols = [meta["part_col"]]
    cols += [c for c, t in meta["types"].items()
             if t.startswith("timestamp") and c != meta["part_col"]]
    return cols


def preflight_clamp_check(cur, table, part_col, src_start, src_end, tgt_start, days_mode):
    if days_mode:
        return
    cur.execute(f"SELECT max(extract(day from {part_col})) FROM {table} "
                f"WHERE {part_col} >= %s AND {part_col} < %s", (src_start, src_end))
    r = cur.fetchone()
    max_day = int(r[0]) if r and r[0] is not None else 0
    if max_day > month_days(tgt_start):
        sys.exit(
            f"[错误] {table}: 源月最大日期是 {max_day} 号，但目标月 {tgt_start:%Y-%m} 只有 "
            f"{month_days(tgt_start)} 天。按月平移会把 {month_days(tgt_start)+1}~{max_day} 号"
            f"挤到同一天造成主键自撞。请换一个 ≥{max_day} 天的目标月，或加 --shift-mode days"
            f"（按固定天数平移，不自撞，但可能有几天溢出到相邻月）。")


def migrate_table(conn, table, args, src_start, src_end, tgt_start, tgt_end, n_months, days):
    cur = conn.cursor()
    meta = table_meta(cur, table)
    shift_cols = resolve_shift_cols(meta, args.shift_cols)
    if meta["part_col"] not in shift_cols:
        sys.exit(f"[错误] {table}: 分区列 {meta['part_col']} 必须在平移列里，否则数据不会换块")

    select_list = build_select_list(meta, shift_cols, n_months, days)
    insert_sql = build_insert_sql(table, meta, select_list, args.on_conflict)
    pcol = meta["part_col"]

    src_chunks = chunks_overlapping(cur, table, src_start, src_end)
    tgt_chunks = chunks_overlapping(cur, table, tgt_start, tgt_end)
    sizes = chunk_after_bytes(cur, table)
    src_mb = sum(sizes.get(c[1], 0) for c in src_chunks) / 1e6

    print(f"\n{'='*70}\n表 {table}")
    print(f"  分区列={pcol}  主键=({', '.join(meta['pk'])})")
    print(f"  平移列={shift_cols}  平移={'%+d 天' % days if days is not None else '%+d 月' % n_months}")
    print(f"  源 {src_start:%Y-%m}: {len(src_chunks)} 块 ≈{src_mb:.0f}MB(压缩后)  "
          f"目标 {tgt_start:%Y-%m}: {len(tgt_chunks)} 块已存在")
    print(f"  冲突策略={args.on_conflict}({'DO NOTHING/保留目标' if args.on_conflict=='skip' else 'DO UPDATE/覆盖目标'})"
          f"  删源={'否(仅复制)' if args.keep_source else '是'}")

    if args.dry_run:
        print("  ── 将执行的 INSERT（按块分批，参数为每块与源月交集的子区间）──")
        for line in insert_sql.splitlines():
            print("    " + line)
        if not args.keep_source:
            print(f"    DELETE FROM {table} WHERE {pcol} >= <子区间起> AND {pcol} < <子区间止>")
        print("  [dry-run] 未改动数据库。")
        return

    preflight_clamp_check(cur, table, pcol, src_start, src_end, tgt_start, days is not None)

    # 目标月已压缩的块先解压，保证 ON CONFLICT 唯一性检查与插入正常
    for sch, name, rs, re, comp in tgt_chunks:
        if comp:
            print(f"  解压目标块 {name} …", flush=True)
            cur.execute("SELECT decompress_chunk(%s::regclass, true)", (qname(sch, name),))
            conn.commit()

    delete_sql = f"DELETE FROM {table} WHERE {pcol} >= %s AND {pcol} < %s"
    touched = []
    for sch, name, rs, re, comp in src_chunks:
        if _stop["requested"]:
            print("  [!] 收到停止信号，跳过剩余块。", flush=True)
            break
        lo = max(rs.date() if isinstance(rs, datetime) else rs, src_start)
        hi = min(re.date() if isinstance(re, datetime) else re, src_end)
        if comp:
            print(f"  解压源块 {name} …", flush=True)
            cur.execute("SELECT decompress_chunk(%s::regclass, true)", (qname(sch, name),))
            conn.commit()
        print(f"  搬运 {name} [{lo} .. {hi}) …", flush=True)
        cur.execute(insert_sql, (lo, hi))
        moved = cur.rowcount
        deleted = 0
        if not args.keep_source:
            cur.execute(delete_sql, (lo, hi))
            deleted = cur.rowcount
        conn.commit()
        print(f"    插入 {moved:,} 行，删除源 {deleted:,} 行。", flush=True)
        touched.append((sch, name))

    if not args.no_recompress:
        recompress = tgt_chunks + [(s, n, None, None, None) for s, n in touched]
        seen = set()
        for sch, name, *_ in recompress:
            if (sch, name) in seen:
                continue
            seen.add((sch, name))
            try:
                cur.execute("SELECT compress_chunk(%s::regclass, true)", (qname(sch, name),))
                conn.commit()
                print(f"  重新压缩 {name}", flush=True)
            except psycopg2.Error as e:
                conn.rollback()
                print(f"  [跳过压缩] {name}: {str(e).splitlines()[0]}", flush=True)
    print(f"  ✓ {table} 完成。")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source-month", required=True, help="源月份 YYYY-MM，例如 2025-06")
    p.add_argument("--target-month", required=True, help="目标月份 YYYY-MM，例如 2025-03")
    p.add_argument("--on-conflict", choices=["skip", "merge"], default="skip",
                   help="目标月已有同主键数据时：skip=兼容(保留目标) / merge=合并(源覆盖目标)")
    p.add_argument("--group", choices=["curve", "event", "log", "all"], default="curve",
                   help="按 tables.ini 的 group 批量处理（默认 curve）")
    p.add_argument("--tables", help="逗号分隔的表名，优先于 --group")
    p.add_argument("--shift-cols", default="auto",
                   help="要平移的列，逗号分隔；auto=分区列+所有时间戳列（data_time 这种时段字符串不动）")
    p.add_argument("--shift-mode", choices=["months", "days"], default="months",
                   help="months=按月平移(直观，2 月这种短月会拒绝) / days=按固定天数平移(不自撞，可能溢出几天)")
    p.add_argument("--keep-source", action="store_true", help="只复制不删源（源月保留）")
    p.add_argument("--no-recompress", action="store_true", help="结束后不重新压缩被动过的块")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不动库（推荐先跑一次）")
    p.add_argument("-y", "--yes", action="store_true", help="确认执行（去掉 --dry-run 时必须加）")
    p.add_argument("-c", "--config", default=DEFAULT_CONFIG, help=f"表清单（默认 {DEFAULT_CONFIG}）")
    p.add_argument("--env-file", help="数据库连接配置文件（默认同目录 db.env）")
    args = p.parse_args()

    src_start = month_start(args.source_month)
    tgt_start = month_start(args.target_month)
    src_end, tgt_end = next_month(src_start), next_month(tgt_start)
    n_months = months_between(src_start, tgt_start)
    if n_months == 0:
        sys.exit("[错误] 源月和目标月相同，无需搬运。")
    days = (tgt_start - src_start).days if args.shift_mode == "days" else None

    tables = curve_tables_from_ini(args.config, args.group, args.tables)

    print(f"源月 {src_start:%Y-%m} → 目标月 {tgt_start:%Y-%m}  "
          f"（{'%+d 天' % days if days is not None else '%+d 月' % n_months}）")
    print(f"表: {tables}")
    print(f"冲突={args.on_conflict}  模式={'DRY-RUN' if args.dry_run else '执行'}")

    if not args.dry_run and not args.yes:
        sys.exit("\n[中止] 这会改动真实数据（解压/搬运/删源/重压缩，量很大）。"
                 "确认无误请加 --yes；想先看计划请加 --dry-run。")

    _install_signal_handlers()
    dsn = get_dsn(args.env_file)
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    with conn.cursor() as c:
        c.execute("SET statement_timeout=0")
        conn.commit()

    try:
        for t in tables:
            if _stop["requested"]:
                print("\n[!] 已停止，剩余表未处理。", flush=True)
                break
            migrate_table(conn, t, args, src_start, src_end, tgt_start, tgt_end, n_months, days)
    finally:
        conn.close()
    print("\n全部完成。" if not args.dry_run else "\ndry-run 结束，未改动数据库。")


if __name__ == "__main__":
    main()
