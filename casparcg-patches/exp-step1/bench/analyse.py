#!/usr/bin/env python3
"""Compare DeckLink experiment runs per mode (python3 stdlib only; run with `python3 -I`).

  analyse.py RUNS_CSV [RUNS_CSV ...] [--log DIAG_LOG ...] [--csv DIAG_CSV ...]
             [--runs-dir DIR] [--consumer dl1/ch1] [--stall-ms 30]
             [--cb-thread REGEX] [--include-warmup] [--out PREFIX]
  analyse.py --self-test

Inputs
  runs.csv          columns mode,start_utc,end_utc,run_id (more columns are allowed; when a `kind`
                    column exists, rows with kind=warmup are skipped unless --include-warmup).
  diag log          --log, the shared decklink-diag.log. Every S line, E header and I line is assigned to
                    the run whose [start_utc, end_utc) window holds its timestamp. Without --log the
                    per-run copy runs/<id>/diag.log is used if it exists.
  diag CSV          --csv, or <log>.csv when it exists, or runs/<id>/diag.csv. Rows are assigned by
                    wall_ms. Columns are looked up by header name, so the appended experiment columns
                    (cv_us, al_us, pool_hit, pool_miss, mode, ...) may be absent.
  pidstat           runs/<id>/pidstat.txt (pidstat [-h] -t -u [-w]). Total = the process row (TID '-')
                    or, if there is none, the sum of the thread rows per sample. Callback thread = thread
                    rows whose command matches --cb-thread (default 'ScheduledFrame').
  captures          runs/<id>/capture-*.txt with 'gaps_per_min=<n>' and 'av_offset_ms_median=<x> p95=<y>'.

Metrics per mode (repeats pooled; rates divided by the summed run window length)
  cb p50/p99/max    CSV cb_us; else the per-second S cb_max values (source shown in the 'src' column)
  cv p50/p99/max    CSV cv_us (>= 0); else per-second S cv_max values
  stalls/h          CSV rows with ia_us > --stall-ms; else E INTER_ARRIVAL_<ms>ms events with ms > stall-ms
  slips/h           E events carrying A_UNACC_STEP or BA_MIN_STEP, de-duplicated per frame (+-1 frame)
  leadcorr/h        I AUDIO_LEAD_CORRECTED lines (audio lead keeper)
  partial/h         sum of S sched_acc; else SCHED_ACCEPT events
  late/drop/flush/h sums of S res=...
  cpu%, cb cpu%     mean over pidstat samples
  gaps/min          mean over captures; A/V offset: median of capture medians, max of capture p95
Percentiles are nearest-rank. Output: markdown table on stdout and in PREFIX.md, full data in PREFIX.json
(default PREFIX = <dir of first runs.csv>/analysis).
"""
import argparse
import bisect
import csv
import glob
import json
import math
import os
import re
import sys
import tempfile
from array import array
from datetime import datetime, timezone

KV = re.compile(r"(\w+)=(\S+)")
LINE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)\s+(\S+)\s+([SEI])\s(.*)$")
REASON = re.compile(r"^[A-Z][A-Z0-9_]*(?:-?[0-9][0-9A-Za-z_.-]*)?$")
IA_EVT = re.compile(r"^INTER_ARRIVAL_(\d+(?:\.\d+)?)ms$")
SLIP_PREFIXES = ("A_UNACC_STEP", "BA_MIN_STEP")


# ---------------------------------------------------------------- helpers

def parse_utc_ms(s):
    s = s.strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", s):  # epoch seconds
        return int(float(s) * 1000)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(round(dt.timestamp() * 1000))


def pct(sorted_xs, p):
    """Nearest-rank percentile of an already sorted sequence."""
    n = len(sorted_xs)
    if n == 0:
        return None
    k = max(1, math.ceil(p / 100.0 * n))
    return sorted_xs[min(k, n) - 1]


def ms_field(v):
    """'1.7ms' -> 1.7, '20.0..21.7ms' -> 21.7 (upper bound)."""
    try:
        return float(v.split("..")[-1].rstrip("ms"))
    except (ValueError, AttributeError):
        return None


def to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def per_h(count, hours):
    if count is None or not hours:
        return None
    return count / hours


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def median(xs):
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


# ---------------------------------------------------------------- run bookkeeping

class Run:
    def __init__(self, run_id, mode, start_ms, end_ms, base_dir, kind):
        self.id = run_id
        self.mode = mode
        self.start = start_ms
        self.end = end_ms
        self.dir = base_dir
        self.kind = kind
        # diag log
        self.s_lines = 0
        self.res = {"late": 0, "drop": 0, "flush": 0}
        self.s_cb_max = []           # ms per second
        self.s_cv_max = []           # ms per second
        self.s_sched_acc = None      # None = field never seen
        self.s_pool_hit = None
        self.s_pool_miss = None
        self.evt_sched_accept = 0
        self.evt_ia_stalls = 0
        self.slip_frames = []
        self.lead_corr = 0
        self.consumers = set()
        # csv
        self.csv_rows = 0
        self.cb_us = array("l")
        self.cv_us = array("l")
        self.csv_stalls = 0
        self.csv_has_ia = False
        self.csv_pool_hit = None
        self.csv_pool_miss = None
        # pidstat / captures
        self.cpu_total = []
        self.cpu_cb = []
        self.caps = []

    @property
    def hours(self):
        return max(0, self.end - self.start) / 3.6e6


