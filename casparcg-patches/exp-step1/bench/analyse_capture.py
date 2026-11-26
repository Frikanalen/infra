#!/usr/bin/env python3
"""Parse the ffmpeg outputs produced by capture-analyse.sh (stdlib only)."""
import argparse
import bisect
import json
import re
import statistics
import sys

RE_START = re.compile(r"silence_start:\s*(-?[0-9.]+)")
RE_END = re.compile(r"silence_end:\s*(-?[0-9.]+)\s*\|\s*silence_duration:\s*(-?[0-9.]+)")
RE_PTS = re.compile(r"pts_time:(-?[0-9.]+)")
RE_YAVG = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")
RE_DUR = re.compile(r"Duration:\s*(\d+):(\d+):([0-9.]+)")


def pct(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def parse_silence(path):
    starts, ends, dur = [], [], None
    with open(path, errors="replace") as fh:
        for line in fh:
            m = RE_START.search(line)
            if m:
                starts.append(float(m.group(1)))
            m = RE_END.search(line)
            if m:
                ends.append((float(m.group(1)), float(m.group(2))))
            m = RE_DUR.search(line)
            if m and dur is None:
                dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return starts, ends, dur


def parse_luma(path):
    frames = []
    pts = None
    with open(path, errors="replace") as fh:
        for line in fh:
            m = RE_PTS.search(line)
            if m:
                pts = float(m.group(1))
                continue
            m = RE_YAVG.search(line)
            if m and pts is not None:
                frames.append((pts, float(m.group(1))))
                pts = None
    return frames


def flash_onsets(frames, thr):
    onsets, prev = [], False
    for t, y in frames:
        cur = y >= thr
        if cur and not prev:
            onsets.append(t)
        prev = cur
    return onsets


def intervals_report(ts, nominal=1.0, tol=0.005, unit=0.02):
    """Intervals between successive events; anything off nominal by > tol is
    reported, with its size in units (frames of 20 ms)."""
    odd = []
    for a, b in zip(ts, ts[1:]):
        d = b - a
        if abs(d - nominal) > tol:
            odd.append({"at_s": round(a, 4), "interval_ms": round(d * 1000, 2),
                        "frames_off": round((d - nominal) / unit, 2)})
    return odd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gaps", required=True)
    ap.add_argument("--beeps", required=True)
    ap.add_argument("--luma", required=True)
    ap.add_argument("--flash-yavg", type=float, default=128)
    ap.add_argument("--gap-skip-s", type=float, default=1.0)
    ap.add_argument("--beep-min-ms", type=float, default=8)
    ap.add_argument("--fps", type=float, default=50)
    ap.add_argument("--source", default="")
    ap.add_argument("--json")
    a = ap.parse_args()

    g_starts, g_ends, dur = parse_silence(a.gaps)
    skipped = sum(1 for x in g_starts if x < a.gap_skip_s)
    g_ends = [(e, d) for e, d in g_ends if e - d >= a.gap_skip_s]
    g_starts = [x for x in g_starts if x >= a.gap_skip_s]
    b_starts, b_ends, _ = parse_silence(a.beeps)
    frames = parse_luma(a.luma)

    t0 = frames[0][0] if frames else 0.0
    if dur is None and frames:
        dur = frames[-1][0] - t0
    dur = dur or 0.0

    # Gaps per minute (relative to the start of the file).
    per_min = {}
    for s in g_starts:
        per_min[int(max(s, 0) // 60)] = per_min.get(int(max(s, 0) // 60), 0) + 1
    n_min = max(1, int(dur // 60) + (1 if dur % 60 > 1 else 0))
    gaps_per_minute = [per_min.get(i, 0) for i in range(n_min)]
    gap_durs = [d for _, d in g_ends]

    # A burst is a non-silent span of at least --beep-min-ms; shorter spans are
    # clicks from discontinuities (e.g. at an audio gap) leaking through the filter.
    beeps, clicks = [], 0
    for e, _ in b_ends:
        i = bisect.bisect_right(b_starts, e)
        span = (b_starts[i] - e) if i < len(b_starts) else float("inf")
        if span * 1000 >= a.beep_min_ms:
            beeps.append(e)
        else:
            clicks += 1
    flashes = flash_onsets(frames, a.flash_yavg)
    unit = 1.0 / a.fps

    offsets = []
    for f in flashes:
        i = bisect.bisect_left(beeps, f)
        cands = [beeps[k] - f for k in (i - 1, i) if 0 <= k < len(beeps)]
        cands = [d for d in cands if abs(d) <= 0.5]
        if cands:
            offsets.append(min(cands, key=abs) * 1000)

    res = {
        "source": a.source,
        "duration_s": round(dur, 3),
        "gaps": {
            "total": len(g_starts),
            "skipped_at_start": skipped,
            "per_minute": gaps_per_minute,
            "silent_ms_total": round(sum(gap_durs) * 1000, 3),
            "longest_ms": round(max(gap_durs) * 1000, 3) if gap_durs else 0,
            "first_starts_s": [round(x, 4) for x in g_starts[:20]],
        },
        "beeps": {"count": len(beeps), "clicks_ignored": clicks,
                  "odd_intervals": intervals_report(beeps, unit=unit)},
        "flashes": {"count": len(flashes), "frames": len(frames),
                    "odd_intervals": intervals_report(flashes, tol=unit / 2, unit=unit)},
        "av_offset_ms": {
            "pairs": len(offsets),
            "median": round(statistics.median(offsets), 2) if offsets else None,
            "p5": round(pct(offsets, 5), 2) if offsets else None,
            "p95": round(pct(offsets, 95), 2) if offsets else None,
            "min": round(min(offsets), 2) if offsets else None,
            "max": round(max(offsets), 2) if offsets else None,
            "abs_p95": round(pct([abs(x) for x in offsets], 95), 2) if offsets else None,
            "distinct_frames": sorted({round(x / (unit * 1000)) for x in offsets}),
        },
    }

    o = res["av_offset_ms"]
    print(f"file: {a.source}  duration {res['duration_s']} s")
    print(f"  audio gaps: {res['gaps']['total']} total, per minute {gaps_per_minute}, "
          f"silent {res['gaps']['silent_ms_total']} ms, longest {res['gaps']['longest_ms']} ms")
    print(f"  beeps {len(beeps)} (odd intervals {len(res['beeps']['odd_intervals'])}), "
          f"flashes {len(flashes)} (odd intervals {len(res['flashes']['odd_intervals'])})")
    if offsets:
        print(f"  A/V offset (audio - video): median {o['median']} ms, p5 {o['p5']}, p95 {o['p95']}, "
              f"min {o['min']}, max {o['max']}  ({o['pairs']} pairs; frame steps seen {o['distinct_frames']})")
    else:
        print("  A/V offset: no flash/beep pairs found (check FLASH_CROP / FLASH_YAVG / BEEP_THRESH)")
    for kind in ("beeps", "flashes"):
        for x in res[kind]["odd_intervals"][:10]:
            print(f"    {kind[:-1]} interval {x['interval_ms']} ms at {x['at_s']} s ({x['frames_off']:+} frames)")

    if a.json:
        with open(a.json, "w") as fh:
            json.dump(res, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
