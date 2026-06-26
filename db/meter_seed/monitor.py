#!/usr/bin/env python3
"""监控 seed_meter_data 的执行进度（按设备数量）。

读取 pg_stat_activity 中正在执行的 SQL，
以及 d_load_voltage 中已提交的 DISTINCT mp_id 数量来推算进度。

用法：
  python3 monitor.py --start 2025-01-01 --end 2025-06-30
  python3 monitor.py --start 2025-01-01 --end 2025-06-30 --interval 3
"""
import argparse
import re
import sys
import time
from datetime import date

import psycopg2
import dbconfig

try:
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich import box
    RICH = True
except ImportError:
    RICH = False

_MP_RE  = re.compile(r"INSERT\s+INTO\s+(\w+)", re.IGNORECASE)
_VAL_RE = re.compile(r"mp_id[,\s]+\w+[,\s]+\w+[,\s]+\w+[,\s]+[\w.]+\)\s+VALUES\s+\([\d.]+,\s*([\d]+)", re.IGNORECASE)


def fetch_active_meter(cur):
    """返回正在写入的表名，以及尽力解析出的 mp_id（可能为 None）。"""
    cur.execute("""
        SELECT left(query, 500), EXTRACT(EPOCH FROM (now() - query_start)) * 1000
        FROM pg_stat_activity
        WHERE pid != pg_backend_pid()
          AND state = 'active'
          AND query ~* 'INSERT\\s+INTO\\s+d_'
        ORDER BY query_start
        LIMIT 1
    """)
    row = cur.fetchone()
    if not row:
        return None, None, None
    q, ms = row
    m = _MP_RE.search(q)
    table = m.group(1) if m else None
    # mp_id 在列表靠后，500 字符内可能看不到 VALUES 里的值，尽力解析
    mp_m = _VAL_RE.search(q)
    mp_id = mp_m.group(1) if mp_m else None
    return table, mp_id, float(ms or 0)


def fetch_progress(cur, start_d, end_d):
    """已提交的 DISTINCT mp_id 数（来自 d_load_voltage）。"""
    cur.execute(
        "SELECT COUNT(DISTINCT mp_id) FROM d_load_voltage "
        "WHERE data_date BETWEEN %s AND %s",
        (start_d, end_d),
    )
    return int(cur.fetchone()[0])


def fetch_total_meters(cur):
    cur.execute("SELECT COUNT(*) FROM c_meter")
    return int(cur.fetchone()[0])


# ── 渲染 ─────────────────────────────────────────────────────────────────────

def _bar(done, total, w=36):
    if not total:
        return "─" * w
    n = min(w, int(done / total * w))
    return "█" * n + "░" * (w - n)


def _hms(sec):
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _eta(elapsed, done, total):
    if done <= 0:
        return "--:--"
    rem = (total - done) * elapsed / done
    return _hms(rem)


def build_display(active_table, active_mp, active_ms,
                  done, total, start_d, end_d, elapsed):
    pct = done / total * 100 if total else 0.0
    bar = _bar(done, total)

    if not RICH:
        active_str = f"{active_table}" if active_table else "无活跃写入"
        if active_mp:
            active_str += f"  mp_id={active_mp}"
        return (
            f"进度: {done}/{total} meters  {pct:.1f}%  "
            f"ETA {_eta(elapsed, done, total)}  ⏱ {_hms(elapsed)}\n"
            f"当前: {active_str}"
        )

    # ── rich 面板 ─────────────────────────────────────────────────
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold cyan", no_wrap=True)
    grid.add_column()

    # 进度行
    grid.add_row(
        "设备进度",
        f"{bar}  [bold]{done:,}[/bold] / [bold]{total:,}[/bold]  "
        f"[yellow]{pct:.2f}%[/yellow]",
    )
    grid.add_row(
        "",
        f"⏱ 已用 [cyan]{_hms(elapsed)}[/cyan]   "
        f"ETA [cyan]{_eta(elapsed, done, total)}[/cyan]",
    )
    grid.add_row("", "")

    # 当前写入
    if active_table:
        mp_hint = f"  [dim]mp_id = {active_mp}[/dim]" if active_mp else ""
        grid.add_row(
            "当前写入",
            f"[bold]{active_table}[/bold]{mp_hint}  "
            f"[dim]{active_ms:.0f}ms[/dim]",
        )
    else:
        grid.add_row("当前写入", "[dim]无活跃 INSERT（meter 间提交间隙 / 已完成）[/dim]")

    grid.add_row(
        "范围",
        f"[dim]{start_d} ~ {end_d}[/dim]",
    )

    status = "[bold green]✓ 完成[/bold green]" if done >= total else "[bold]seed_meter_data 进度[/bold]"
    border = "green" if done >= total else "blue"
    return Panel(grid, title=status, border_style=border, padding=(0, 1))


# ── main ─────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start",    required=True, help="seed 的开始日期 YYYY-MM-DD")
    p.add_argument("--end",      required=True, help="seed 的结束日期 YYYY-MM-DD")
    p.add_argument("--interval", type=float, default=2.0, help="刷新间隔（秒），默认 2")
    p.add_argument("--env-file", help="db.env 路径")
    return p.parse_args()


def main():
    args   = parse_args()
    start_d = date.fromisoformat(args.start)
    end_d   = date.fromisoformat(args.end)

    conn = psycopg2.connect(dbconfig.get_dsn(args.env_file))
    conn.autocommit = True
    cur  = conn.cursor()

    total      = fetch_total_meters(cur)
    start_time = time.time()

    def poll():
        active_table, active_mp, active_ms = fetch_active_meter(cur)
        done = fetch_progress(cur, start_d, end_d)
        return active_table, active_mp, active_ms or 0.0, done

    if RICH:
        console = Console()
        with Live(console=console, refresh_per_second=4,
                  transient=False, vertical_overflow="visible") as live:
            try:
                while True:
                    at, amp, ams, done = poll()
                    live.update(build_display(
                        at, amp, ams, done, total,
                        start_d, end_d, time.time() - start_time,
                    ))
                    if done >= total:
                        time.sleep(0.5)
                        break
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                pass
    else:
        try:
            while True:
                at, amp, ams, done = poll()
                print(build_display(at, amp, ams, done, total,
                                    start_d, end_d, time.time() - start_time))
                if done >= total:
                    break
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