class RunIndex:
    """Maps a timestamp (ms) to the run whose window contains it."""

    def __init__(self, runs):
        self.runs = sorted(runs, key=lambda r: r.start)
        self.starts = [r.start for r in self.runs]

    def find(self, t_ms):
        i = bisect.bisect_right(self.starts, t_ms) - 1
        if i >= 0 and t_ms < self.runs[i].end:
            return self.runs[i]
        return None


def read_runs_csv(path, runs_dir, include_warmup):
    base = runs_dir or os.path.join(os.path.dirname(os.path.abspath(path)), "runs")
    runs = []
    with open(path, newline="", errors="replace") as fh:
        for row in csv.DictReader(fh):
            row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            kind = row.get("kind", "")
            if kind == "warmup" and not include_warmup:
                continue
            try:
                start = parse_utc_ms(row["start_utc"])
                end = parse_utc_ms(row["end_utc"])
            except (KeyError, ValueError):
                print(f"warning: {path}: skipping row without valid start/end: {row}", file=sys.stderr)
                continue
            if start is None or end is None or end <= start:
                print(f"warning: {path}: skipping run {row.get('run_id')} (empty window)", file=sys.stderr)
                continue
            rid = row.get("run_id") or f"run{len(runs)}"
            mode = row.get("mode", "")
            mode = mode if mode else "base"
            runs.append(Run(rid, mode, start, end, os.path.join(base, rid), kind))
    return runs


# ---------------------------------------------------------------- diag log

def consumer_ok(cons, want):
    return want is None or cons == want


def handle_log_line(m, run):
    _, cons, typ, rest = m.groups()
    run.consumers.add(cons)
    if typ == "S":
        kv = dict(KV.findall(rest))
        run.s_lines += 1
        for part in kv.get("res", "").split(","):
            if ":" in part:
                k, v = part.split(":", 1)
                if k in run.res and v.isdigit():
                    run.res[k] += int(v)
        cb = ms_field(kv.get("cb_max"))
        if cb is not None:
            run.s_cb_max.append(cb)
        cv = ms_field(kv.get("cv_max"))
        if cv is not None:
            run.s_cv_max.append(cv)
        for key, attr in (("sched_acc", "s_sched_acc"), ("pool_hit", "s_pool_hit"),
                          ("pool_miss", "s_pool_miss")):
            v = to_int(kv.get(key))
            if v is not None:
                setattr(run, attr, (getattr(run, attr) or 0) + v)
    elif typ == "E":
        toks = rest.split()
        frame = None
        if toks and toks[0].startswith("fr="):
            frame = to_int(toks[0][3:])
        reasons = [t for t in toks[1:] if REASON.match(t)] if frame is not None else \
            [t for t in toks if REASON.match(t)]
        slip = False
        for r in reasons:
            if r.startswith("SCHED_ACCEPT_"):
                run.evt_sched_accept += 1
            mi = IA_EVT.match(r)
            if mi and float(mi.group(1)) > run._stall_ms:
                run.evt_ia_stalls += 1
            if r.startswith(SLIP_PREFIXES):
                slip = True
        if slip:
            run.slip_frames.append(frame if frame is not None else -1)
    elif typ == "I":
        if rest.startswith("AUDIO_LEAD_CORRECTED"):
            run.lead_corr += 1


def read_log(path, index, want_consumer, only_run=None):
    if not os.path.exists(path):
        print(f"warning: diag log {path} not found", file=sys.stderr)
        return set()
    seen = set()
    with open(path, errors="replace") as fh:
        for line in fh:
            if not line or line[0].isspace():
                continue  # E context lines
            m = LINE.match(line.rstrip("\n"))
            if not m:
                continue
            cons = m.group(2)
            seen.add(cons)
            if not consumer_ok(cons, want_consumer):
                continue
            t = parse_utc_ms(m.group(1))
            run = index.find(t)
            if run is None or (only_run is not None and run is not only_run):
                continue
            handle_log_line(m, run)
    return seen


def dedupe_frames(frames):
    """Count slip events, merging events on the same or adjacent frames (A_UNACC_STEP + BA_MIN_STEP)."""
    known = sorted(f for f in frames if f >= 0)
    count = sum(1 for f in frames if f < 0)
    last = None
    for f in known:
        if last is None or f - last > 1:
            count += 1
        last = f
    return count


# ---------------------------------------------------------------- diag CSV

