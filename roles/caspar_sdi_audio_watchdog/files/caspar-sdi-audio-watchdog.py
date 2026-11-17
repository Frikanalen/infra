#!/usr/bin/env python3
"""Restart CasparCG when its DeckLink SDI audio is jammed.

CasparCG 2.5's DeckLink consumer can lose embedded audio for good after a
short output timing hiccup, while the mixer and the ffmpeg (multicast)
consumer keep good audio. Only re-creating the consumer brings it back.

This compares two audio levels once a second:
  * pre:  the channel's own multicast (before the DeckLink card)
  * post: the encoder appliance's program return (after SDI)
The audio is "jammed" when pre is above PRE_MIN_DBFS while post is below
POST_MAX_DBFS. If that holds for JAM_SECONDS, the watchdog restarts nodm
(and with it CasparCG). Playout re-cues by itself on reconnect.

Fail-safe: if either input has no fresh audio, nothing is done.
Air check: before restarting it asks atemkontroll (the studio ATEM's HTTP
API, GET /state) what is on program. It restarts only if CasparCG's ATEM
input is on program; another source, a transition or fade-to-black
inhibits. If atemkontroll is unset, unreachable or unclear, ON_UNKNOWN_PROGRAM
decides (restart or inhibit).
Rate-limited: COOLDOWN_SECONDS between restarts, at most MAX_RESTARTS per
WINDOW_SECONDS (counted across watchdog restarts), then log only.

Config comes from the environment (see the systemd unit). Stdlib only;
needs the ffmpeg CLI.
"""

import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request

CFG = {
    "PRE_URL": os.environ.get("PRE_URL", "udp://239.1.1.51:5004?overrun_nonfatal=1&fifo_size=1000000"),
    "POST_URL": os.environ.get("POST_URL", "http://simula.frikanalen.tv:9094/frikanalen.ts"),
    "PRE_MIN_DBFS": float(os.environ.get("PRE_MIN_DBFS", "-50")),
    "POST_MAX_DBFS": float(os.environ.get("POST_MAX_DBFS", "-70")),
    "JAM_SECONDS": int(os.environ.get("JAM_SECONDS", "45")),
    "STALE_SECONDS": float(os.environ.get("STALE_SECONDS", "5")),
    "COOLDOWN_SECONDS": int(os.environ.get("COOLDOWN_SECONDS", "600")),
    "MAX_RESTARTS": int(os.environ.get("MAX_RESTARTS", "3")),
    "WINDOW_SECONDS": int(os.environ.get("WINDOW_SECONDS", "21600")),
    "RESTART_COMMAND": os.environ.get("RESTART_COMMAND", "systemctl restart nodm"),
    "DRY_RUN": os.environ.get("DRY_RUN", "true").lower() in ("1", "true", "yes"),
    "STATE_FILE": os.environ.get("STATE_FILE", "/var/lib/caspar-sdi-audio-watchdog/state.json"),
    "METRICS_FILE": os.environ.get("METRICS_FILE", ""),
    "ATEM_URL": os.environ.get("ATEM_URL", "").rstrip("/"),
    "ATEM_TOKEN": os.environ.get("ATEM_TOKEN", ""),
    "ATEM_INPUT": int(os.environ.get("ATEM_INPUT", "1")),
    "ATEM_ME": int(os.environ.get("ATEM_ME", "0")),
    "ON_UNKNOWN_PROGRAM": os.environ.get("ON_UNKNOWN_PROGRAM", "restart"),
}

RATE = 8000  # Hz, mono s16le; plenty for an RMS level
BYTES_PER_SECOND = RATE * 2


def log(level, msg, **fields):
    """One line per event; journald → Alloy → Loki. key=value for LogQL."""
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    print(f"{level} {msg} {extra}".rstrip(), flush=True)


