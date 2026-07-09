#!/usr/bin/env python3
"""为 sys_fep_comm_log（FEP 通信日志）生成种子数据。

comm_log_id 由 seq_sys_fep_comm_log_id 序列自动生成，不插入。事件按真实生产
数据里观察到的分布造：Load Profile 占绝大多数且几乎全部因为 UNIT_OFFLINE[2]
失败，Heart Beat/Push Object List 几乎必定成功，等等——具体权重/失败原因见
下面 _TEMPLATES（数值来自对 eco_ma.sys_fep_comm_log 真实数据的抽样统计，不是
拍脑袋编的）。

用法同 seed_event_data.py：
  python3 seed_log_data.py --start 2025-01-01 --days 7
  python3 seed_log_data.py --start 2025-01-01 --end 2025-01-31 --count-per-day 5000
  python3 seed_log_data.py --start 2025-01-01 --days 1 --dry-run
"""
import argparse
import random
import sys
from datetime import date, datetime, timedelta

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

TABLE = "sys_fep_comm_log"

# 模板字段：title/action_type/command_type/log_content/log_content_code/
# log_content_param 在真实数据里是强绑定的一组值（同一个 title 只对应一套），
# weight 是相对权重（不必加和为 100），fail_rate 是这个模板整体的失败率，
# fail_reasons 是失败时 failed_reason 的加权候选池，run_ms 是失败/成功各自的
# 耗时区间（毫秒，对应 run_time 列——失败多是设备离线检测，耗时短；成功是
# 真实通信往返，耗时明显更长）。
_TEMPLATES = [
    dict(title="Load Profile", action_type="get", command_type="52",
         log_content="Get the value of Load Profile", log_content_code=160131,
         log_content_param="grp{40930000002}", weight=780, fail_rate=0.999,
         fail_reasons=[("UNIT_OFFLINE[2]", 0.98), ("DECODE_FAILURE[12]", 0.02)],
         fail_ms=(2, 25000), ok_ms=(600, 6000)),
    dict(title="Auto Registration Capture", action_type="set", command_type="88",
         log_content="Set the value of Auto Registration Capture", log_content_code=160132,
         log_content_param="grp{60540000134}", weight=35, fail_rate=1.0,
         fail_reasons=[("UNIT_OFFLINE[2]", 0.84), ("INITIAL_FAILURE[23]", 0.16)],
         fail_ms=(2, 200), ok_ms=(1000, 15000)),
    dict(title="Load Profile 7", action_type="get", command_type="52",
         log_content="Get the value of Load Profile 7", log_content_code=160131,
         log_content_param="grp{40930000002}", weight=10, fail_rate=0.98,
         fail_reasons=[("UNIT_OFFLINE[2]", 0.98), ("TIMEOUT[-1]", 0.02)],
         fail_ms=(2, 400), ok_ms=(200, 2000)),
    dict(title="Heart Beat", action_type="notify", command_type="92",
         log_content="Notify the command of Heart Beat", log_content_code=160134,
         log_content_param="grp{60540000136}", weight=10, fail_rate=0.0,
         fail_reasons=[], fail_ms=(1, 1), ok_ms=(1, 10)),
    dict(title="Daily Billing Profile", action_type="get", command_type="52",
         log_content="Get the value of Daily Billing Profile", log_content_code=160131,
         log_content_param="grp{40930000002}", weight=7, fail_rate=0.985,
         fail_reasons=[("UNIT_OFFLINE[2]", 0.97), ("DECODE_FAILURE[12]", 0.03)],
         fail_ms=(2, 500), ok_ms=(100, 1500)),
    dict(title="modem", action_type="get", command_type="91",
         log_content="Get the value of modem", log_content_code=160131,
         log_content_param="grp{40930000002}", weight=2, fail_rate=0.026,
         fail_reasons=[("TIMEOUT[-1]", 0.6), ("UNIT_OFFLINE[2]", 0.4)],
         fail_ms=(2000, 70000), ok_ms=(500, 5000)),
    dict(title="Push Object List", action_type="notify", command_type="21",
         log_content="Notify the command of Push Object List", log_content_code=160134,
         log_content_param="grp{60540000136}", weight=2, fail_rate=0.0,
         fail_reasons=[], fail_ms=(1, 1), ok_ms=(1, 60)),
]

