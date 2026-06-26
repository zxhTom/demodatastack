#!/usr/bin/env python3
"""为 c_meter 中的电能表生成 12 张 d_* 负荷/抄表曲线表的整天数据。

时间粒度固定为 15 分钟（每天 96 点），mp_id 取 meter_id（这批表没有
c_meter_mp_rela 映射记录）。各表里业务含义明确的字段（电压/电流/功率/
功率因数/累计电量寄存器等）按物理关系生成，少用的扩展字段留空，
与库里真实数据的留空方式一致。
"""
import argparse
import math
import random
import sys
from datetime import date, datetime, timedelta

import psycopg2
import psycopg2.extras

import dbconfig

PK = ("mp_id", "data_date", "data_time", "profile_id", "supply_id")
SLOT_MINUTES = 15
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES

GRID_TABLES = (
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
)


def tou_bucket(hour):
    if hour >= 23 or hour < 7:
        return "4"  # 谷
    if hour in (9, 10, 18, 19, 20):
        return "2"  # 峰
    return "3"  # 平


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


class Register:
    """带尖峰平谷(1-4)分时累计的电量寄存器，1 档(尖)不使用，留 0。"""

    def __init__(self):
        self.r = 0.0
        self.buckets = {"1": 0.0, "2": 0.0, "3": 0.0, "4": 0.0}

    def add(self, amount, bucket):
        self.r += amount
        self.buckets[bucket] += amount

    def row(self, amount, bucket, prefix):
        out = {f"{prefix}_r": round(self.r, 4), f"{prefix}_e": round(amount, 4)}
        for n in "1234":
            out[f"{prefix}_r{n}"] = round(self.buckets[n], 4)
            out[f"{prefix}_e{n}"] = round(amount, 4) if n == bucket else 0.0
        return out


class SimpleRegister:
    """无分时拆分的累计寄存器（rap/rrp/rp1/rp2/rp3/rp4）。"""

    def __init__(self):
        self.r = 0.0

    def add(self, amount):
        self.r += amount

    def row(self, amount, prefix):
        return {f"{prefix}_r": round(self.r, 4), f"{prefix}_e": round(amount, 4)}


class MeterState:
    def __init__(self, meter_id, org_no):
        self.meter_id = int(meter_id)
        self.org_no = org_no
        self.rng = random.Random(self.meter_id * 1_000_003 + 17)
        self.base_kw = self.rng.uniform(2.0, 18.0)
        self.pf_base = self.rng.uniform(0.93, 0.99)
        self.pap = Register()
        self.prp = Register()
        self.rap = SimpleRegister()
        self.rrp = SimpleRegister()
        self.rp1 = SimpleRegister()
        self.rp4 = SimpleRegister()
        self.rp2 = SimpleRegister()
        self.rp3 = SimpleRegister()
        self.pv = Register()
        self.rv = Register()
        self.abs_e = Register()
        self.max_demand = {}  # key -> running max for d_demand_curve c_*/*_d


def load_factor(minute_of_day):
    x = minute_of_day / 1440 * 2 * math.pi
    base = 0.55 + 0.25 * math.sin(x - math.pi / 2) + 0.15 * math.sin(2 * x)
    return clamp(base, 0.15, 1.0)


def build_slot(state, dt):
    rng = state.rng
    minute = dt.hour * 60 + dt.minute
    lf = load_factor(minute)
    kw = state.base_kw * lf * (1 + rng.uniform(-0.04, 0.04))
    pf = clamp(state.pf_base + rng.uniform(-0.01, 0.01), 0.85, 0.999)
    ang = math.acos(pf)

    phases = [kw / 3 * (1 + rng.uniform(-0.05, 0.05)) for _ in range(3)]
    ap_a, ap_b, ap_c = phases
    ap = ap_a + ap_b + ap_c
    rp_a, rp_b, rp_c = [w * math.tan(ang) for w in phases]
    rp = rp_a + rp_b + rp_c

    ua, ub, uc = [220 + rng.uniform(-3, 3) for _ in range(3)]
    ia, ib, ic = [
        (w * 1000) / (u * pf) if u else 0.0
        for w, u in zip(phases, (ua, ub, uc))
    ]

    pa = rng.uniform(0, kw * 0.01)  # 极小反向有功（漏电/计量噪声）
    pa_a = pa_b = pa_c = pa / 3
    pr = rp * rng.uniform(0.005, 0.02)
    pr_a = pr_b = pr_c = pr / 3

    freq = 50 + rng.uniform(-0.05, 0.05)

    return dict(
        ua=ua, ub=ub, uc=uc,
        ia=ia, ib=ib, ic=ic,
        pf=pf, ang=ang, freq=freq,
        ap=ap, ap_a=ap_a, ap_b=ap_b, ap_c=ap_c,
        rp=rp, rp_a=rp_a, rp_b=rp_b, rp_c=rp_c,
        pa=pa, pa_a=pa_a, pa_b=pa_b, pa_c=pa_c,
        pr=pr, pr_a=pr_a, pr_b=pr_b, pr_c=pr_c,
    )