def read_diag_csv(path, index, stall_ms, want_consumer, only_run=None):
    if not os.path.exists(path):
        return False
    with open(path, newline="", errors="replace") as fh:
        rd = csv.reader(fh)
        header = next(rd, None)
        if not header:
            return False
        col = {name.strip(): i for i, name in enumerate(header)}
        if "wall_ms" not in col:
            print(f"warning: {path}: no wall_ms column, ignored", file=sys.stderr)
            return False
        i_wall = col["wall_ms"]
        i_cons = col.get("consumer")
        i_cb, i_cv, i_ia = col.get("cb_us"), col.get("cv_us"), col.get("ia_us")
        i_ph, i_pm = col.get("pool_hit"), col.get("pool_miss")
        stall_us = stall_ms * 1000.0

        def get(row, i):
            if i is None or i >= len(row):
                return None
            return to_int(row[i])

        for row in rd:
            if not row or row[0].startswith("wall_ms"):
                continue  # repeated header
            t = get(row, i_wall)
            if t is None:
                continue
            if want_consumer is not None and i_cons is not None and i_cons < len(row) \
                    and row[i_cons] and row[i_cons] != want_consumer:
                continue
            run = index.find(t)
            if run is None or (only_run is not None and run is not only_run):
                continue
            run.csv_rows += 1
            v = get(row, i_cb)
            if v is not None and v >= 0:
                run.cb_us.append(v)
            v = get(row, i_cv)
            if v is not None and v >= 0:
                run.cv_us.append(v)
            v = get(row, i_ia)
            if v is not None:
                run.csv_has_ia = True
                if v > stall_us:
                    run.csv_stalls += 1
            v = get(row, i_ph)
            if v is not None:
                run.csv_pool_hit = (run.csv_pool_hit or 0) + v
            v = get(row, i_pm)
            if v is not None:
                run.csv_pool_miss = (run.csv_pool_miss or 0) + v
    return True


# ---------------------------------------------------------------- pidstat

def read_pidstat(path, cb_re):
    """Returns (list of total %CPU per sample, list of callback-thread %CPU per sample)."""
    if not os.path.exists(path):
        return [], []
    cols = None
    samples = {}  # time -> {"proc": x or None, "threads": sum, "cb": sum}
    order = []
    with open(path, errors="replace") as fh:
        for line in fh:
            toks = line.split()
            if not toks:
                continue
            if toks[0] == "#" or ("TID" in toks and ("%CPU" in toks or "cswch/s" in toks)):
                t = toks[1:] if toks[0] == "#" else toks
                cols = {name: i for i, name in enumerate(t)}
                continue
            if cols is None or toks[0].startswith(("Average", "Linux")) or "%CPU" not in cols \
                    or "Command" not in cols:
                continue
            ncmd = cols["Command"]
            # Without -h the time column header is the time itself (e.g. '16:28:50'), so data lines
            # carry the same number of fixed columns as the header.
            if len(toks) < ncmd + 1:
                continue
            fixed, cmd = toks[:ncmd], " ".join(toks[ncmd:])
            try:
                cpu = float(fixed[cols["%CPU"]])
            except (ValueError, IndexError):
                continue
            tid = fixed[cols["TID"]] if "TID" in cols else "-"
            key = fixed[0]
            s = samples.get(key)
            if s is None:
                s = samples[key] = {"proc": None, "threads": 0.0, "nthreads": 0, "cb": 0.0}
                order.append(key)
            if tid == "-":
                s["proc"] = (s["proc"] or 0.0) + cpu
            else:
                s["threads"] += cpu
                s["nthreads"] += 1
                if cb_re.search(cmd):
                    s["cb"] += cpu
    total, cbs = [], []
    for k in order:
        s = samples[k]
        if s["proc"] is not None:
            total.append(s["proc"])
        elif s["nthreads"]:
            total.append(s["threads"])
        if s["nthreads"]:
            cbs.append(s["cb"])
    return total, cbs


# ---------------------------------------------------------------- captures

CAP_KV = re.compile(r"([A-Za-z_][\w]*)=(-?[\d.]+)")


def read_captures(run_dir):
    caps = []
    for p in sorted(glob.glob(os.path.join(run_dir, "capture-*.txt"))):
        c = {"file": os.path.basename(p), "gaps_per_min": None, "av_median": None, "av_p95": None}
        with open(p, errors="replace") as fh:
            for line in fh:
                kv = dict(CAP_KV.findall(line))
                if "gaps_per_min" in kv:
                    c["gaps_per_min"] = float(kv["gaps_per_min"])
                if "av_offset_ms_median" in kv:
                    c["av_median"] = float(kv["av_offset_ms_median"])
                    if "p95" in kv:
                        c["av_p95"] = float(kv["p95"])
        caps.append(c)
    return caps


# ---------------------------------------------------------------- aggregation

def stats3(values):
    s = sorted(values)
    return {"n": len(s), "p50": pct(s, 50), "p99": pct(s, 99), "max": s[-1] if s else None}


