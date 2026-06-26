#!/usr/bin/env python3
"""通过读取 pg_stat_activity 监控 seed 脚本的执行进度。

直接连接 PostgreSQL，解析正在执行的 SQL，同时查询各表的 COUNT/MAX(data_date)。
不需要包装或修改 seed 脚本，seed 脚本独立运行即可。

用法：
  python3 monitor.py                                        # 自动探测
  python3 monitor.py --start 2025-01-01 --end 2025-06-30  # 指定范围（进度更准确）
  python3 monitor.py --start 2025-01-01 --end 2025-06-30 --interval 1
"""
import argparse
import re
import sys
import time
from datetime import date, datetime

import psycopg2
import psycopg2.extras
import dbconfig

try:
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich import box
    RICH = True
except ImportError:
    RICH = False

# ── 表分组 ──────────────────────────────────────────────────────────────────

METER_TABLES = [
    "d_load_voltage", "d_load_current", "d_load_power", "d_load_power_r",
    "d_load_power_v", "d_load_instant", "d_load_angle", "d_load_status",
    "d_read_curve",   "d_read_curve_r", "d_read_curve_v", "d_demand_curve",
]
EVENT_TABLES = [
    "d_alarm_event", "d_communication_event_log", "d_config_modification_event_log",
    "d_disconnector_event_log", "d_fraud_event_log", "d_power_failure_event_log",
    "d_power_quality_event_log", "d_recharge_event_log", "d_special_event_log",
    "d_standard_event_log",
]
ALL_TABLES  = METER_TABLES + EVENT_TABLES
METER_SET   = set(METER_TABLES)
EVENT_SET   = set(EVENT_TABLES)
SLOTS_DAY   = 96

# ── DB 查询 ──────────────────────────────────────────────────────────────────

_INSERT_RE = re.compile(r'INSERT\s+INTO\s+(\w+)', re.IGNORECASE)
_DELETE_RE = re.compile(r'DELETE\s+FROM\s+(\w+)',  re.IGNORECASE)


def fetch_active(cur):
    """返回 seed 相关的活跃 SQL 列表。"""
    cur.execute("""
        SELECT pid, state,
               left(query, 200)                           AS q,
               EXTRACT(EPOCH FROM (now()-query_start))*1000 AS ms
        FROM pg_stat_activity
        WHERE pid != pg_backend_pid()
          AND state NOT IN ('idle')
          AND (query ~* 'INSERT\\s+INTO\\s+d_'
            OR query ~* 'DELETE\\s+FROM\\s+d_')
        ORDER BY query_start
    """)
    rows = []
    for pid, state, q, ms in cur.fetchall():
        m = _INSERT_RE.search(q) or _DELETE_RE.search(q)
        if not m:
            continue
        tbl = m.group(1)
        if tbl in METER_SET or tbl in EVENT_SET:
            rows.append({
                "pid":   pid,
                "state": state,
                "table": tbl,
                "ms":    float(ms or 0),
                "op":    "INSERT" if _INSERT_RE.search(q) else "DELETE",
                "q":     q,
            })
    return rows


def fetch_table_stats(cur, tables, start_d=None, end_d=None):
    """查各表当前 COUNT / MAX(data_date)。"""
    stats = {}
    for t in tables:
        try:
            date_col = "alarm_time::date" if t == "d_alarm_event" else "data_date"
            if start_d and end_d:
                cur.execute(
                    f"SELECT COUNT(*), MAX({date_col}), MIN({date_col}) "
                    f"FROM {t} WHERE {date_col} BETWEEN %s AND %s",
                    (start_d, end_d),
                )
            else:
                cur.execute(
                    f"SELECT COUNT(*), MAX({date_col}), MIN({date_col}) FROM {t}"
                )
            cnt, mx, mn = cur.fetchone()
            stats[t] = {"count": int(cnt or 0), "max_date": mx, "min_date": mn}
        except Exception as e:
            stats[t] = {"count": 0, "max_date": None, "min_date": None, "err": str(e)}
    return stats


def fetch_meter_count(cur):
    cur.execute("SELECT COUNT(*) FROM c_meter")
    return int(cur.fetchone()[0])

# ── 渲染工具 ─────────────────────────────────────────────────────────────────

