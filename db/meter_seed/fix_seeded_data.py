#!/usr/bin/env python3
"""修复旧版 seed_meter_data.py 写入的错误数据。

修复内容：
  1. profile_id: 1  →  40930000011
  2. mp_id:  meter_id（错误）  →  r_mp.mp_id（正确）
  3. tmnl_id: meter_id（错误）  →  r_mp.tmnl_id（正确）

识别标志：profile_id = 1（旧脚本写入的标记）。

用法：
  python3 fix_seeded_data.py --dry-run
  python3 fix_seeded_data.py
  python3 fix_seeded_data.py --tables d_load_voltage,d_read_curve
  python3 fix_seeded_data.py --chunk-days 3
"""
import argparse
import sys
from datetime import date, timedelta

import psycopg2
import psycopg2.extras
import dbconfig

try:
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import (
        BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
        TaskProgressColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn,
    )
    from rich.table import Table
    RICH = True
except ImportError:
    RICH = False

TABLES = [
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

NEW_PROFILE_ID = 40930000011

# 识别"错误行"的条件：t.mp_id 实际上是个 meter_id，r_mp 里存的才是真正的 mp_id。
# 用 rmp.meter_id = t.mp_id 反查，且 rmp.mp_id != t.mp_id 确认值确实需要修改。
# 不依赖 profile_id，因为修复过程中有些行已经写了 40930000011 但 mp_id 仍然错误。
_WRONG_COND = """
    JOIN r_mp rmp
      ON rmp.meter_id = t.mp_id
     AND rmp.is_delete = '01'
     AND rmp.mp_id    != t.mp_id
"""


def get_date_range(conn):
    mn, mx = date.max, date.min
    with conn.cursor() as cur:
        for t in TABLES:
            cur.execute(
                f"SELECT MIN(t.data_date), MAX(t.data_date) FROM {t} t {_WRONG_COND}"
            )
            row = cur.fetchone()
            if row[0]:
                mn = min(mn, row[0])
                mx = max(mx, row[1])
    return (mn, mx) if mn <= mx else (None, None)


def count_rows(conn, table, start, end):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM {table} t {_WRONG_COND} "
            "WHERE t.data_date BETWEEN %s AND %s",
            (start, end),
        )
        return cur.fetchone()[0]


def fix_chunk(conn, table, chunk_start, chunk_end):
    """识别：t.mp_id == meter_id（非真实 mp_id），用 r_mp 修正三个字段。"""
    sql = f"""
        UPDATE {table} t
        SET    mp_id      = rmp.mp_id,
               tmnl_id   = rmp.tmnl_id,
               profile_id = {NEW_PROFILE_ID}
        FROM   r_mp rmp
        WHERE  rmp.meter_id = t.mp_id
          AND  rmp.is_delete = '01'
          AND  rmp.mp_id    != t.mp_id
          AND  t.data_date BETWEEN %s AND %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (chunk_start, chunk_end))
        return cur.rowcount


def daterange_chunks(start, end, days):
    cur = start
    while cur <= end:
        yield cur, min(cur + timedelta(days=days - 1), end)
        cur += timedelta(days=days)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tables",      help="逗号分隔的表名，默认全部 12 张")
    p.add_argument("--chunk-days",  type=int, default=7,
                   help="每次 UPDATE 的日期区间长度（默认 7 天）")
    p.add_argument("--env-file",    help="db.env 路径")
    p.add_argument("--dry-run",     action="store_true",
                   help="只统计需修复行数，不执行 UPDATE")
    return p.parse_args()


def main():
    args = parse_args()
    tables = [t.strip() for t in args.tables.split(",")] if args.tables else TABLES
    unknown = set(tables) - set(TABLES)
    if unknown:
        sys.exit(f"未知表名: {unknown}")

    conn = psycopg2.connect(dbconfig.get_dsn(args.env_file))
    conn.autocommit = False

    # 获取需要修复的日期范围
    print("扫描数据范围…")
    start, end = get_date_range(conn)
    if not start:
        print("未找到 profile_id=1 的数据，无需修复。")
        conn.close()
        return

    n_chunks = sum(1 for _ in daterange_chunks(start, end, args.chunk_days))
    print(f"日期范围: {start} ~ {end}  ({(end - start).days + 1} 天，每块 {args.chunk_days} 天，共 {n_chunks} 块)")
    print(f"目标表:   {', '.join(tables)}")
    print()

    if args.dry_run:
        print("=== DRY RUN：统计各表需修复行数 ===")
        total = 0
        for t in tables:
            n = count_rows(conn, t, start, end)
            print(f"  {t}: {n:,}")
            total += n
        print(f"\n合计: {total:,} 行")
        conn.close()
        return

    # ── 执行修复 ─────────────────────────────────────────────────────────────
    if RICH:
        _run_rich(conn, tables, start, end, args.chunk_days, n_chunks)
    else:
        _run_plain(conn, tables, start, end, args.chunk_days)

    conn.close()


def _run_rich(conn, tables, start, end, chunk_days, n_chunks):
    console = Console()
    total_fixed = {t: 0 for t in tables}

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=32),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    )

    tbl_task  = progress.add_task("表", total=len(tables))
    chunk_task = progress.add_task("分块", total=n_chunks)

    with progress:
        for ti, table in enumerate(tables):
            progress.update(tbl_task,   description=f"[cyan]{table}[/cyan]", completed=ti)
            progress.update(chunk_task, description="分块", completed=0, total=n_chunks)

            table_fixed = 0
            for ci, (cs, ce) in enumerate(daterange_chunks(start, end, chunk_days)):
                progress.update(chunk_task,
                                description=f"[dim]{cs}~{ce}[/dim]",
                                completed=ci)
                try:
                    n = fix_chunk(conn, table, cs, ce)
                    conn.commit()
                    table_fixed += n
                except Exception as e:
                    conn.rollback()
                    console.print(f"[red]ERROR {table} {cs}~{ce}: {e}[/red]")
                    raise

            total_fixed[table] = table_fixed
            progress.update(chunk_task, completed=n_chunks)

        progress.update(tbl_task, completed=len(tables))

    # 汇总
    console.print("\n[bold green]=== 修复完成 ===[/bold green]")
    grand = 0
    for t in tables:
        n = total_fixed[t]
        grand += n
        console.print(f"  {t}: [cyan]{n:,}[/cyan] 行已更新")
    console.print(f"\n[bold]合计 {grand:,} 行[/bold]")


def _run_plain(conn, tables, start, end, chunk_days):
    grand = 0
    for table in tables:
        print(f"{table} …")
        table_fixed = 0
        for cs, ce in daterange_chunks(start, end, chunk_days):
            try:
                n = fix_chunk(conn, table, cs, ce)
                conn.commit()
                table_fixed += n
                print(f"  {cs}~{ce}: {n} 行")
            except Exception as e:
                conn.rollback()
                print(f"  ERROR {cs}~{ce}: {e}", file=sys.stderr)
                raise
        print(f"  小计: {table_fixed:,} 行")
        grand += table_fixed
    print(f"\n合计: {grand:,} 行已更新")


if __name__ == "__main__":
    main()