def common(mp_id, org_no, dt, profile_id):
    return dict(
        mp_id=mp_id,
        data_date=dt.date(),
        data_time=dt.strftime("%H:%M"),
        task_id=None,
        data_src="01",
        tmnl_id=mp_id,
        org_no=org_no,
        coll_time=dt,
        freeze_time=dt,
        profile_id=profile_id,
        supply_id=0,
    )


def row_voltage(base, slot):
    base.update(slot)
    return dict(
        ua=round(slot["ua"], 4), ub=round(slot["ub"], 4), uc=round(slot["uc"], 4),
        cava=round(slot["ua"], 4), cavb=round(slot["ub"], 4), cavc=round(slot["uc"], 4),
        uab=round(slot["ua"] - slot["ub"], 4),
        st_v=0,
    )


def row_current(slot):
    return dict(
        ia=round(slot["ia"], 4), ib=round(slot["ib"], 4), ic=round(slot["ic"], 4),
        caca=round(slot["ia"], 4), cacb=round(slot["ib"], 4), cacc=round(slot["ic"], 4),
    )


def row_power(slot):
    return dict(
        ap=round(slot["ap"], 4), ap_a=round(slot["ap_a"], 4),
        ap_b=round(slot["ap_b"], 4), ap_c=round(slot["ap_c"], 4),
        rp=round(slot["rp"], 4), rp_a=round(slot["rp_a"], 4),
        rp_b=round(slot["rp_b"], 4), rp_c=round(slot["rp_c"], 4),
    )


def row_power_r(slot):
    return dict(
        pa=round(slot["pa"], 4), pa_a=round(slot["pa_a"], 4),
        pa_b=round(slot["pa_b"], 4), pa_c=round(slot["pa_c"], 4),
        pr=round(slot["pr"], 4), pr_a=round(slot["pr_a"], 4),
        pr_b=round(slot["pr_b"], 4), pr_c=round(slot["pr_c"], 4),
    )


def row_power_v(slot):
    net_ap = slot["ap"] - slot["pa"]
    return dict(
        pv_p=round(slot["ap"], 4), rv_p=round(slot["pa"], 4),
        abs_ap=round(slot["ap"], 4),
        abs_ap_a=round(slot["ap_a"], 4), abs_ap_b=round(slot["ap_b"], 4), abs_ap_c=round(slot["ap_c"], 4),
        net_ap=round(net_ap, 4),
        net_ap_a=round(slot["ap_a"] - slot["pa_a"], 4),
        net_ap_b=round(slot["ap_b"] - slot["pa_b"], 4),
        net_ap_c=round(slot["ap_c"] - slot["pa_c"], 4),
        pv_pa=round(slot["ap_a"], 4), pv_pb=round(slot["ap_b"], 4), pv_pc=round(slot["ap_c"], 4),
        rv_pa=round(slot["pa_a"], 4), rv_pb=round(slot["pa_b"], 4), rv_pc=round(slot["pa_c"], 4),
    )


def row_instant(slot):
    return dict(
        pf=round(slot["pf"], 4), pf_a=round(slot["pf"], 4), pf_b=round(slot["pf"], 4), pf_c=round(slot["pf"], 4),
        capf_a=round(slot["pf"], 4), capf_b=round(slot["pf"], 4), capf_c=round(slot["pf"], 4),
        freq=round(slot["freq"], 4), ca_freq=round(slot["freq"], 4),
        ot=round(25 + 15 * load_factor(0), 2),
    )


