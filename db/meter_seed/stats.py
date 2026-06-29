#!/usr/bin/env python3
"""统计 seed_meter_data / seed_event_data 涉及的 22 张表的数据量与关键字段均值。

用法：
  python3 stats.py
  python3 stats.py --start 2025-01-01 --end 2025-12-31   # 只统计指定时间范围
  python3 stats.py --env-file db.env
"""
import argparse
import sys
from datetime import date

import psycopg2
import dbconfig

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
    RICH = True
except ImportError:
    RICH = False

# ── 表定义 ───────────────────────────────────────────────────────────────────

METER_TABLES = [
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

# 每张表要统计均值的关键字段 (label, column)
METER_AVG_COLS = {
    "d_load_voltage":  [("Ua(V)", "ua"), ("Ub(V)", "ub"), ("Uc(V)", "uc"),
                        ("Uab(V)", "cav12"), ("Ubc(V)", "cav23"), ("Uca(V)", "cav31")],
    "d_load_current":  [("Ia(A)", "ia"), ("Ib(A)", "ib"), ("Ic(A)", "ic")],
    "d_load_power":    [("AP(kW)", "ap"), ("RP(kvar)", "rp"),
                        ("AP_a", "ap_a"), ("AP_b", "ap_b"), ("AP_c", "ap_c")],
    "d_load_power_r":  [("PA(kW)", "pa"), ("PR(kvar)", "pr")],
    "d_load_power_v":  [("PV_p", "pv_p"), ("RV_p", "rv_p"), ("Net_AP", "net_ap")],
    "d_load_instant":  [("PF", "pf"), ("Freq(Hz)", "freq")],
    "d_load_angle":    [("Angle_a", "angle_a"), ("Angle_b", "angle_b"), ("Angle_c", "angle_c")],
    "d_load_status":   [],
    "d_read_curve":    [("PAP_e(kWh)", "pap_e"), ("PRP_e(kvarh)", "prp_e"),
                        ("PAP_r(kWh)", "pap_r")],
    "d_read_curve_r":  [("RAP_e(kWh)", "rap_e"), ("RRP_e(kvarh)", "rrp_e")],
    "d_read_curve_v":  [("PV_e", "pv_e"), ("RV_e", "rv_e")],
    "d_demand_curve":  [("L_PAP_d", "l_pap_d"), ("C_PAP_d", "c_pap_d"),
                        ("L_PRP_d", "l_prp_d"), ("C_PRP_d", "c_prp_d")],
}


# ── 查询函数 ─────────────────────────────────────────────────────────────────

def date_cond(start, end, col="data_date"):
    """返回 (where_clause, params)，start/end 均可为 None。"""
    if start and end:
        return f"{col} BETWEEN %s AND %s", (start, end)
    if start:
        return f"{col} >= %s", (start,)
    if end:
        return f"{col} <= %s", (end,)
    return "TRUE", ()


def query_meter_table(cur, table, start, end):
    cond, params = date_cond(start, end)
    # 基本信息
    cur.execute(
        f"SELECT COUNT(*), MIN(data_date), MAX(data_date), COUNT(DISTINCT mp_id) "
        f"FROM {table} WHERE {cond}",
        params,
    )
    cnt, mn, mx, n_mp = cur.fetchone()

    # 关键字段均值
    avg_cols = METER_AVG_COLS.get(table, [])
    avgs = {}
    if avg_cols and cnt:
        exprs = ", ".join(f"AVG({c})" for _, c in avg_cols)
        cur.execute(f"SELECT {exprs} FROM {table} WHERE {cond}", params)
        row = cur.fetchone()
        avgs = {label: row[i] for i, (label, _) in enumerate(avg_cols)}

    return {"count": cnt, "min_date": mn, "max_date": mx, "n_mp": n_mp, "avgs": avgs}


def query_event_table(cur, table, start, end):
    id_col = "device_id" if table == "d_alarm_event" else "mp_id"
    date_col = "data_date" if table != "d_alarm_event" else "data_date"
    cond, params = date_cond(start, end, col=date_col)

    cur.execute(
        f"SELECT COUNT(*), MIN({date_col}), MAX({date_col}), COUNT(DISTINCT {id_col}) "
        f"FROM {table} WHERE {cond}",
        params,
    )
    cnt, mn, mx, n_dev = cur.fetchone()

    # 每天平均条数
    n_days = (mx - mn).days + 1 if mn and mx else None
    avg_per_day = round(cnt / n_days, 1) if n_days else None

    # 事件码分布（前3）
    top_codes = []
    if cnt and table != "d_alarm_event":
        cur.execute(
            f"SELECT event_code, COUNT(*) FROM {table} WHERE {cond} "
            f"GROUP BY event_code ORDER BY 2 DESC LIMIT 3",
            params,
        )
        top_codes = cur.fetchall()
    elif cnt and table == "d_alarm_event":
        cur.execute(
            f"SELECT alarm_code, COUNT(*) FROM {table} WHERE {cond} "
            f"GROUP BY alarm_code ORDER BY 2 DESC LIMIT 3",
            params,
        )
        top_codes = cur.fetchall()

    return {
        "count": cnt, "min_date": mn, "max_date": mx,
        "n_dev": n_dev, "avg_per_day": avg_per_day, "top_codes": top_codes,
    }


# ── 展示 ─────────────────────────────────────────────────────────────────────

def fmt_num(v, decimals=2):
    if v is None:
        return "-"
    try:
        return f"{float(v):.{decimals}f}"
    except Exception:
        return str(v)


def print_meter_section(results):
    if RICH:
        console = Console()
        t = Table(title="⚡ Meter 模块（12 张表）", box=box.SIMPLE_HEAVY,
                  show_lines=True, header_style="bold cyan")
        t.add_column("表名",         style="bold", no_wrap=True)
        t.add_column("行数",          justify="right")
        t.add_column("日期范围",      no_wrap=True)
        t.add_column("设备数",        justify="right")
        t.add_column("关键均值", style="dim")

        for table, r in results.items():
            date_range = f"{r['min_date']} ~ {r['max_date']}" if r['min_date'] else "-"
            avgs_str = "  ".join(f"{k}={fmt_num(v)}" for k, v in r['avgs'].items()) or "—"
            t.add_row(
                table,
                f"{r['count']:,}" if r['count'] else "0",
                date_range,
                str(r['n_mp'] or 0),
                avgs_str,
            )

        # 汇总行
        total = sum(r['count'] or 0 for r in results.values())
        t.add_section()
        t.add_row("[bold]合计[/bold]", f"[bold]{total:,}[/bold]", "", "", "")
        console.print(t)
    else:
        print("\n=== Meter 模块（12 张表）===")
        for table, r in results.items():
            date_range = f"{r['min_date']} ~ {r['max_date']}" if r['min_date'] else "-"
            avgs_str = "  ".join(f"{k}={fmt_num(v)}" for k, v in r['avgs'].items()) or "—"
            print(f"  {table}: {r['count']:,} 行  {date_range}  devices={r['n_mp']}")
            if avgs_str != "—":
                print(f"    均值: {avgs_str}")
        total = sum(r['count'] or 0 for r in results.values())
        print(f"  合计: {total:,} 行")


def print_event_section(results):
    if RICH:
        console = Console()
        t = Table(title="📋 Event 模块（10 张表）", box=box.SIMPLE_HEAVY,
                  show_lines=True, header_style="bold magenta")
        t.add_column("表名",         style="bold", no_wrap=True)
        t.add_column("行数",          justify="right")
        t.add_column("日期范围",      no_wrap=True)
        t.add_column("设备数",        justify="right")
        t.add_column("日均条数",      justify="right")
        t.add_column("Top 事件码")

        for table, r in results.items():
            date_range = f"{r['min_date']} ~ {r['max_date']}" if r['min_date'] else "-"
            top = "  ".join(f"{c}({n})" for c, n in r['top_codes']) if r['top_codes'] else "—"
            t.add_row(
                table,
                f"{r['count']:,}" if r['count'] else "0",
                date_range,
                str(r['n_dev'] or 0),
                str(r['avg_per_day'] or "-"),
                top,
            )

        total = sum(r['count'] or 0 for r in results.values())
        t.add_section()
        t.add_row("[bold]合计[/bold]", f"[bold]{total:,}[/bold]", "", "", "", "")
        console.print(t)
    else:
        print("\n=== Event 模块（10 张表）===")
        for table, r in results.items():
            date_range = f"{r['min_date']} ~ {r['max_date']}" if r['min_date'] else "-"
            top = "  ".join(f"{c}({n})" for c, n in r['top_codes']) if r['top_codes'] else "—"
            print(f"  {table}: {r['count']:,} 行  {date_range}  devices={r['n_dev']}  日均={r['avg_per_day']}")
            if top != "—":
                print(f"    Top 事件码: {top}")
        total = sum(r['count'] or 0 for r in results.values())
        print(f"  合计: {total:,} 行")


# ── main ─────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start",    help="统计起始日期 YYYY-MM-DD（不填则全量）")
    p.add_argument("--end",      help="统计结束日期 YYYY-MM-DD（不填则全量）")
    p.add_argument("--env-file", help="db.env 路径")
    return p.parse_args()


def main():
    args = parse_args()
    start = date.fromisoformat(args.start) if args.start else None
    end   = date.fromisoformat(args.end)   if args.end   else None

    conn = psycopg2.connect(dbconfig.get_dsn(args.env_file))
    conn.set_session(readonly=True)
    cur = conn.cursor()

    if RICH:
        Console().print(f"\n[bold]数据库:[/bold] {dbconfig.get_dsn(args.env_file)[:60]}…")
        if start or end:
            Console().print(f"[bold]范围:[/bold] {start or '不限'} ~ {end or '不限'}\n")

    print("查询 Meter 模块…", flush=True)
    meter_results = {}
    for table in METER_TABLES:
        try:
            meter_results[table] = query_meter_table(cur, table, start, end)
        except Exception as e:
            meter_results[table] = {"count": None, "min_date": None, "max_date": None,
                                    "n_mp": None, "avgs": {}, "error": str(e)}

    print("查询 Event 模块…", flush=True)
    event_results = {}
    for table in EVENT_TABLES:
        try:
            event_results[table] = query_event_table(cur, table, start, end)
        except Exception as e:
            event_results[table] = {"count": None, "min_date": None, "max_date": None,
                                    "n_dev": None, "avg_per_day": None, "top_codes": [],
                                    "error": str(e)}

    cur.close()
    conn.close()

    print_meter_section(meter_results)
    print()
    print_event_section(event_results)

    grand = (sum(r.get('count') or 0 for r in meter_results.values()) +
             sum(r.get('count') or 0 for r in event_results.values()))
    if RICH:
        Console().print(f"\n[bold]全库合计: {grand:,} 行[/bold]")
    else:
        print(f"\n全库合计: {grand:,} 行")


if __name__ == "__main__":
    main()