def summarise(runs, stall_ms):
    modes = {}
    for r in runs:
        modes.setdefault(r.mode, []).append(r)
    out = {}
    for mode, rs in modes.items():
        hours = sum(r.hours for r in rs)
        d = {"runs": [r.id for r in rs], "n_runs": len(rs), "hours": hours,
             "diag_seconds": sum(r.s_lines for r in rs), "csv_rows": sum(r.csv_rows for r in rs)}

        # callback / conversion durations (ms)
        all_csv = all(len(r.cb_us) for r in rs)
        if all_csv:
            cb = stats3([v / 1000.0 for r in rs for v in r.cb_us])
            d["cb_src"] = "csv"
        else:
            cb = stats3([v for r in rs for v in r.s_cb_max])
            d["cb_src"] = "S" if cb["n"] else None
            if any(len(r.cb_us) for r in rs):
                d["note_cb"] = "some runs lack CSV cb_us; per-second S cb_max used for the whole mode"
        d["cb_ms"] = cb
        if all(len(r.cv_us) for r in rs):
            d["cv_ms"] = stats3([v / 1000.0 for r in rs for v in r.cv_us])
            d["cv_src"] = "csv"
        else:
            cv = stats3([v for r in rs for v in r.s_cv_max])
            d["cv_ms"] = cv
            d["cv_src"] = "S" if cv["n"] else None

        # stalls
        if all(r.csv_has_ia for r in rs):
            stalls, d["stall_src"] = sum(r.csv_stalls for r in rs), "csv"
        else:
            stalls, d["stall_src"] = sum(r.evt_ia_stalls for r in rs), "events"
        d["stalls"] = stalls
        d["stalls_per_h"] = per_h(stalls, hours)

        slips = sum(dedupe_frames(r.slip_frames) for r in rs)
        d["slips"], d["slips_per_h"] = slips, per_h(slips, hours)
        lc = sum(r.lead_corr for r in rs)
        d["lead_corr"], d["lead_corr_per_h"] = lc, per_h(lc, hours)

        if all(r.s_sched_acc is not None for r in rs):
            partial, d["partial_src"] = sum(r.s_sched_acc for r in rs), "S"
        else:
            partial, d["partial_src"] = sum(r.evt_sched_accept for r in rs), "events"
        d["partial"], d["partial_per_h"] = partial, per_h(partial, hours)

        for k in ("late", "drop", "flush"):
            n = sum(r.res[k] for r in rs)
            d[k] = n
            d[k + "_per_h"] = per_h(n, hours)

        hit = miss = None
        if all(r.csv_pool_hit is not None for r in rs):
            hit = sum(r.csv_pool_hit for r in rs)
            miss = sum(r.csv_pool_miss or 0 for r in rs)
        elif all(r.s_pool_hit is not None for r in rs):
            hit = sum(r.s_pool_hit for r in rs)
            miss = sum(r.s_pool_miss or 0 for r in rs)
        d["pool_hit"], d["pool_miss"] = hit, miss
        d["pool_hit_pct"] = (100.0 * hit / (hit + miss)) if hit is not None and (hit + miss) else None

        d["cpu_pct"] = mean([v for r in rs for v in r.cpu_total])
        d["cb_thread_cpu_pct"] = mean([v for r in rs for v in r.cpu_cb])
        caps = [c for r in rs for c in r.caps]
        d["captures"] = len(caps)
        d["gaps_per_min"] = mean([c["gaps_per_min"] for c in caps if c["gaps_per_min"] is not None])
        d["av_offset_ms_median"] = median([c["av_median"] for c in caps if c["av_median"] is not None])
        p95s = [c["av_p95"] for c in caps if c["av_p95"] is not None]
        d["av_offset_ms_p95_max"] = max(p95s) if p95s else None
        out[mode] = d
    return out


def run_details(r):
    return {"run_id": r.id, "mode": r.mode, "kind": r.kind, "hours": r.hours, "diag_seconds": r.s_lines,
            "csv_rows": r.csv_rows, "consumers": sorted(r.consumers),
            "cb_ms": stats3([v / 1000.0 for v in r.cb_us]) if len(r.cb_us) else stats3(r.s_cb_max),
            "stalls": r.csv_stalls if r.csv_has_ia else r.evt_ia_stalls,
            "slips": dedupe_frames(r.slip_frames), "lead_corr": r.lead_corr, **r.res,
            "cpu_pct": mean(r.cpu_total), "cb_thread_cpu_pct": mean(r.cpu_cb), "captures": r.caps}


def fmt(x, nd=1):
    if x is None:
        return "-"
    if isinstance(x, int):
        return str(x)
    return f"{x:.{nd}f}"


def markdown(summary, stall_ms):
    hdr = ["mode", "runs", "h", "cb p50/p99/max ms", "cv p50/p99/max ms", "src cb/cv",
           f"stalls/h (>{stall_ms:g}ms)", "slips/h", "leadcorr/h", "partial/h", "late/h", "drop/h",
           "flush/h", "pool hit%", "cpu%", "cb cpu%", "gaps/min", "A/V ms med (p95 max)"]
    lines = ["| " + " | ".join(hdr) + " |", "|" + "|".join("---" for _ in hdr) + "|"]

    def trip(s):
        return "/".join(fmt(s[k], 2 if k != "max" else 1) for k in ("p50", "p99", "max")) if s["n"] else "-"

    for mode in sorted(summary, key=lambda m: (m != "base", m)):
        d = summary[mode]
        av = "-" if d["av_offset_ms_median"] is None else \
            f"{fmt(d['av_offset_ms_median'])} ({fmt(d['av_offset_ms_p95_max'])})"
        row = [mode, str(d["n_runs"]), fmt(d["hours"], 2), trip(d["cb_ms"]), trip(d["cv_ms"]),
               f"{d['cb_src'] or '-'}/{d['cv_src'] or '-'}", fmt(d["stalls_per_h"]), fmt(d["slips_per_h"]),
               fmt(d["lead_corr_per_h"]), fmt(d["partial_per_h"]), fmt(d["late_per_h"]),
               fmt(d["drop_per_h"]), fmt(d["flush_per_h"]), fmt(d["pool_hit_pct"]), fmt(d["cpu_pct"]),
               fmt(d["cb_thread_cpu_pct"]), fmt(d["gaps_per_min"], 2), av]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- main