_DEVICE_TYPE = "01"          # 真实数据里绝大多数（>99.6%）device_type=01（电表）
_NAMED_OPERATORS = [13197, 13336, 13461, 33608, 31807, 13282, 14142, 4652, 13290]


def _rand_ts(rng, start_dt, end_dt):
    delta = int((end_dt - start_dt).total_seconds())
    return start_dt + timedelta(seconds=rng.randint(0, max(0, delta - 1)))


def _weighted_choice(rng, pairs):
    """pairs = [(value, weight), ...]，weight 不必归一化。"""
    total = sum(w for _, w in pairs)
    r = rng.uniform(0, total)
    upto = 0
    for value, w in pairs:
        upto += w
        if upto >= r:
            return value
    return pairs[-1][0]


def _gen_row(rng, device_id, tmpl, ts):
    fail = rng.random() < tmpl["fail_rate"]
    if fail:
        result = "fail"
        failed_reason = _weighted_choice(rng, tmpl["fail_reasons"]) if tmpl["fail_reasons"] else None
        run_ms = rng.randint(*tmpl["fail_ms"])
    else:
        result = "success"
        failed_reason = None
        run_ms = rng.randint(*tmpl["ok_ms"])

    oper_id = -1 if rng.random() < 0.97 else rng.choice(_NAMED_OPERATORS)

    return {
        "oper_id":           oper_id,
        "device_type":       _DEVICE_TYPE,
        "device_id":         device_id,
        "action_type":       tmpl["action_type"],
        "command_type":      tmpl["command_type"],
        "start_time":        ts,
        "end_time":          ts + timedelta(milliseconds=run_ms),
        "run_time":          run_ms,
        "result":            result,
        "failed_reason":     failed_reason,
        "title":             tmpl["title"],
        "log_content":       tmpl["log_content"],
        "log_content_code":  tmpl["log_content_code"],
        "log_content_param": tmpl["log_content_param"],
    }


def build_rows(devices, count, start_dt, end_dt, seed):
    rng = random.Random(seed)
    tmpl_pairs = [(t, t["weight"]) for t in _TEMPLATES]
    rows = []
    for _ in range(count):
        device_id = rng.choice(devices)
        tmpl = _weighted_choice(rng, tmpl_pairs)
        ts = _rand_ts(rng, start_dt, end_dt)
        rows.append(_gen_row(rng, device_id, tmpl, ts))
    return rows


def fetch_devices(conn, meter_ids):
    """通过 r_mp(is_delete='01') 获取真实 mp_id 作为 device_id，
    跟 seed_event_data.py 的 fetch_meters 是同一套查询（sys_fep_comm_log
    里 device_type=01 时的 device_id 实测就是 r_mp.mp_id）。"""
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


def _make_sql(cols):
    col_list = ", ".join(cols)
    placeholders = "(" + ", ".join(["%s"] * len(cols)) + ")"
    return f"INSERT INTO {TABLE} ({col_list}) VALUES {placeholders}"


def insert_rows(conn, rows, batch_size):
    if not rows:
        return 0
    cols = sorted(rows[0].keys())
    values = [[r[c] for c in cols] for r in rows]
    sql = _make_sql(cols)
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(values), batch_size):
            chunk = values[i:i + batch_size]
            psycopg2.extras.execute_batch(cur, sql, chunk, page_size=len(chunk))
            total += len(chunk)
    return total