def row_angle(slot, rng):
    base120 = 120.0
    noise = lambda: rng.uniform(-0.3, 0.3)
    iv12 = base120 + noise()
    iv23 = base120 + noise()
    iv31 = base120 + noise()
    ang_deg = math.degrees(slot["ang"])
    return dict(
        iv12=round(iv12, 4), iv23=round(iv23, 4), iv31=round(iv31, 4),
        iv32=round(iv23, 4), iv13=round(iv31, 4), iv21=round(iv12, 4),
        ivcpa=round(ang_deg + noise(), 4), ivcpb=round(ang_deg + noise(), 4), ivcpc=round(ang_deg + noise(), 4),
        icv12=round(base120 + noise(), 4), icv23=round(base120 + noise(), 4), icv31=round(base120 + noise(), 4),
    )


def row_status():
    return dict(profile_status="00", sequence_no=-1)


def row_read_curve(state, slot, bucket):
    e_pap = slot["ap"] * 0.25
    e_prp = slot["rp"] * 0.25
    state.pap.add(e_pap, bucket)
    state.prp.add(e_prp, bucket)
    state.rp1.add(e_prp * 0.5)
    state.rp4.add(e_prp * 0.5)
    out = {}
    out.update(state.pap.row(e_pap, bucket, "pap"))
    out.update(state.prp.row(e_prp, bucket, "prp"))
    out.update(state.rp1.row(e_prp * 0.5, "rp1"))
    out.update(state.rp4.row(e_prp * 0.5, "rp4"))
    return out, e_pap, e_prp


def row_read_curve_r(state, slot, bucket):
    e_rap = slot["pa"] * 0.25
    e_rrp = slot["pr"] * 0.25
    state.rap.add(e_rap)
    state.rrp.add(e_rrp)
    state.rp2.add(e_rrp * 0.5)
    state.rp3.add(e_rrp * 0.5)
    out = {}
    out.update(state.rap.row(e_rap, "rap"))
    out.update(state.rrp.row(e_rrp, "rrp"))
    out.update(state.rp2.row(e_rrp * 0.5, "rp2"))
    out.update(state.rp3.row(e_rrp * 0.5, "rp3"))
    return out, e_rap, e_rrp


def row_read_curve_v(state, bucket, e_pap, e_rap):
    state.pv.add(e_pap, bucket)
    state.rv.add(e_rap, bucket)
    e_abs = e_pap + e_rap
    state.abs_e.add(e_abs, bucket)

    out = {}
    out.update(state.pv.row(e_pap, bucket, "pv"))
    out.update(state.rv.row(e_rap, bucket, "rv"))

    abs_row = state.abs_e.row(e_abs, bucket, "abs")
    out["abs"] = abs_row["abs_r"]
    out["abs_e"] = abs_row["abs_e"]
    for n in "1234":
        out[f"abs_{n}"] = abs_row[f"abs_r{n}"]
        out[f"abs_e{n}"] = abs_row[f"abs_e{n}"]

    out["rabs"] = round(state.rv.r, 4)
    for n in "1234":
        out[f"rabs_{n}"] = round(state.rv.buckets[n], 4)
    out["net"] = round(state.pv.r - state.rv.r, 4)
    for n in "1234":
        out[f"net_{n}"] = round(state.pv.buckets[n] - state.rv.buckets[n], 4)
    return out


def row_demand_curve(state, slot):
    l_vals = {
        "pap": slot["ap"], "prp": slot["rp"], "rap": slot["pa"], "rrp": slot["pr"],
        "pv": slot["ap"], "rv": slot["pa"], "abs": abs(slot["ap"]),
    }
    out = {}
    for key, val in l_vals.items():
        out[f"l_{key}_d"] = round(val, 4)
        prev = state.max_demand.get(key, 0.0)
        cur_max = max(prev, val)
        state.max_demand[key] = cur_max
        out[f"c_{key}_d"] = round(cur_max, 4)
        if key != "abs":
            out[f"{key}_d"] = round(cur_max, 4)
    for n in "1234":
        v = slot["rp"] * 0.25
        out[f"l_rp{n}_d"] = round(v, 4)
        prev = state.max_demand.get(f"rp{n}", 0.0)
        cur_max = max(prev, v)
        state.max_demand[f"rp{n}"] = cur_max
        out[f"c_rp{n}_d"] = round(cur_max, 4)
        out[f"rp{n}_d"] = round(cur_max, 4)
    out["st_d"] = 0
    out["abs_d"] = 0
    return out


