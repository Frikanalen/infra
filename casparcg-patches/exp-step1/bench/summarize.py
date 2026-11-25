#!/usr/bin/env python3
"""Summarise a run-matrix output directory per run and per mode (stdlib only).

  summarize.py OUT_DIR [--stall-ms 30] [--csv OUT_DIR/summary.csv]

Per run it reads:
  runs/<id>/diag.log   'S' lines (per-second summaries) and 'E' event headers
  runs/<id>/diag.csv   per-callback rows, if present: percentiles of every *_us column
  runs/<id>/cap-*.analysis.json   from capture-analyse.sh
  runs/<id>/pidstat.txt           mean %CPU of the casparcg process (pidstat -h format)
and prints one row per run plus a per-mode aggregate (warm-up runs excluded).
Rates are normalised per hour so runs of different lengths compare.
"""
import argparse
import csv
import glob
import json
import os
import re
import statistics
import sys
from datetime import datetime

KV = re.compile(r"(\w+)=(\S+)")
EVT = re.compile(r" E fr=\d+ (\S+)")


def pct(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def ms(v):
    try:
        return float(v.rstrip("ms").split("..")[-1])
    except ValueError:
        return None


def parse_diag(path, stall_ms):
    r = {"seconds": 0, "late": 0, "drop": 0, "flush": 0, "cb_max_over": 0, "pop_max_over": 0,
         "ia_over": 0, "cb_max": [], "events": {}}
    if not os.path.exists(path):
        return None
    with open(path, errors="replace") as fh:
        for line in fh:
            if " S " in line:
                kv = dict(KV.findall(line))
                r["seconds"] += 1
                res = kv.get("res", "")
                for part in res.split(","):
                    if ":" in part:
                        k, v = part.split(":", 1)
                        if k in ("late", "drop", "flush") and v.isdigit():
                            r[k] += int(v)
                cb = ms(kv.get("cb_max", ""))
                if cb is not None:
                    r["cb_max"].append(cb)
                    r["cb_max_over"] += cb > stall_ms
                pm = ms(kv.get("pop_max", ""))
                if pm is not None:
                    r["pop_max_over"] += pm > stall_ms
                ia = ms(kv.get("ia", ""))
                if ia is not None:
                    r["ia_over"] += ia > stall_ms
            else:
                m = EVT.search(line)
                if m:
                    name = re.sub(r"_-?\d.*$", "", m.group(1))
                    r["events"][name] = r["events"].get(name, 0) + 1
    return r


def parse_csv(path, stall_ms):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    cols = {}
    rows = 0
    with open(path, newline="", errors="replace") as fh:
        rd = csv.reader(fh)
        header = None
        for row in rd:
            if header is None or (row and row[0] == "wall_ms"):
                if row and row[0] == "wall_ms":
                    header = row
                    continue
                if header is None:
                    continue  # slice started mid-file before a header: skip until we know columns
            rows += 1
            for i, name in enumerate(header):
                if name.endswith("_us") and i < len(row):
                    try:
                        cols.setdefault(name, []).append(float(row[i]))
                    except ValueError:
                        pass
    if not rows:
        return None
    out = {"rows": rows}
    for name, xs in cols.items():
        out[name] = {"p50": pct(xs, 50), "p99": pct(xs, 99), "p999": pct(xs, 99.9), "max": max(xs),
                     "over": sum(1 for x in xs if x > stall_ms * 1000)}
    return out


def parse_pidstat(path):
    if not os.path.exists(path):
        return None
    cpu, idx, tid = [], None, None
    with open(path, errors="replace") as fh:
        for line in fh:
            if line.startswith("#"):
                hdr = line[1:].split()
                idx = hdr.index("%CPU") if "%CPU" in hdr else None
                tid = hdr.index("TID") if "TID" in hdr else None
                continue
            parts = line.split()
            if idx is None or len(parts) <= idx:
                continue
            # process rows have TID '-'; thread rows have TGID '-'
            if tid is not None and len(parts) > tid and parts[tid] != "-":
                continue
            try:
                cpu.append(float(parts[idx]))
            except ValueError:
                pass
    return statistics.mean(cpu) if cpu else None


def hours(start, end):
    try:
        f = "%Y-%m-%dT%H:%M:%SZ"
        return max((datetime.strptime(end, f) - datetime.strptime(start, f)).total_seconds() / 3600, 1e-6)
    except ValueError:
        return None


def fmt(x, nd=1):
    return "-" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir")
    ap.add_argument("--stall-ms", type=float, default=30)
    ap.add_argument("--csv")
    a = ap.parse_args()

    runs_csv = os.path.join(a.out_dir, "runs.csv")
    if not os.path.exists(runs_csv):
        print(f"no {runs_csv}", file=sys.stderr)
        return 1
    rows = []
    with open(runs_csv, newline="") as fh:
        for r in csv.DictReader(fh):
            d = os.path.join(a.out_dir, "runs", r["run_id"])
            h = hours(r["start_utc"], r["end_utc"])
            dg = parse_diag(os.path.join(d, "diag.log"), a.stall_ms)
            cs = parse_csv(os.path.join(d, "diag.csv"), a.stall_ms)
            caps = []
            for j in sorted(glob.glob(os.path.join(d, "cap-*.analysis.json"))):
                with open(j) as fh2:
                    caps.append(json.load(fh2))
            cap_min = sum(c["duration_s"] for c in caps) / 60 if caps else 0
            gaps = sum(c["gaps"]["total"] for c in caps)
            offs = [c["av_offset_ms"]["median"] for c in caps if c["av_offset_ms"]["median"] is not None]
            slips = sum(len(c["beeps"]["odd_intervals"]) for c in caps)
            row = {
                "run_id": r["run_id"], "mode": r["mode"], "kind": r["kind"], "status": r["status"],
                "hours": h,
                "late_per_h": dg and h and dg["late"] / h,
                "drop_per_h": dg and h and dg["drop"] / h,
                "stall_s_per_h": dg and h and dg["cb_max_over"] / h,
                "pop_stall_s_per_h": dg and h and dg["pop_max_over"] / h,
                "ia_stall_s_per_h": dg and h and dg["ia_over"] / h,
                "cb_max_p99_ms": dg and pct(dg["cb_max"], 99),
                "cb_p50_us": cs and cs.get("cb_us", {}).get("p50"),
                "cb_p99_us": cs and cs.get("cb_us", {}).get("p99"),
                "cb_p999_us": cs and cs.get("cb_us", {}).get("p999"),
                "cb_over_per_h": cs and h and cs.get("cb_us", {}).get("over", 0) / h,
                "gaps_per_min": (gaps / cap_min) if cap_min else None,
                "beep_slips": slips if caps else None,
                "av_offset_med_ms": statistics.median(offs) if offs else None,
                "cpu_pct": parse_pidstat(os.path.join(d, "pidstat.txt")),
                "events": dg["events"] if dg else {},
                "csv_cols": {k: v for k, v in (cs or {}).items() if k != "rows"},
            }
            rows.append(row)

    cols = ["run_id", "mode", "status", "hours", "late_per_h", "drop_per_h", "stall_s_per_h",
            "pop_stall_s_per_h", "ia_stall_s_per_h", "cb_max_p99_ms", "cb_p50_us", "cb_p99_us", "cb_p999_us",
            "cb_over_per_h", "gaps_per_min", "beep_slips", "av_offset_med_ms", "cpu_pct"]
    print(f"stall threshold {a.stall_ms} ms; *_per_h = per hour of run; stall_s = seconds whose cb_max exceeded it")
    print("\t".join(cols))
    for r in rows:
        print("\t".join(fmt(r[c], 2) if isinstance(r[c], float) else str(r[c] if r[c] is not None else "-")
                        for c in cols))

    print("\nper mode (measure runs only, mean of runs; n = runs):")
    modes = {}
    for r in rows:
        if r["kind"] == "measure":
            modes.setdefault(r["mode"], []).append(r)
    agg_cols = cols[3:]
    print("\t".join(["mode", "n"] + agg_cols))
    for m, rs in modes.items():
        vals = []
        for c in agg_cols:
            xs = [r[c] for r in rs if isinstance(r[c], (int, float)) and r[c] is not None]
            vals.append(fmt(statistics.mean(xs), 2) if xs else "-")
        print("\t".join([m, str(len(rs))] + vals))

    print("\nevent counts per run (diag 'E' triggers):")
    for r in rows:
        ev = ", ".join(f"{k}={v}" for k, v in sorted(r["events"].items(), key=lambda kv: -kv[1])[:8])
        print(f"  {r['run_id']}: {ev or '-'}")
    extra = {k for r in rows for k in r["csv_cols"]} - {"cb_us"}
    if extra:
        print("\nother *_us columns from diag.csv (p50/p99/p99.9 us):")
        for r in rows:
            parts = [f"{k}={fmt(v['p50'],0)}/{fmt(v['p99'],0)}/{fmt(v['p999'],0)}"
                     for k, v in sorted(r["csv_cols"].items()) if k in extra]
            if parts:
                print(f"  {r['run_id']}: {' '.join(parts)}")

    out = a.csv or os.path.join(a.out_dir, "summary.csv")
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([r[c] if r[c] is not None else "" for c in cols])
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
