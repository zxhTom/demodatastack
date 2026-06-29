#!/usr/bin/env python3
"""为 10 张事件日志表生成种子数据。

事件随机分布在给定时间范围内，每张表生成指定条数（--count）。
"""
import argparse
import random
import sys
from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras

import dbconfig

try:
    from rich.console import Console
    from rich.progress import (
        BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
        TaskProgressColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn,
    )
    RICH = True
except ImportError:
    RICH = False

ALL_TABLES = [
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

# (event_code, sub_event_code) 池，基于数据库现有数据
_EC = {
    "d_communication_event_log":       [(143, 0), (146, 0), (244, 0)],
    "d_config_modification_event_log": [(1, 0)],
    "d_disconnector_event_log":        [(170, 0), (171, 0), (172, 0), (173, 0)],
    "d_fraud_event_log":               [(150, 0), (151, 0), (155, 0), (158, 0)],
    "d_power_failure_event_log":       [(210, 0)],
    "d_power_quality_event_log":       [(220, 0), (221, 0), (222, 0), (223, 0)],
    "d_recharge_event_log":            [(201, 0), (202, 0), (205, 0), (206, 0),
                                        (207, 0), (208, 0), (214, 0), (226, 0),
                                        (239, 0), (252, 0)],
    "d_special_event_log":             [(187, 0), (188, 0), (201, 0), (202, 0),
                                        (203, 0), (209, 0), (210, 0), (301, 0),
                                        (302, 0), (303, 0), (329, 0), (341, 0)],
    "d_standard_event_log":            [(1, 0), (2, 0), (3, 0), (4, 0), (5, 0)],
}

_ALARM_CODES = ["E001", "E002", "E003", "W001", "W002", "I001"]
_DEVICE_TYPES = ["01", "02", "03"]
_DATA_STATUS = ["0", "1", "11", "10"]
_MONITORED_UNITS = ["V", "A", "kW"]
_DURATION_UNITS = ["s", "ms", "min"]


def _rand_ts(rng, start_dt, end_dt):
    delta = int((end_dt - start_dt).total_seconds())
    return start_dt + timedelta(seconds=rng.randint(0, max(0, delta - 1)))


def _col_date(rng, ts):
    return ts + timedelta(seconds=rng.randint(60, 28800))


# ── 各表行生成函数 ──────────────────────────────────────────────────────────

def _gen_alarm_event(rng, mp_id, ts):
    # content_id 由 seq_content_id 序列自动生成，不插入
    return {
        "device_id":    mp_id,
        "device_type":  rng.choice(_DEVICE_TYPES),
        "data_date":    ts,
        "register_no":  str(rng.randint(1, 200)),
        "alarm_code":   rng.choice(_ALARM_CODES),
        "alarm_time":   ts,
        "calendar_date": ts.strftime("%Y-%m-%d"),
        "ad_reset":     "0",
        "ar_reset":     "0",
        "mark_as_read": "0",
        "remark":       None,
    }


def _gen_base(rng, mp_id, ec, ts, seq):
    return {
        "mp_id":          mp_id,
        "event_code":     ec[0],
        "sub_event_code": ec[1],
        "data_date":      ts.date(),
        "event_time":     ts,
        "col_date":       _col_date(rng, ts),
        "data_source":    "01",
        "sequence_no":    seq,
    }


def _gen_config_modification(rng, meter_id, ec, ts, seq):
    row = _gen_base(rng, meter_id, ec, ts, seq)
    row["data_status"]     = rng.choice(_DATA_STATUS)
    row["partner_id"]      = None
    row["current_user_id"] = rng.choice(["1,admin", "1,cc", "1,2222", None])
    return row


def _gen_disconnector(rng, meter_id, ec, ts, seq):
    row = _gen_base(rng, meter_id, ec, ts, seq)
    row["monitored_value"] = round(rng.uniform(0, 500), 4)
    row["monitored_unit"]  = rng.choice(_MONITORED_UNITS)
    return row


def _gen_power_failure(rng, meter_id, ec, ts, seq):
    row = _gen_base(rng, meter_id, ec, ts, seq)
    row["duration_time"] = rng.randint(1, 3600)
    row["pf_num"]        = None
    row["pf_num_l1"]     = None
    row["pf_num_l2"]     = None
    row["pf_num_l3"]     = None
    return row


def _gen_power_quality(rng, meter_id, ec, ts, seq):
    row = _gen_base(rng, meter_id, ec, ts, seq)
    row["duration_value"] = round(rng.uniform(0.1, 300), 4)
    row["duration_unit"]  = rng.choice(_DURATION_UNITS)
    row["magnitude"]      = round(rng.uniform(0, 100), 4)
    return row


def _gen_recharge(rng, meter_id, ec, ts, seq):
    row = _gen_base(rng, meter_id, ec, ts, seq)
    row["recharge_amount"] = round(rng.uniform(10, 1000), 2)
    return row


_GENERATORS = {
    "d_alarm_event":                   None,            # 单独处理
    "d_communication_event_log":       _gen_base,
    "d_config_modification_event_log": _gen_config_modification,
    "d_disconnector_event_log":        _gen_disconnector,
    "d_fraud_event_log":               _gen_base,
    "d_power_failure_event_log":       _gen_power_failure,
    "d_power_quality_event_log":       _gen_power_quality,
    "d_recharge_event_log":            _gen_recharge,
    "d_special_event_log":             _gen_base,
    "d_standard_event_log":            _gen_base,
}


def build_rows(table, meters, count, start_dt, end_dt, seed):
    rng = random.Random(seed + hash(table) & 0xFFFFFFFF)
    rows = []
    seen_pks = set()
    attempts = 0
    max_attempts = count * 10

    while len(rows) < count and attempts < max_attempts:
        attempts += 1
        mp_id = rng.choice(meters)
        ts = _rand_ts(rng, start_dt, end_dt)
        seq = rng.randint(1, 9999)

        if table == "d_alarm_event":
            rows.append(_gen_alarm_event(rng, mp_id, ts))
            continue

        ec = rng.choice(_EC[table])
        gen = _GENERATORS[table]
        row = gen(rng, mp_id, ec, ts, seq)

        if table == "d_config_modification_event_log":
            pk = (mp_id, ts.date(), ts)
        else:
            pk = (mp_id, ec[0], ec[1], ts.date(), ts)

        if pk in seen_pks:
            continue
        seen_pks.add(pk)
        rows.append(row)

    if len(rows) < count:
        print(f"  [warn] {table}: 目标 {count} 条，实际生成 {len(rows)} 条（PK 碰撞导致不足）", file=sys.stderr)
    return rows


def fetch_meters(conn, meter_ids):
    """通过 r_mp(is_delete='01') 获取真实 mp_id，无有效 r_mp 记录的 meter 自动跳过。"""
    with conn.cursor() as cur:
        if meter_ids:
            cur.execute(
                """
                SELECT rmp.mp_id
                FROM c_meter cm
                JOIN r_mp rmp ON rmp.meter_id = cm.meter_id AND rmp.is_delete = '01'
                WHERE cm.meter_id = ANY(%s)
                ORDER BY rmp.mp_id
                """,
                (meter_ids,),
            )
        else:
            cur.execute(
                """
                SELECT rmp.mp_id
                FROM c_meter cm
                JOIN r_mp rmp ON rmp.meter_id = cm.meter_id AND rmp.is_delete = '01'
                ORDER BY rmp.mp_id
                """
            )
        return [int(r[0]) for r in cur.fetchall()]


def _make_sql(table, cols):
    col_list = ", ".join(cols)
    placeholders = "(" + ", ".join(["%s"] * len(cols)) + ")"
    return f"INSERT INTO {table} ({col_list}) VALUES {placeholders} ON CONFLICT DO NOTHING"


def insert_rows(conn, table, rows, batch_size):
    if not rows:
        return 0
    cols = sorted(rows[0].keys())
    values = [[r[c] for c in cols] for r in rows]
    sql = _make_sql(table, cols)
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(values), batch_size):
            chunk = values[i:i + batch_size]
            psycopg2.extras.execute_batch(cur, sql, chunk, page_size=len(chunk))
            total += len(chunk)
    return total