def gen_rows_for_slot(state, dt, profile_id, tables):
    slot = build_slot(state, dt)
    bucket = tou_bucket(dt.hour)
    base_common = common(state.meter_id, state.org_no, dt, profile_id)
    rows = {}

    if "d_load_voltage" in tables:
        rows["d_load_voltage"] = {**base_common, **row_voltage({}, slot)}
    if "d_load_current" in tables:
        rows["d_load_current"] = {**base_common, **row_current(slot)}
    if "d_load_power" in tables:
        rows["d_load_power"] = {**base_common, **row_power(slot)}
    if "d_load_power_r" in tables:
        rows["d_load_power_r"] = {**base_common, **row_power_r(slot)}
    if "d_load_power_v" in tables:
        rows["d_load_power_v"] = {**base_common, **row_power_v(slot)}
    if "d_load_instant" in tables:
        rows["d_load_instant"] = {**base_common, **row_instant(slot)}
    if "d_load_angle" in tables:
        rows["d_load_angle"] = {**base_common, **row_angle(slot, state.rng)}
    if "d_load_status" in tables:
        rows["d_load_status"] = {**base_common, **row_status()}

    need_curve = {"d_read_curve", "d_read_curve_v"} & tables
    need_curve_r = {"d_read_curve_r", "d_read_curve_v"} & tables
    rc, e_pap, e_prp = (None, 0.0, 0.0)
    rcr, e_rap, e_rrp = (None, 0.0, 0.0)
    if need_curve:
        rc, e_pap, e_prp = row_read_curve(state, slot, bucket)
    if need_curve_r:
        rcr, e_rap, e_rrp = row_read_curve_r(state, slot, bucket)
    if "d_read_curve" in tables:
        rows["d_read_curve"] = {**base_common, **rc}
    if "d_read_curve_r" in tables:
        rows["d_read_curve_r"] = {**base_common, **rcr}
    if "d_read_curve_v" in tables:
        rows["d_read_curve_v"] = {**base_common, **row_read_curve_v(state, bucket, e_pap, e_rap)}
    if "d_demand_curve" in tables:
        rows["d_demand_curve"] = {**base_common, **row_demand_curve(state, slot)}

    return rows


def daterange(start, end):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def fetch_meters(conn, meter_ids):
    with conn.cursor() as cur:
        if meter_ids:
            cur.execute(
                "select meter_id, org_no from c_meter where meter_id = any(%s) order by meter_id",
                (meter_ids,),
            )
        else:
            cur.execute("select meter_id, org_no from c_meter order by meter_id")
        return [(int(meter_id), org_no) for meter_id, org_no in cur.fetchall()]


def fetch_baseline(conn, table, mp_ids, before_date):
    """rebuild/fill 时，从指定起始日期之前的历史数据里恢复累计电量基线。"""
    cols = {
        "d_read_curve": ["pap_r", "prp_r", "rp1_r", "rp4_r"],
        "d_read_curve_r": ["rap_r", "rrp_r", "rp2_r", "rp3_r"],
        "d_read_curve_v": ["pv_r", "rv_r"],
    }.get(table)
    if not cols or not mp_ids:
        return {}
    sql = (
        f"select mp_id, {', '.join(cols)} from {table} "
        f"where mp_id = any(%s) and data_date < %s "
        f"order by mp_id, data_date desc, data_time desc"
    )
    out = {}
    with conn.cursor() as cur:
        cur.execute(sql, (mp_ids, before_date))
        for row in cur.fetchall():
            mp_id = int(row[0])
            if mp_id in out:
                continue
            out[mp_id] = dict(zip(cols, row[1:]))
    return out