def analyse(args):
    runs = []
    for rc in args.runs_csv:
        runs.extend(read_runs_csv(rc, args.runs_dir, args.include_warmup))
    if not runs:
        raise SystemExit("no runs found")
    for r in runs:
        r._stall_ms = args.stall_ms
    index = RunIndex(runs)
    seen = set()
    if args.log:
        for lp in args.log:
            seen |= read_log(lp, index, args.consumer)
    else:
        for r in runs:
            seen |= read_log(os.path.join(r.dir, "diag.log"), index, args.consumer, only_run=r) \
                if os.path.exists(os.path.join(r.dir, "diag.log")) else set()
    if args.consumer is None and len(seen) > 1:
        print(f"warning: several consumers in the diag log ({', '.join(sorted(seen))}); "
              f"they are mixed together, pass --consumer", file=sys.stderr)
    csvs = list(args.csv or [])
    if not csvs and args.log:
        csvs = [lp + ".csv" for lp in args.log if os.path.exists(lp + ".csv")]
    if csvs:
        for cp in csvs:
            read_diag_csv(cp, index, args.stall_ms, args.consumer)
    else:
        for r in runs:
            read_diag_csv(os.path.join(r.dir, "diag.csv"), index, args.stall_ms, args.consumer, only_run=r)
    cb_re = re.compile(args.cb_thread)
    for r in runs:
        r.cpu_total, r.cpu_cb = read_pidstat(os.path.join(r.dir, "pidstat.txt"), cb_re)
        r.caps = read_captures(r.dir)
    summary = summarise(runs, args.stall_ms)
    md = markdown(summary, args.stall_ms)
    result = {"stall_ms": args.stall_ms, "consumer": args.consumer, "modes": summary,
              "runs": [run_details(r) for r in sorted(runs, key=lambda r: r.start)]}
    prefix = args.out or os.path.join(os.path.dirname(os.path.abspath(args.runs_csv[0])), "analysis")
    if prefix != "-":
        with open(prefix + ".md", "w") as fh:
            fh.write(md)
        with open(prefix + ".json", "w") as fh:
            json.dump(result, fh, indent=2, sort_keys=True)
    return md, result, prefix


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs_csv", nargs="*")
    ap.add_argument("--log", action="append", help="shared diag log (repeatable)")
    ap.add_argument("--csv", action="append", help="diag CSV (repeatable; default <log>.csv)")
    ap.add_argument("--runs-dir", help="directory holding <run_id>/ (default <runs.csv dir>/runs)")
    ap.add_argument("--consumer", help="only this consumer, e.g. dl1/ch1")
    ap.add_argument("--stall-ms", type=float, default=30.0)
    ap.add_argument("--cb-thread", default="ScheduledFrame", help="regex for the callback thread name")
    ap.add_argument("--include-warmup", action="store_true")
    ap.add_argument("--out", help="output prefix for .md/.json ('-' = stdout only)")
    ap.add_argument("--self-test", action="store_true")
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.runs_csv:
        ap.error("RUNS_CSV is required")
    md, _, prefix = analyse(args)
    sys.stdout.write(md)
    if prefix != "-":
        print(f"\nwritten: {prefix}.md {prefix}.json", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- self-test

def self_test():
    T0 = parse_utc_ms("2026-11-25T10:00:00Z")
    MIN = 60000
    windows = {  # run_id: (mode, kind, start, end)
        "00-warmup-base": ("base", "warmup", T0 - 2 * MIN, T0 - MIN),
        "01-r1-base": ("base", "measure", T0, T0 + MIN),
        "02-r1-ahead": ("ahead,pool", "measure", T0 + 2 * MIN, T0 + 3 * MIN),
        "03-r2-base": ("base", "measure", T0 + 4 * MIN, T0 + 5 * MIN),
    }

    def iso(ms):
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
            f"{ms % 1000:03d}Z"

    def s_line(t, cons, fr, res="ok:50,late:0,drop:0,flush:0,other:0", cb=1.7, cv=1.5, sched_acc=0,
               ph=0, pm=0):
        return (f"{iso(t)} {cons} S fr={fr} n=50 res={res} ba=2860..3828 ia=20.0..20.0ms pop_max=0.3ms "
                f"sv_max=0.1ms sa_max=0.0ms cb_max={cb:.1f}ms sched_acc={sched_acc} oncb=25 ovf=0 sup=0 "
                f"cv_max={cv:.1f}ms cv_p95=1.4ms al_max=0.0ms swait_max=0.0ms age_max=0.0ms cv_cb=50 "
                f"pool_hit={ph} pool_miss={pm}\n")

    def e_block(t, cons, fr, reasons):
        ctx = "".join(f"  f={f} t=x r=ok cft=0 ia=20000 pop=300 sv=50 sa=30 cb=2000\n"
                      for f in range(fr - 5, fr + 5))
        return f"{iso(t)} {cons} E fr={fr} {reasons}\n" + ctx

    log, rows = [], []
    for rid, (mode, kind, start, end) in windows.items():
        ahead = mode.startswith("ahead")
        warm = kind == "warmup"
        for sec in range(60):
            t = start + sec * 1000 + 999
            fr = (start - T0) // 20 + sec * 50 + 50
            res = "ok:50,late:0,drop:0,flush:0,other:0"
            if rid == "01-r1-base" and sec == 10:
                res = "ok:47,late:2,drop:1,flush:0,other:0"
            if rid == "03-r2-base" and sec == 20:
                res = "ok:47,late:0,drop:0,flush:3,other:0"
            if warm:
                res = "ok:0,late:50,drop:50,flush:50,other:0"
            sacc = 1 if (rid in ("01-r1-base", "03-r2-base") and sec == 30) else 0
            cb = 35.0 if (not ahead and sec in (5, 6)) else (0.3 if ahead else 1.7)
            if warm:
                cb = 99.0
            cv = 33.0 if (not ahead and sec == 5) else 1.5
            log.append(s_line(t, "dl1/ch1", fr, res, cb, cv, sacc,
                              ph=(50 if ahead and sec else (40 if ahead else 0)),
                              pm=(10 if ahead and sec == 0 else 0)))
            # a second consumer that must be filtered out
            log.append(s_line(t + 1, "dl2/ch1", fr, "ok:0,late:100,drop:100,flush:100,other:0", 500.0))
            for k in range(50):
                w = start + sec * 1000 + k * 20
                f = fr - 50 + k
                stall = (not ahead and not warm and sec == 5 and k < 10)
                cbu = 35000 if stall else (300 if ahead else 1700)
                if warm:
                    cbu = 99000
                cvu = 33000 if stall else (1400 if ahead else 1500)
                ia = 36000 if stall else 20000
                phit = 1 if ahead and not (sec == 0 and k < 10) else 0
                pmiss = 1 if ahead and sec == 0 and k < 10 else 0
                rows.append([w, "dl1/ch1", f, "ok", ia, cbu, cvu, phit, pmiss])
                rows.append([w, "dl2/ch1", f, "ok", 90000, 500000, 500000, 0, 0])
        if rid == "01-r1-base":
            log.append(e_block(start + 2000, "dl1/ch1", 100, "A_UNACC_STEP_960"))
            log.append(e_block(start + 2100, "dl1/ch1", 100, "BA_MIN_STEP sec_last_fr=149 prev_min=2860 "
                                                             "min=1900 delta=-960 ba_cand=100:-960,90:0 "
                                                             "unacc_cand=100:960"))
            log.append(e_block(start + 3000, "dl1/ch1", 150, "SCHED_ACCEPT_0_OF_960"))
            log.append(e_block(start + 5100, "dl1/ch1", 300, "INTER_ARRIVAL_36ms CFT_STEP_2000"))
            log.append(e_block(start + 5200, "dl1/ch1", 301, "INTER_ARRIVAL_SHORT_4000us"))
        if rid == "03-r2-base":
            log.append(e_block(start + 40000, "dl1/ch1", 14500, "PLAYHEAD_STEP_2000_HW_1000 A_UNACC_STEP_-960"))
            log.append(e_block(start + 5100, "dl1/ch1", 12300, "INTER_ARRIVAL_45ms"))
            log.append(e_block(start + 5300, "dl1/ch1", 12301, "INTER_ARRIVAL_25ms"))  # under threshold
        if rid == "02-r1-ahead":
            log.append(f"{iso(start + 30000)} dl1/ch1 I AUDIO_LEAD_CORRECTED -960 baseline=4800 lead=3840\n")
        if warm:
            log.append(e_block(start + 1000, "dl1/ch1", 10, "A_UNACC_STEP_960"))
            log.append(f"{iso(start + 1500)} dl1/ch1 I AUDIO_LEAD_CORRECTED 960 baseline=1 lead=2\n")
    # outside every window
    log.append(f"{iso(T0 + 3 * MIN + 30000)} dl1/ch1 I AUDIO_LEAD_CORRECTED 960 baseline=1 lead=2\n")
    log.append(e_block(T0 + 3 * MIN + 30000, "dl1/ch1", 9999, "A_UNACC_STEP_960"))
    log.append(s_line(T0 + 3 * MIN + 30000, "dl1/ch1", 9999, "ok:0,late:9,drop:9,flush:9,other:0"))
    log.sort(key=lambda s: s[:24])

    def pidstat(dir_, total_samples, cb_val, with_proc=True):
        lines = ["Linux 6.8.0 (caspar1) \t11/25/2026 \t_x86_64_\t(16 CPU)\n", "\n"]
        hdr = ("# Time        UID      TGID       TID    %usr %system  %guest   %wait    %CPU   CPU   "
               "cswch/s nvcswch/s  Command\n")
        for i, tot in enumerate(total_samples):
            ts = 1764064800 + i
            lines.append(hdr)
            if with_proc:
                lines.append(f"{ts} 0 1635 - 1.0 1.0 0.00 0.00 {tot:.2f} 3 0.00 0.00 casparcg\n")
            lines.append(f"{ts} 0 - 1635 1.0 1.0 0.00 0.00 10.00 3 1.00 0.00 |__casparcg\n")
            lines.append(f"{ts} 0 - 1700 1.0 1.0 0.00 0.00 {cb_val:.2f} 5 50.00 0.00 |__ScheduledFrameC\n")
            lines.append(f"{ts} 0 - 1701 1.0 1.0 0.00 0.00 5.00 6 1.00 0.00 |__decklink diag\n")
            lines.append("\n")
        lines.append("Average: 0 1635 - 1.0 1.0 0.00 0.00 99.00 - 0.00 0.00 casparcg\n")
        os.makedirs(dir_, exist_ok=True)
        with open(os.path.join(dir_, "pidstat.txt"), "w") as fh:
            fh.writelines(lines)

    def write_case(root, csv_cols, with_csv=True, per_run_logs=False):
        runs_dir = os.path.join(root, "runs")
        os.makedirs(runs_dir)
        with open(os.path.join(root, "runs.csv"), "w") as fh:
            fh.write("run_id,mode,round,kind,start_utc,end_utc,status\n")
            for rid, (mode, kind, start, end) in windows.items():
                fh.write(f'{rid},"{mode}",1,{kind},{iso(start)[:19]}Z,{iso(end)[:19]}Z,ok\n')
        logp = os.path.join(root, "decklink-diag.log")
        with open(logp, "w") as fh:
            fh.writelines(log)
        if per_run_logs:
            for rid, (_, _, start, end) in windows.items():
                os.makedirs(os.path.join(runs_dir, rid), exist_ok=True)
                with open(os.path.join(runs_dir, rid, "diag.log"), "w") as fh:
                    keep = False
                    for line in log:
                        if not line[0].isspace():
                            keep = start <= parse_utc_ms(line[:24]) < end
                        if keep:
                            fh.write(line)
        if with_csv:
            all_cols = ["wall_ms", "consumer", "frame_no", "result", "ia_us", "cb_us", "cv_us",
                        "pool_hit", "pool_miss"]
            idx = [all_cols.index(c) for c in csv_cols]
            with open(logp + ".csv", "w") as fh:
                fh.write(",".join(csv_cols) + "\n")
                for r in rows:
                    fh.write(",".join(str(r[i]) for i in idx) + "\n")
        pidstat(os.path.join(runs_dir, "01-r1-base"), [20, 30], 4)
        pidstat(os.path.join(runs_dir, "03-r2-base"), [30, 40], 4)
        pidstat(os.path.join(runs_dir, "02-r1-ahead"), [0, 0, 0], 1, with_proc=False)
        pidstat(os.path.join(runs_dir, "00-warmup-base"), [99], 99)
        with open(os.path.join(runs_dir, "01-r1-base", "capture-01.txt"), "w") as fh:
            fh.write("file=cap-01.mkv\ngaps_per_min=0.5\nav_offset_ms_median=2.0 p95=4.0\n")
        with open(os.path.join(runs_dir, "03-r2-base", "capture-01.txt"), "w") as fh:
            fh.write("gaps_per_min=1.5\nav_offset_ms_median=4.0 p95=10.0\n")
        return logp

    def approx(a, b, what):
        assert a is not None and abs(a - b) < 1e-6, f"{what}: got {a}, expected {b}"

    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        # --- case 1: shared log + full CSV (no 'mode' column), consumer filter
        root = os.path.join(tmp, "c1")
        os.makedirs(root)
        logp = write_case(root, ["wall_ms", "consumer", "frame_no", "result", "ia_us", "cb_us", "cv_us",
                                 "pool_hit", "pool_miss"])
        args = build_parser().parse_args([os.path.join(root, "runs.csv"), "--log", logp,
                                          "--consumer", "dl1/ch1"])
        md, res, prefix = analyse(args)
        print("case 1 (shared log, CSV with experiment columns):\n" + md)
        b, a = res["modes"]["base"], res["modes"]["ahead,pool"]
        assert set(res["modes"]) == {"base", "ahead,pool"}, res["modes"].keys()
        assert b["n_runs"] == 2 and a["n_runs"] == 1
        approx(b["hours"], 2 / 60, "base hours")
        assert b["csv_rows"] == 6000 and a["csv_rows"] == 3000, (b["csv_rows"], a["csv_rows"])
        assert b["cb_src"] == "csv" and b["cv_src"] == "csv"
        approx(b["cb_ms"]["p50"], 1.7, "base cb p50")
        approx(b["cb_ms"]["p99"], 1.7, "base cb p99")       # 20 of 6000 slow < 1 %
        approx(b["cb_ms"]["max"], 35.0, "base cb max")      # warm-up 99 ms excluded
        approx(a["cb_ms"]["max"], 0.3, "ahead cb max")
        approx(b["cv_ms"]["max"], 33.0, "base cv max")
        approx(a["cv_ms"]["p50"], 1.4, "ahead cv p50")
        approx(b["stalls_per_h"], 600.0, "base stalls/h")   # 20 rows ia>30ms / (2/60 h)
        assert b["stall_src"] == "csv" and a["stalls"] == 0
        assert b["slips"] == 2, b["slips"]                  # fr 100 (2 events merged) + fr 14500
        approx(b["slips_per_h"], 60.0, "base slips/h")
        assert a["slips"] == 0 and a["lead_corr"] == 1 and b["lead_corr"] == 0
        approx(a["lead_corr_per_h"], 60.0, "ahead leadcorr/h")
        assert b["partial_src"] == "S" and b["partial"] == 2
        approx(b["partial_per_h"], 60.0, "base partial/h")
        approx(b["late_per_h"], 60.0, "late/h")
        approx(b["drop_per_h"], 30.0, "drop/h")
        approx(b["flush_per_h"], 90.0, "flush/h")
        assert a["pool_hit"] == 2990 and a["pool_miss"] == 10, (a["pool_hit"], a["pool_miss"])
        approx(b["cpu_pct"], 30.0, "base cpu")
        approx(b["cb_thread_cpu_pct"], 4.0, "base cb cpu")
        approx(a["cpu_pct"], 16.0, "ahead cpu (thread sum)")
        approx(a["cb_thread_cpu_pct"], 1.0, "ahead cb cpu")
        approx(b["gaps_per_min"], 1.0, "gaps/min")
        approx(b["av_offset_ms_median"], 3.0, "av median")
        approx(b["av_offset_ms_p95_max"], 10.0, "av p95 max")
        assert a["gaps_per_min"] is None and a["captures"] == 0
        assert os.path.exists(prefix + ".md") and os.path.exists(prefix + ".json")
        json.load(open(prefix + ".json"))
        print("case 1 ok")

        # --- case 2: per-run diag.log copies, no CSV anywhere -> S/E fallbacks
        root = os.path.join(tmp, "c2")
        os.makedirs(root)
        write_case(root, [], with_csv=False, per_run_logs=True)
        args = build_parser().parse_args([os.path.join(root, "runs.csv"), "--consumer", "dl1/ch1",
                                          "--out", "-"])
        md, res, _ = analyse(args)
        print("case 2 (per-run logs, no CSV):\n" + md)
        b, a = res["modes"]["base"], res["modes"]["ahead,pool"]
        assert b["cb_src"] == "S" and b["stall_src"] == "events" and b["csv_rows"] == 0
        assert b["cb_ms"]["n"] == 120
        approx(b["cb_ms"]["p50"], 1.7, "S cb p50")
        approx(b["cb_ms"]["p99"], 35.0, "S cb p99")         # 4 of 120 seconds at 35 ms
        approx(b["cb_ms"]["max"], 35.0, "S cb max")
        approx(b["cv_ms"]["max"], 33.0, "S cv max")
        assert b["stalls"] == 2, b["stalls"]                # 36 ms and 45 ms events; 25 ms ignored
        approx(b["stalls_per_h"], 60.0, "event stalls/h")
        assert b["slips"] == 2 and a["lead_corr"] == 1
        assert a["pool_hit"] == 40 + 59 * 50 and a["pool_miss"] == 10, (a["pool_hit"], a["pool_miss"])
        print("case 2 ok")

        # --- case 3: old CSV header (no cv_us / pool columns, no consumer column) -> cv from S lines
        root = os.path.join(tmp, "c3")
        os.makedirs(root)
        rows[:] = [r for r in rows if r[1] == "dl1/ch1"]
        logp = write_case(root, ["wall_ms", "frame_no", "result", "ia_us", "cb_us"])
        args = build_parser().parse_args([os.path.join(root, "runs.csv"), "--log", logp,
                                          "--consumer", "dl1/ch1", "--out", "-"])
        md, res, _ = analyse(args)
        print("case 3 (old CSV header):\n" + md)
        b, a = res["modes"]["base"], res["modes"]["ahead,pool"]
        assert b["cb_src"] == "csv" and b["cv_src"] == "S"
        approx(b["cb_ms"]["max"], 35.0, "old csv cb max")
        approx(b["cv_ms"]["max"], 33.0, "S cv max (old csv)")
        approx(b["stalls_per_h"], 600.0, "old csv stalls/h")
        assert a["pool_hit"] == 40 + 59 * 50                # from S lines
        print("case 3 ok")

        # --- warm-up included on request
        args = build_parser().parse_args([os.path.join(root, "runs.csv"), "--log", logp,
                                          "--consumer", "dl1/ch1", "--out", "-", "--include-warmup"])
        _, res, _ = analyse(args)
        b = res["modes"]["base"]
        assert b["n_runs"] == 3 and b["late"] == 2 + 50 * 60, b["late"]
        approx(b["cb_ms"]["max"], 99.0, "warm-up cb max")
        print("warm-up case ok")
    print("SELF-TEST PASSED" if not failures else "SELF-TEST FAILED")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