def _bar(done, total, w=24):
    if not total:
        return "─" * w
    n = min(w, int(done / total * w))
    return "█" * n + "░" * (w - n)


def _pct(done, total):
    return done / total * 100 if total else 0.0


def _eta(elapsed, done, total):
    if done <= 0 or elapsed <= 0:
        return "--:--"
    rem = (total - done) * elapsed / done
    m, s = divmod(int(rem), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _hms(sec):
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _fmt_ms(ms):
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms/1000:.1f}s"

# ── 构建显示面板 ──────────────────────────────────────────────────────────────

def build_panel(active, meter_stats, event_stats,
                start_d, end_d, total_meters, elapsed):
    if not RICH:
        return _plain_text(active, meter_stats, event_stats, elapsed)

    # ── 活跃 SQL 区 ────────────────────────────────────────────────
    act_tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    act_tbl.add_column(style="bold", no_wrap=True, width=10)
    act_tbl.add_column(style="cyan", no_wrap=True, width=36)
    act_tbl.add_column(style="dim",  no_wrap=True, width=8)
    act_tbl.add_column(style="yellow", no_wrap=True)

    if active:
        for a in active:
            act_tbl.add_row(
                a["op"],
                a["table"],
                _fmt_ms(a["ms"]),
                f"PID {a['pid']}  {a['state']}",
            )
    else:
        act_tbl.add_row("", "[dim]无活跃 seed SQL（seed 可能已完成或处于提交间隙）[/dim]", "", "")

    # ── 检测当前运行类型 ────────────────────────────────────────────
    active_tables = {a["table"] for a in active}
    show_meter = bool(active_tables & METER_SET) or bool(meter_stats)
    show_event = bool(active_tables & EVENT_SET) or bool(event_stats)

    sections = []

    # ── Meter Seed 表格 ────────────────────────────────────────────
    if show_meter and meter_stats:
        n_days = (end_d - start_d).days + 1 if (start_d and end_d) else 0
        expected_per = total_meters * n_days * SLOTS_DAY if (total_meters and n_days) else 0

        total_done = sum(s["count"] for s in meter_stats.values())
        total_exp  = expected_per * len(METER_TABLES)

        tbl = Table(
            title=f"[bold blue]Meter Seed[/bold blue]"
                  + (f"  {start_d} ~ {end_d}" if start_d else ""),
            box=box.SIMPLE_HEAD, show_header=True, padding=(0, 1),
        )
        tbl.add_column("表名",        style="cyan",  no_wrap=True, width=24)
        tbl.add_column("已插入",      justify="right", width=10)
        tbl.add_column("预计总量",    justify="right", width=10)
        tbl.add_column("进度",        width=28)
        tbl.add_column("最新日期",    width=12)
        tbl.add_column("状态",        width=8)

        for t in METER_TABLES:
            s   = meter_stats.get(t, {})
            cnt = s.get("count", 0)
            mx  = s.get("max_date")
            err = s.get("err")
            is_active = t in active_tables

            if err:
                tbl.add_row(t, "─", "─", "[red]error[/red]", "─", "")
                continue

            if expected_per:
                bar = _bar(cnt, expected_per)
                pct = f"{_pct(cnt, expected_per):.1f}%"
            else:
                bar = "─" * 24
                pct = ""

            status = "[bold green]写入中[/bold green]" if is_active else (
                     "[green]完成[/green]"             if cnt >= expected_per > 0 else
                     "[dim]等待[/dim]")

            tbl.add_row(
                t,
                f"{cnt:,}",
                f"{expected_per:,}" if expected_per else "─",
                f"{bar} {pct}",
                str(mx) if mx else "─",
                status,
            )

        # 整体汇总行
        if total_exp:
            overall_bar = _bar(total_done, total_exp)
            overall_pct = _pct(total_done, total_exp)
            tbl.add_row(
                "[bold]整体[/bold]",
                f"[bold]{total_done:,}[/bold]",
                f"[bold]{total_exp:,}[/bold]",
                f"{overall_bar} [bold yellow]{overall_pct:.1f}%[/bold yellow]"
                f"  ETA {_eta(elapsed, total_done, total_exp)}",
                "",
                "",
            )
        sections.append(tbl)

    # ── Event Seed 表格 ────────────────────────────────────────────
    if show_event and event_stats:
        tbl = Table(
            title=f"[bold green]Event Seed[/bold green]"
                  + (f"  {start_d} ~ {end_d}" if start_d else ""),
            box=box.SIMPLE_HEAD, show_header=True, padding=(0, 1),
        )
        tbl.add_column("表名",     style="cyan",  no_wrap=True, width=36)
        tbl.add_column("已插入",   justify="right", width=10)
        tbl.add_column("最新日期", width=12)
        tbl.add_column("状态",     width=8)

        for t in EVENT_TABLES:
            s   = event_stats.get(t, {})
            cnt = s.get("count", 0)
            mx  = s.get("max_date")
            is_active = t in active_tables
            status = "[bold green]写入中[/bold green]" if is_active else (
                     "[dim]等待/完成[/dim]")
            tbl.add_row(t, f"{cnt:,}", str(mx) if mx else "─", status)

        sections.append(tbl)

    # ── 组合 ───────────────────────────────────────────────────────
    from rich.columns import Columns
    from rich import get_console

    top = Table.grid(padding=(0, 0))
    top.add_row(Panel(act_tbl, title="当前活跃 SQL", border_style="dim", padding=(0, 1)))
    for s in sections:
        top.add_row(s)

    footer = (f"[dim]⏱ {_hms(elapsed)}  "
              f"刷新中...  数据库: {dbconfig.load_config()['DB_NAME']} "
              f"@ {dbconfig.load_config()['DB_HOST']}:{dbconfig.load_config()['DB_PORT']}[/dim]")
    top.add_row(Text.from_markup(footer))

    return Panel(top, title="[bold]PostgreSQL Seed 进度监控[/bold]", border_style="blue")