def apply_baseline(state, baseline):
    if not baseline:
        return
    if "pap_r" in baseline:
        state.pap.r = float(baseline["pap_r"] or 0)
    if "prp_r" in baseline:
        state.prp.r = float(baseline["prp_r"] or 0)
    if "rp1_r" in baseline:
        state.rp1.r = float(baseline["rp1_r"] or 0)
    if "rp4_r" in baseline:
        state.rp4.r = float(baseline["rp4_r"] or 0)
    if "rap_r" in baseline:
        state.rap.r = float(baseline["rap_r"] or 0)
    if "rrp_r" in baseline:
        state.rrp.r = float(baseline["rrp_r"] or 0)
    if "rp2_r" in baseline:
        state.rp2.r = float(baseline["rp2_r"] or 0)
    if "rp3_r" in baseline:
        state.rp3.r = float(baseline["rp3_r"] or 0)
    if "pv_r" in baseline:
        state.pv.r = float(baseline["pv_r"] or 0)
    if "rv_r" in baseline:
        state.rv.r = float(baseline["rv_r"] or 0)


def conflict_clause(cols, mode):
    if mode == "fill":
        return f"ON CONFLICT ({', '.join(PK)}) DO NOTHING"
    # overwrite / rebuild (rebuild already deleted the range, but keep upsert as safety net)
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in PK)
    return f"ON CONFLICT ({', '.join(PK)}) DO UPDATE SET {set_clause}"


def insert_sql(table, cols, mode):
    col_list = ", ".join(cols)
    placeholders = "(" + ", ".join(["%s"] * len(cols)) + ")"
    return f"INSERT INTO {table} ({col_list}) VALUES {placeholders} {conflict_clause(cols, mode)}"


def insert_rows(conn, table, rows, mode, batch_size):
    if not rows:
        return 0
    cols = sorted(rows[0].keys())
    values = [[r[c] for c in cols] for r in rows]
    sql = insert_sql(table, cols, mode)
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(values), batch_size):
            chunk = values[i:i + batch_size]
            psycopg2.extras.execute_batch(cur, sql, chunk, page_size=len(chunk))
            total += len(chunk)
    return total


def mogrify_insert_statements(conn, table, rows, mode, batch_size):
    """生成等价的 INSERT 语句文本，不连库执行，用于 --sql-out 模式。"""
    if not rows:
        return []
    cols = sorted(rows[0].keys())
    values = [[r[c] for c in cols] for r in rows]
    col_list = ", ".join(cols)
    placeholders = "(" + ", ".join(["%s"] * len(cols)) + ")"
    conflict = conflict_clause(cols, mode)

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


def delete_range(conn, table, mp_ids, start, end):
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM {table} WHERE mp_id = any(%s) AND data_date BETWEEN %s AND %s",
            (mp_ids, start, end),
        )
        return cur.rowcount