def mogrify_rows(conn, table, rows, batch_size):
    if not rows:
        return []
    cols = sorted(rows[0].keys())
    values = [[r[c] for c in cols] for r in rows]
    placeholders = "(" + ", ".join(["%s"] * len(cols)) + ")"
    conflict = "ON CONFLICT DO NOTHING"
    col_list = ", ".join(cols)
    statements = []
    with conn.cursor() as cur:
        for i in range(0, len(values), batch_size):
            chunk = values[i:i + batch_size]
            value_list = ", ".join(
                cur.mogrify(placeholders, row).decode("utf-8") for row in chunk
            )
            statements.append(
                f"INSERT INTO {table} ({col_list}) VALUES {value_list} {conflict};"
            )
    return statements


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    p.add_argument("--end",   required=True, help="结束日期 YYYY-MM-DD（含）")
    p.add_argument("--count", type=int, default=None,
                   help="每张表生成的总条数")
    p.add_argument("--count-per-day", type=int, default=None,
                   help="每张表每天生成的条数，与时间范围联动（和 --count 二选一，默认 10）")
    p.add_argument("--tables", help="逗号分隔的表名，默认全部 10 张")
    p.add_argument("--meters", help="逗号分隔的 meter_id，默认 c_meter 全部")
    p.add_argument("--batch-size", type=int, default=500)
    p.add_argument("--seed", type=int, default=42, help="随机种子（默认 42）")
    p.add_argument("--env-file", help="db.env 路径")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印预计行数，不连库写入")
    p.add_argument("--sql-out", help="将 INSERT 语句写入此文件，不直接写库（仍会连库读取 c_meter）")
    return p.parse_args()