class Level:
    """Reads one source with ffmpeg; keeps the last 1-second RMS in dBFS."""

    def __init__(self, name, url):
        self.name = name
        self.url = url
        self.dbfs = None
        self.updated = 0.0
        self.proc = None
        threading.Thread(target=self._run, daemon=True, name=name).start()

    def fresh(self):
        return self.dbfs is not None and time.monotonic() - self.updated < CFG["STALE_SECONDS"]

    def _run(self):
        backoff = 2
        while True:
            cmd = [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-analyzeduration", "2000000", "-probesize", "2000000",
                "-i", self.url, "-map", "0:a:0", "-vn", "-sn", "-dn",
                "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-",
            ]
            started = time.monotonic()
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                while True:
                    buf = self.proc.stdout.read(BYTES_PER_SECOND)
                    if len(buf) < BYTES_PER_SECOND:
                        break
                    n = len(buf) // 2
                    acc = 0
                    mv = memoryview(buf).cast("h")
                    for s in mv:
                        acc += s * s
                    rms = math.sqrt(acc / n) / 32768.0
                    self.dbfs = 20 * math.log10(rms) if rms > 0 else -120.0
                    self.updated = time.monotonic()
            except Exception as e:  # keep going; fail-safe is "no fresh level"
                log("WARN", "reader error", source=self.name, error=repr(e))
            finally:
                if self.proc and self.proc.poll() is None:
                    self.proc.kill()
                if self.proc:
                    self.proc.wait()
            log("WARN", "reader stopped, restarting", source=self.name)
            if time.monotonic() - started > 60:
                backoff = 2
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def program_decision(state, input_id, me=0):
    """Read an atemkontroll /state document.

    Returns (verdict, detail), verdict one of:
      "on_air"  - our input is on program, no transition: restarting is right
      "inhibit" - another source is on program, or a transition or
                  fade-to-black is under way
      "unknown" - the mixer state can't be trusted
    """
    if not isinstance(state, dict) or not state.get("connected"):
        return "unknown", "atemkontroll not connected to the mixer"
    mes = [m for m in state.get("mixEffects") or [] if isinstance(m, dict) and m.get("index") == me]
    if not mes:
        return "unknown", f"no M/E {me} in state"
    m = mes[0]
    program = m.get("program")
    if not isinstance(program, int):
        return "unknown", f"program={program!r}"
    ftb = m.get("fadeToBlack") or {}
    if m.get("inTransition") or ftb.get("inTransition"):
        return "inhibit", f"program={program} transition in progress"
    if ftb.get("fullyBlack"):
        return "inhibit", f"program={program} faded to black"
    if program != input_id:
        return "inhibit", f"program={program} (caspar is input {input_id})"
    return "on_air", f"program={program}"


def ask_atem():
    if not CFG["ATEM_URL"]:
        return "unknown", "atemkontroll URL not configured"
    req = urllib.request.Request(CFG["ATEM_URL"] + "/state")
    if CFG["ATEM_TOKEN"]:
        req.add_header("Authorization", "Bearer " + CFG["ATEM_TOKEN"])
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            state = json.load(r)
    except Exception as e:
        return "unknown", f"atemkontroll unreachable: {e!r}"
    return program_decision(state, CFG["ATEM_INPUT"], CFG["ATEM_ME"])


def load_state():
    try:
        with open(CFG["STATE_FILE"]) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"restarts": []}


def save_state(state):
    tmp = CFG["STATE_FILE"] + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, CFG["STATE_FILE"])