def mogrify_delete_statement(conn, table, mp_ids, start, end):
    """生成等价的 DELETE 语句文本，不连库执行，用于 --sql-out 模式。"""
    with conn.cursor() as cur:
        return cur.mogrify(
            f"DELETE FROM {table} WHERE mp_id = any(%s) AND data_date BETWEEN %s AND %s;",
            (mp_ids, start, end),
        ).decode("utf-8")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", help="开始日期 YYYY-MM-DD")
    p.add_argument("--end", help="结束日期 YYYY-MM-DD")
    p.add_argument("--days", type=int, help="时间跨度（天），配合 --start 或 --end 使用")
    p.add_argument("--mode", choices=["rebuild", "overwrite", "fill"], default="fill")
    p.add_argument("--meters", help="逗号分隔的 meter_id 列表，默认 c_meter 全部")
    p.add_argument("--tables", help="逗号分隔的表名，默认全部 12 张")
    p.add_argument("--profile-id", type=int, default=1)
    p.add_argument("--batch-size", type=int, default=2000)
    p.add_argument("--env-file", help="db.env 路径，默认脚本同目录下的 db.env")
    p.add_argument("--dry-run", action="store_true", help="只打印将要生成的行数，不连库写入")
    p.add_argument("--sql-out", help="将生成的 INSERT/DELETE 语句写入此文件，不直接执行写入（仍会连库读取 c_meter/基线数据）")
    p.add_argument("--yes", action="store_true", help="rebuild 模式会先删除范围内数据，需要这个开关确认（使用 --sql-out 时不需要，因为不会立即执行删除）")
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
    tables = set(args.tables.split(",")) if args.tables else set(GRID_TABLES)
    unknown = tables - set(GRID_TABLES)
    if unknown:
        sys.exit(f"未知表名: {unknown}")
    meter_ids = [int(x) for x in args.meters.split(",")] if args.meters else None

    if args.mode == "rebuild" and not args.dry_run and not args.sql_out and not args.yes:
        sys.exit("rebuild 模式会先删除该时间范围内的现有数据，请加 --yes 确认（或改用 --sql-out 只生成 SQL）")

    if args.dry_run:
        n_days = (end - start).days + 1
        n_meters = len(meter_ids) if meter_ids else "?"
        print(f"meters={n_meters} tables={sorted(tables)} range={start}~{end} ({n_days} 天) mode={args.mode}")
        if meter_ids:
            print(f"预计生成行数 = {len(meter_ids)} x {len(tables)} x {n_days} 天 x {SLOTS_PER_DAY} 点"
                  f" = {len(meter_ids) * len(tables) * n_days * SLOTS_PER_DAY}")
        else:
            print(f"预计生成行数 = ?(未指定 --meters，需连库) x {len(tables)} x {n_days} 天 x {SLOTS_PER_DAY} 点")
        return

    dsn = dbconfig.get_dsn(args.env_file)
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    sql_file = open(args.sql_out, "w", encoding="utf-8") if args.sql_out else None
    try:
        meters = fetch_meters(conn, meter_ids)
        if not meters:
            sys.exit("没有匹配到任何 c_meter 记录")
        mp_ids = [m[0] for m in meters]

        n_days = (end - start).days + 1
        print(f"meters={len(meters)} tables={sorted(tables)} range={start}~{end} ({n_days} 天) mode={args.mode}")

        if sql_file:
            print(f"SQL 输出模式：不会直接写库，语句将写入 {args.sql_out}")

        if args.mode == "rebuild":
            for t in tables:
                if sql_file:
                    stmt = mogrify_delete_statement(conn, t, mp_ids, start, end)
                    sql_file.write(stmt + "\n")
                    print(f"[rebuild] {t}: DELETE 语句已写入文件")
                else:
                    deleted = delete_range(conn, t, mp_ids, start, end)
                    print(f"[rebuild] {t}: deleted {deleted} rows in range")

        baselines = {
            t: fetch_baseline(conn, t, mp_ids, start)
            for t in ("d_read_curve", "d_read_curve_r", "d_read_curve_v")
            if t in tables
        }

        buffers = {t: [] for t in tables}
        total_written = {t: 0 for t in tables}

        def flush(t):
            if not buffers[t]:
                return
            if sql_file:
                for stmt in mogrify_insert_statements(conn, t, buffers[t], args.mode, args.batch_size):
                    sql_file.write(stmt + "\n")
                total_written[t] += len(buffers[t])
            else:
                total_written[t] += insert_rows(conn, t, buffers[t], args.mode, args.batch_size)
            buffers[t] = []

        for meter_id, org_no in meters:
            state = MeterState(meter_id, org_no)
            for t in ("d_read_curve", "d_read_curve_r", "d_read_curve_v"):
                apply_baseline(state, baselines.get(t, {}).get(meter_id))

            for day in daterange(start, end):
                for slot_idx in range(SLOTS_PER_DAY):
                    dt = datetime(day.year, day.month, day.day) + timedelta(minutes=slot_idx * SLOT_MINUTES)
                    rows = gen_rows_for_slot(state, dt, args.profile_id, tables)
                    for t, row in rows.items():
                        buffers[t].append(row)

            for t in tables:
                if len(buffers[t]) >= args.batch_size * 4:
                    flush(t)
            conn.commit()
            print(f"meter {meter_id} done")

        for t in tables:
            flush(t)
        conn.commit()

        for t in tables:
            if sql_file:
                print(f"{t}: {total_written[t]} 行已写入 SQL 文件")
            else:
                verb = "skipped existing / inserted missing among" if args.mode == "fill" else "written"
                print(f"{t}: {total_written[t]} rows processed ({verb})")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        if sql_file:
            sql_file.close()


if __name__ == "__main__":
    main()