def main():
    args = parse_args()
    start_dt = datetime.fromisoformat(args.start)
    end_dt   = datetime.fromisoformat(args.end) + timedelta(days=1)
    n_days   = (end_dt - start_dt).days

    if args.count is not None and args.count_per_day is not None:
        sys.exit("--count 和 --count-per-day 不能同时使用")
    if args.count is not None:
        count = args.count
    elif args.count_per_day is not None:
        count = args.count_per_day * n_days
    else:
        count = 10 * n_days  # 默认每天 10 条

    tables = set(args.tables.split(",")) if args.tables else set(ALL_TABLES)
    unknown = tables - set(ALL_TABLES)
    if unknown:
        sys.exit(f"未知表名: {unknown}")

    meter_ids = [int(x) for x in args.meters.split(",")] if args.meters else None

    if args.dry_run:
        n_meters = len(meter_ids) if meter_ids else "?"
        print(f"tables={sorted(tables)}")
        print(f"range={args.start}~{args.end} ({n_days}天)  count={count}/表  meters={n_meters}")
        print(f"预计总行数 = {len(tables)} x {count} = {len(tables) * count}")
        return

    dsn = dbconfig.get_dsn(args.env_file)
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    sql_file = open(args.sql_out, "w", encoding="utf-8") if args.sql_out else None
    try:
        meters = fetch_meters(conn, meter_ids)
        if not meters:
            sys.exit("没有匹配到任何 c_meter 记录")
        print(f"meters={len(meters)}  range={args.start}~{args.end} ({n_days}天)  count={count}/表  seed={args.seed}")
        if sql_file:
            print(f"SQL 输出模式：语句将写入 {args.sql_out}")

        sorted_tables = sorted(tables)

        def _run_tables(prog, tbl_task):
            for ti, table in enumerate(sorted_tables):
                if prog:
                    prog.update(tbl_task,
                                description=f"[cyan]{table}[/cyan]",
                                completed=ti)
                rows = build_rows(table, meters, count, start_dt, end_dt, args.seed)
                if sql_file:
                    for stmt in mogrify_rows(conn, table, rows, args.batch_size):
                        sql_file.write(stmt + "\n")
                    if not prog:
                        print(f"  {table}: {len(rows)} 行已写入 SQL 文件")
                else:
                    inserted = insert_rows(conn, table, rows, args.batch_size)
                    conn.commit()
                    if not prog:
                        print(f"  {table}: 生成 {len(rows)} 行，实际插入 {inserted} 行")
            if prog:
                prog.update(tbl_task, completed=len(sorted_tables),
                            description="[bold green]完成[/bold green]")

        if RICH and not sql_file:
            console = Console()
            with Progress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                BarColumn(bar_width=36),
                MofNCompleteColumn(),
                TaskProgressColumn(),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                console=console,
                transient=False,
            ) as prog:
                t_task = prog.add_task("[bold]表[/bold]", total=len(sorted_tables))
                _run_tables(prog, t_task)
        else:
            _run_tables(None, None)

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        if sql_file:
            sql_file.close()


if __name__ == "__main__":
    main()