def write_metrics(pre, post, jammed_for, restarts_in_window, last_restart):
    path = CFG["METRICS_FILE"]
    if not path or not os.path.isdir(os.path.dirname(path)):
        return
    lines = [
        "# HELP caspar_sdi_audio_pre_dbfs Audio RMS of the channel multicast (before DeckLink).",
        "# TYPE caspar_sdi_audio_pre_dbfs gauge",
        f"caspar_sdi_audio_pre_dbfs {pre.dbfs if pre.fresh() else 'NaN'}",
        "# HELP caspar_sdi_audio_post_dbfs Audio RMS of the program return (after SDI).",
        "# TYPE caspar_sdi_audio_post_dbfs gauge",
        f"caspar_sdi_audio_post_dbfs {post.dbfs if post.fresh() else 'NaN'}",
        "# HELP caspar_sdi_audio_jammed_seconds How long the jammed condition has held.",
        "# TYPE caspar_sdi_audio_jammed_seconds gauge",
        f"caspar_sdi_audio_jammed_seconds {jammed_for}",
        "# HELP caspar_sdi_audio_restarts_window Watchdog restarts in the rate-limit window.",
        "# TYPE caspar_sdi_audio_restarts_window gauge",
        f"caspar_sdi_audio_restarts_window {restarts_in_window}",
        "# HELP caspar_sdi_audio_last_restart_timestamp_seconds Time of the last watchdog restart.",
        "# TYPE caspar_sdi_audio_last_restart_timestamp_seconds gauge",
        f"caspar_sdi_audio_last_restart_timestamp_seconds {last_restart}",
    ]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log("INFO", "starting", **{k.lower(): v for k, v in CFG.items()
                               if not k.endswith("_URL") and k != "ATEM_TOKEN"}, atem_url=CFG["ATEM_URL"] or "unset")
    pre = Level("pre", CFG["PRE_URL"])
    post = Level("post", CFG["POST_URL"])
    state = load_state()
    jam_since = None
    last_status = 0.0
    last_limit_log = 0.0
    last_inhibit_log = 0.0

    while True:
        time.sleep(1)
        now = time.time()
        state["restarts"] = [t for t in state["restarts"] if now - t < CFG["WINDOW_SECONDS"]]
        last_restart = max(state["restarts"], default=0)

        if not (pre.fresh() and post.fresh()):
            if jam_since is not None:
                log("INFO", "input lost, jam timer reset", pre_fresh=pre.fresh(), post_fresh=post.fresh())
            jam_since = None
        elif pre.dbfs > CFG["PRE_MIN_DBFS"] and post.dbfs < CFG["POST_MAX_DBFS"]:
            if jam_since is None:
                jam_since = now
                log("WARN", "possible SDI audio jam", pre_dbfs=f"{pre.dbfs:.1f}", post_dbfs=f"{post.dbfs:.1f}")
        else:
            if jam_since is not None and now - jam_since >= 5:
                log("INFO", "condition cleared", after_s=int(now - jam_since),
                    pre_dbfs=f"{pre.dbfs:.1f}", post_dbfs=f"{post.dbfs:.1f}")
            jam_since = None

        jammed_for = int(now - jam_since) if jam_since else 0
        write_metrics(pre, post, jammed_for, len(state["restarts"]), last_restart)

        if now - last_status >= 60:
            last_status = now
            log("INFO", "status",
                pre_dbfs=f"{pre.dbfs:.1f}" if pre.fresh() else "stale",
                post_dbfs=f"{post.dbfs:.1f}" if post.fresh() else "stale",
                jammed_s=jammed_for, restarts_in_window=len(state["restarts"]))

        if jammed_for < CFG["JAM_SECONDS"]:
            continue

        if now - last_restart < CFG["COOLDOWN_SECONDS"]:
            continue  # the last restart may still be settling
        if len(state["restarts"]) >= CFG["MAX_RESTARTS"]:
            if now - last_limit_log >= 300:
                last_limit_log = now
                log("CRIT", "SDI audio jammed but restart limit reached; not restarting",
                    jammed_s=jammed_for, restarts_in_window=len(state["restarts"]),
                    window_s=CFG["WINDOW_SECONDS"])
            continue

        verdict, detail = ask_atem()
        if verdict == "unknown":
            verdict = "on_air" if CFG["ON_UNKNOWN_PROGRAM"] == "restart" else "inhibit"
            detail = f"program unknown ({detail}); on_unknown_program={CFG['ON_UNKNOWN_PROGRAM']}"
        if verdict == "inhibit":
            if now - last_inhibit_log >= 300:
                last_inhibit_log = now
                log("WARN", "SDI audio jammed; restart inhibited", reason=repr(detail), jammed_s=jammed_for)
            continue
        if CFG["DRY_RUN"]:
            log("CRIT", "SDI audio jammed; DRY RUN, would restart", command=repr(CFG["RESTART_COMMAND"]), air=repr(detail),
                jammed_s=jammed_for, pre_dbfs=f"{pre.dbfs:.1f}", post_dbfs=f"{post.dbfs:.1f}")
        else:
            log("CRIT", "SDI audio jammed; restarting CasparCG", command=repr(CFG["RESTART_COMMAND"]), air=repr(detail),
                jammed_s=jammed_for, pre_dbfs=f"{pre.dbfs:.1f}", post_dbfs=f"{post.dbfs:.1f}",
                restarts_in_window=len(state["restarts"]) + 1)
            rc = subprocess.call(CFG["RESTART_COMMAND"], shell=True)
            log("INFO", "restart command finished", rc=rc)
        # A dry run also counts, so it logs at the same rate a real run would act.
        state["restarts"].append(now)
        save_state(state)
        jam_since = None


if __name__ == "__main__":
    main()