def _plain_text(active, meter_stats, event_stats, elapsed):
    lines = [f"⏱ {_hms(elapsed)}"]
    if active:
        for a in active:
            lines.append(f"  {a['op']} {a['table']}  {_fmt_ms(a['ms'])}")
    for t, s in {**meter_stats, **event_stats}.items():
        lines.append(f"  {t}: count={s['count']}  max_date={s.get('max_date')}")
    return "\n".join(lines)

# ── main ─────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start",    help="seed 的开始日期 YYYY-MM-DD（可选，让进度百分比更准）")
    p.add_argument("--end",      help="seed 的结束日期 YYYY-MM-DD（可选）")
    p.add_argument("--type",     choices=["meter", "event", "auto"], default="auto")
    p.add_argument("--interval", type=float, default=2.0, help="刷新间隔（秒），默认 2")
    p.add_argument("--env-file", help="db.env 路径")
    return p.parse_args()


def main():
    args = parse_args()
    start_d = date.fromisoformat(args.start) if args.start else None
    end_d   = date.fromisoformat(args.end)   if args.end   else None

    dsn  = dbconfig.get_dsn(args.env_file)
    conn = psycopg2.connect(dsn)
    conn.autocommit = True          # 只读监控，不需要事务

    cur = conn.cursor()
    total_meters = fetch_meter_count(cur)
    start_time   = time.time()

    if RICH:
        console = Console()
        with Live(console=console, refresh_per_second=2,
                  transient=False, vertical_overflow="visible") as live:
            try:
                while True:
                    active = fetch_active(cur)

                    active_tables = {a["table"] for a in active}
                    need_meter = (args.type in ("meter", "auto") and
                                  (active_tables & METER_SET or args.type == "meter"))
                    need_event = (args.type in ("event", "auto") and
                                  (active_tables & EVENT_SET or args.type == "event"))

                    meter_stats = fetch_table_stats(cur, METER_TABLES, start_d, end_d) if need_meter else {}
                    event_stats = fetch_table_stats(cur, EVENT_TABLES, start_d, end_d) if need_event else {}

                    live.update(build_panel(
                        active, meter_stats, event_stats,
                        start_d, end_d, total_meters,
                        time.time() - start_time,
                    ))
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                pass
    else:
        try:
            while True:
                active       = fetch_active(cur)
                meter_stats  = fetch_table_stats(cur, METER_TABLES, start_d, end_d)
                event_stats  = fetch_table_stats(cur, EVENT_TABLES, start_d, end_d)
                print(_plain_text(active, meter_stats, event_stats,
                                  time.time() - start_time))
                print()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