def mogrify_rows(conn, rows, batch_size):
    if not rows:
        return []
    cols = sorted(rows[0].keys())
    values = [[r[c] for c in cols] for r in rows]
    placeholders = "(" + ", ".join(["%s"] * len(cols)) + ")"
    col_list = ", ".join(cols)
    statements = []
    with conn.cursor() as cur:
        for i in range(0, len(values), batch_size):
            chunk = values[i:i + batch_size]
            value_list = ", ".join(
                cur.mogrify(placeholders, row).decode("utf-8") for row in chunk
            )
            statements.append(f"INSERT INTO {TABLE} ({col_list}) VALUES {value_list};")
    return statements


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", help="开始日期 YYYY-MM-DD")
    p.add_argument("--end",   help="结束日期 YYYY-MM-DD（含）")
    p.add_argument("--days",  type=int, help="时间跨度（天），配合 --start 或 --end 使用（三者任选两个组合）")
    p.add_argument("--count", type=int, default=None, help="总生成条数")
    p.add_argument("--count-per-day", type=int, default=None,
                   help="每天生成的条数，与时间范围联动（和 --count 二选一，默认 2000/天——"
                        "真实生产量级约 17 万条/天，按需调大）")
    p.add_argument("--meters", help="逗号分隔的 meter_id，默认 c_meter 全部（生成的 device_id 取自这些表的 mp_id）")
    p.add_argument("--batch-size", type=int, default=500)
    p.add_argument("--seed", type=int, default=42, help="随机种子（默认 42）")
    p.add_argument("--env-file", help="db.env 路径")
    p.add_argument("--dry-run", action="store_true", help="只打印预计行数，不连库写入")
    p.add_argument("--sql-out", help="将 INSERT 语句写入此文件，不直接写库（仍会连库读取 c_meter/r_mp）")
    return p.parse_args()


def resolve_range(args):
    if args.start and args.end:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
    elif args.start and args.days:
        start = date.fromisoformat(args.start)
        end = start + timedelta(days=args.days - 1)
    elif args.end and args.days:
        end = date.fromisoformat(args.end)
        start = end - timedelta(days=args.days - 1)
    elif args.start:
        start = end = date.fromisoformat(args.start)
    elif args.end:
        start = end = date.fromisoformat(args.end)
    else:
        sys.exit("必须指定 --start/--end 中至少一个（可配合 --days）")
    if start > end:
        sys.exit(f"开始日期 {start} 晚于结束日期 {end}")
    return start, end


def main():
    args = parse_args()
    start, end = resolve_range(args)
    start_dt = datetime(start.year, start.month, start.day)
    end_dt   = datetime(end.year, end.month, end.day) + timedelta(days=1)
    n_days   = (end_dt - start_dt).days

    if args.count is not None and args.count_per_day is not None:
        sys.exit("--count 和 --count-per-day 不能同时使用")
    if args.count is not None:
        count = args.count
    elif args.count_per_day is not None:
        count = args.count_per_day * n_days
    else:
        count = 2000 * n_days  # 默认每天 2000 条

    meter_ids = [int(x) for x in args.meters.split(",")] if args.meters else None

    if args.dry_run:
        n_meters = len(meter_ids) if meter_ids else "?"
        print(f"table={TABLE}")
        print(f"range={start}~{end} ({n_days}天)  count={count}  meters={n_meters}")
        print(f"预计总行数 = {count}")
        return

    dsn = dbconfig.get_dsn(args.env_file)
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    sql_file = open(args.sql_out, "w", encoding="utf-8") if args.sql_out else None
    try:
        devices = fetch_devices(conn, meter_ids)
        if not devices:
            sys.exit("没有匹配到任何 c_meter/r_mp 记录")
        print(f"devices={len(devices)}  range={start}~{end} ({n_days}天)  count={count}  seed={args.seed}")
        if sql_file:
            print(f"SQL 输出模式：语句将写入 {args.sql_out}")

        rows = build_rows(devices, count, start_dt, end_dt, args.seed)

        def _run(prog, task):
            if sql_file:
                for i, stmt in enumerate(mogrify_rows(conn, rows, args.batch_size)):
                    sql_file.write(stmt + "\n")
                    if prog:
                        prog.update(task, completed=min((i + 1) * args.batch_size, len(rows)))
                if not prog:
                    print(f"  {TABLE}: {len(rows)} 行已写入 SQL 文件")
            else:
                inserted = 0
                for i in range(0, len(rows), args.batch_size):
                    chunk = rows[i:i + args.batch_size]
                    inserted += insert_rows(conn, chunk, args.batch_size)
                    conn.commit()
                    if prog:
                        prog.update(task, completed=min(i + len(chunk), len(rows)))
                if not prog:
                    print(f"  {TABLE}: 生成 {len(rows)} 行，实际插入 {inserted} 行")

        if RICH:
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
                task = prog.add_task(f"[cyan]{TABLE}[/cyan]", total=len(rows))
                _run(prog, task)
        else:
            _run(None, None)

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        if sql_file:
            sql_file.close()


if __name__ == "__main__":
    main()
